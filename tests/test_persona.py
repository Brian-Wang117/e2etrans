"""Tests for the outbound persona builder."""

from app.outbound.persona import (
    DEFAULT_BOT_NAME,
    FALLBACK_BUSINESS_BACKGROUND,
    MAX_BOT_NAME_CHARS,
    build_persona,
)


def test_business_background_is_embedded_in_system_role():
    persona = build_persona(business_background="某品牌的新品试用回访活动。")
    assert "某品牌的新品试用回访活动。" in persona.system_role
    assert persona.bot_name == DEFAULT_BOT_NAME


def test_empty_background_falls_back_to_default():
    persona = build_persona(business_background="   ")
    assert FALLBACK_BUSINESS_BACKGROUND in persona.system_role


def test_bot_name_is_truncated_to_vendor_limit():
    persona = build_persona(
        business_background="背景",
        bot_name="一个特别特别长的机器人名字超过二十个字符了确实如此",
    )
    assert len(persona.bot_name) == MAX_BOT_NAME_CHARS


def test_custom_speaking_style_is_kept():
    persona = build_persona(business_background="背景", speaking_style="活泼一点")
    assert persona.speaking_style == "活泼一点"
