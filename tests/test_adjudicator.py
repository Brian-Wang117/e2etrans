"""Tests for the end-of-call interest adjudicator."""

import httpx
import pytest

from app.outbound.adjudicator import AdjudicationError, OutboundAdjudicator

DIALOGUE = [("客户", "你们这个活动怎么参加？"), ("客服", "稍后给您发送短信链接。")]


def _adjudicator(content: str, *, status: int = 200) -> OutboundAdjudicator:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        if status != 200:
            return httpx.Response(status, json={"error": "denied"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OutboundAdjudicator(
        api_key="key",
        model="qwen-flash",
        base_url="https://example.invalid/v1/",
        client=client,
    )


async def test_plain_json_is_parsed():
    adjudicator = _adjudicator('{"result": "感兴趣", "reason": "客户主动询问参与方式"}')
    result = await adjudicator.adjudicate(DIALOGUE)
    assert result.verdict == "感兴趣"
    assert result.reason == "客户主动询问参与方式"
    assert result.model == "qwen-flash"


async def test_fenced_json_is_parsed():
    adjudicator = _adjudicator(
        '```json\n{"result": "不感兴趣", "reason": "拒绝"}\n```'
    )
    result = await adjudicator.adjudicate(DIALOGUE)
    assert result.verdict == "不感兴趣"


async def test_verdict_substring_is_normalized_without_confusing_negation():
    adjudicator = _adjudicator('{"result": "客户明显不感兴趣", "reason": ""}')
    result = await adjudicator.adjudicate(DIALOGUE)
    assert result.verdict == "不感兴趣"


async def test_unknown_verdict_falls_back_to_neutral():
    adjudicator = _adjudicator('{"result": "犹豫", "reason": "不确定"}')
    result = await adjudicator.adjudicate(DIALOGUE)
    assert result.verdict == "中立"


async def test_legacy_verdict_key_is_still_accepted():
    adjudicator = _adjudicator('{"verdict": "感兴趣", "reason": "兼容旧键"}')
    result = await adjudicator.adjudicate(DIALOGUE)
    assert result.verdict == "感兴趣"


async def test_non_json_content_raises_malformed():
    adjudicator = _adjudicator("我无法判断")
    with pytest.raises(AdjudicationError) as excinfo:
        await adjudicator.adjudicate(DIALOGUE)
    assert excinfo.value.category == "malformed"


@pytest.mark.parametrize("status, category", [(401, "auth"), (429, "rate_limit"), (500, "transport")])
async def test_http_errors_are_categorized(status, category):
    adjudicator = _adjudicator("", status=status)
    with pytest.raises(AdjudicationError) as excinfo:
        await adjudicator.adjudicate(DIALOGUE)
    assert excinfo.value.category == category


async def test_empty_dialogue_is_rejected():
    adjudicator = _adjudicator("{}")
    with pytest.raises(AdjudicationError) as excinfo:
        await adjudicator.adjudicate([])
    assert excinfo.value.category == "malformed"


async def test_reason_is_truncated():
    long_reason = "理由" * 300
    adjudicator = _adjudicator(f'{{"result": "中立", "reason": "{long_reason}"}}')
    result = await adjudicator.adjudicate(DIALOGUE)
    assert len(result.reason) == 200
