"""Tests for per-customer personalization: rule greeting + LLM background."""

import json

import httpx
import pytest

from app.batch.personalizer import (
    CACHE_KEY,
    FALLBACK_OPENING,
    Personalizer,
    build_opening_text,
    extract_name,
    extract_title,
)

FALLBACK_BACKGROUND = "品牌体验中心新品试用回访"


def make_personalizer(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return Personalizer(
        api_key="test-key",
        model="qwen-flash",
        base_url="https://mock.local/v1",
        fallback_background=FALLBACK_BACKGROUND,
        client=client,
    )


def chat_response(content):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


# -- rule-based opening --------------------------------------------------------


def test_opening_with_name_and_gender():
    assert build_opening_text({"姓名": "王芳", "性别": "女"}) == "您好，请问是王芳女士吗？"
    assert build_opening_text({"姓名": "李强", "性别": "男"}) == "您好，请问是李强先生吗？"


def test_opening_with_name_only():
    assert build_opening_text({"客户姓名": "张三"}) == "您好，请问是张三吗？"


def test_opening_without_name_falls_back():
    assert build_opening_text({"产品": "扫地机"}) == FALLBACK_OPENING
    assert build_opening_text({}) == FALLBACK_OPENING
    assert build_opening_text({"姓名": ""}) == FALLBACK_OPENING


def test_name_candidates_follow_hint_order():
    raw = {"备注": "王芳备注", "称呼": "老王", "姓名": "王芳"}
    # 姓名 matches first in the hints tuple even though 称呼 also matches.
    assert extract_name(raw) == "王芳"


def test_title_falls_back_to_honorific_in_any_field():
    assert extract_title({"联系人": "王女士"}) == "女士"
    assert extract_title({"称呼": "李先生"}) == "先生"
    assert extract_title({"产品": "扫地机"}) == ""
    # Gender column wins over embedded honorifics.
    assert extract_title({"性别": "女", "联系人": "李先生"}) == "女士"


def test_cache_keys_are_ignored_by_extraction():
    raw = {"_personalized_background": "张先生", CACHE_KEY: "x"}
    assert extract_title(raw) == ""


# -- LLM background ------------------------------------------------------------


async def test_personalize_generates_background_via_llm():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return chat_response("该客户5月购买过扫地机器人，回访重点是耗材。")

    personalizer = make_personalizer(handler)
    result = await personalizer.personalize(
        {"姓名": "王芳", "性别": "女", "购买产品": "扫地机器人"}
    )
    assert result.opening_text == "您好，请问是王芳女士吗？"
    assert result.business_background == "该客户5月购买过扫地机器人，回访重点是耗材。"
    assert result.generated is True
    assert seen["auth"] == "Bearer test-key"
    user_content = seen["body"]["messages"][1]["content"]
    assert FALLBACK_BACKGROUND in user_content
    assert "扫地机器人" in user_content


async def test_personalize_truncates_long_background():
    personalizer = make_personalizer(lambda request: chat_response("长" * 500))
    result = await personalizer.personalize({"姓名": "王芳"})
    assert len(result.business_background) == 300


async def test_personalize_http_failure_falls_back():
    personalizer = make_personalizer(lambda request: httpx.Response(500))
    result = await personalizer.personalize({"姓名": "王芳"})
    assert result.business_background == FALLBACK_BACKGROUND
    assert result.generated is False
    assert result.opening_text == "您好，请问是王芳吗？"


async def test_personalize_transport_error_falls_back():
    def handler(request):
        raise httpx.ConnectError("boom")

    personalizer = make_personalizer(handler)
    result = await personalizer.personalize({"姓名": "王芳"})
    assert result.business_background == FALLBACK_BACKGROUND
    assert result.generated is False


async def test_personalize_malformed_response_falls_back():
    personalizer = make_personalizer(lambda request: httpx.Response(200, json={}))
    result = await personalizer.personalize({"姓名": "王芳"})
    assert result.generated is False


async def test_personalize_uses_cache_without_http_call():
    calls = []

    def handler(request):
        calls.append(request)
        return chat_response("不该出现")

    personalizer = make_personalizer(handler)
    raw = {"姓名": "王芳", CACHE_KEY: "上次生成的背景"}
    result = await personalizer.personalize(raw)
    assert result.business_background == "上次生成的背景"
    assert result.generated is False
    assert calls == []


async def test_internal_keys_never_leak_into_prompt():
    seen = {}

    def handler(request):
        seen["content"] = json.loads(request.content)["messages"][1]["content"]
        return chat_response("ok")

    personalizer = make_personalizer(handler)
    await personalizer.personalize({"姓名": "王芳", "_secret": "机密缓存"})
    assert "机密缓存" not in seen["content"]
