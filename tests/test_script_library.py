"""Tests for the fixed-reply script library: matching, priority, validation."""

import pytest

from app.outbound.script_library import (
    BUILTIN_SCRIPTS,
    MAX_REPLY_CHARS,
    Script,
    ScriptMatcher,
    validate_script,
)


def _script(
    *,
    triggers=("打错了",),
    reply="非常抱歉打扰到您了，可能是我们这边登记有误，祝您生活愉快，再见。",
    end_call=True,
    verdict="不感兴趣",
    priority=5,
    category="测试",
):
    return Script(
        category=category,
        triggers=triggers,
        reply=reply,
        end_call=end_call,
        verdict=verdict,
        priority=priority,
    )


# -- ordered-subsequence matching ------------------------------------------------


def test_trigger_matches_with_characters_in_between():
    matcher = ScriptMatcher(BUILTIN_SCRIPTS)
    hit = matcher.match("你、你别再给我打电话了！")
    assert hit is not None
    assert hit.script.category == "投诉免打扰"
    assert hit.trigger == "别再打"


def test_trigger_order_must_be_preserved():
    matcher = ScriptMatcher(BUILTIN_SCRIPTS)
    # Characters of "打错了" appear but in the wrong order: no hit.
    assert matcher.match("错了打没有") is None


def test_punctuation_and_whitespace_are_ignored():
    matcher = ScriptMatcher(BUILTIN_SCRIPTS)
    hit = matcher.match("我 听 不 太 清 楚。")
    assert hit is not None
    assert hit.script.category == "听不清"


def test_empty_and_interjection_only_text_never_match():
    matcher = ScriptMatcher(BUILTIN_SCRIPTS)
    assert matcher.match("") is None
    assert matcher.match("嗯嗯，啊，哦") is None


def test_highest_priority_wins_when_multiple_scripts_hit():
    low = _script(triggers=("不需要",), priority=3, category="低优先级")
    high = _script(
        triggers=("投诉",),
        priority=9,
        category="高优先级",
        reply="很抱歉给您带来困扰，我会把您加入免打扰名单，不再来电，再见。",
    )
    matcher = ScriptMatcher([low, high])
    hit = matcher.match("不需要了，我要投诉")
    assert hit is not None
    assert hit.script.category == "高优先级"


def test_longer_trigger_breaks_priority_ties():
    short = _script(triggers=("投诉",), priority=7, category="短触发词")
    long = _script(
        triggers=("我要投诉",),
        priority=7,
        category="长触发词",
        reply="很抱歉给您带来困扰，我会把您加入免打扰名单，不再来电，再见。",
    )
    matcher = ScriptMatcher([short, long])
    hit = matcher.match("我要投诉你们")
    assert hit is not None
    assert hit.script.category == "长触发词"


def test_library_order_breaks_remaining_ties():
    first = _script(triggers=("投诉",), priority=7, category="先入库")
    second = _script(
        triggers=("举报",),
        priority=7,
        category="后入库",
        reply="很抱歉给您带来困扰，我会把您加入免打扰名单，不再来电，再见。",
    )
    matcher = ScriptMatcher([first, second])
    hit = matcher.match("我要投诉和举报")
    assert hit is not None
    assert hit.script.category == "先入库"


# -- validation -------------------------------------------------------------------


def test_builtin_scripts_all_pass_validation():
    for script in BUILTIN_SCRIPTS:
        assert validate_script(script) == [], script.category


@pytest.mark.parametrize(
    "override, expected_fragment",
    [
        ({"reply": "太短了，再见。"}, "reply length"),
        ({"reply": "很" * (MAX_REPLY_CHARS + 1) + "再见"}, "reply length"),
        ({"end_call": True, "reply": "不好意思，可能是我语速有点快，我放慢一点再给您说一遍。"}, "farewell"),
        (
            {"reply": "这个产品只要99元，非常划算，您可以了解一下，再见。"},
            "hard-coded price",
        ),
        ({"priority": 11}, "priority"),
        ({"priority": -1}, "priority"),
        ({"verdict": "也许感兴趣"}, "verdict"),
        ({"triggers": ()}, "triggers"),
        ({"triggers": ("99元",)}, "hard-coded price"),
    ],
)
def test_validation_rejects_violations(override, expected_fragment):
    problems = validate_script(_script(**override))
    assert any(expected_fragment in problem for problem in problems), problems


def test_empty_verdict_is_allowed_for_non_classifying_scripts():
    assert validate_script(_script(verdict="")) == []
