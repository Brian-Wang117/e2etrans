"""Tests for the single-call state machine (interception, guards, reporting)."""

import pytest

from app.outbound.adjudicator import AdjudicationResult
from app.outbound.call_director import (
    CallDirector,
    Interrupt,
    MuteInput,
    Notify,
    Report,
    Say,
    ScheduleHangup,
    StartAdjudication,
    estimate_speech_seconds,
)
from app.outbound.script_library import BUILTIN_SCRIPTS, ScriptMatcher


def _kinds(actions):
    return [type(action).__name__ for action in actions]


@pytest.fixture
def director():
    return CallDirector(matcher=ScriptMatcher(BUILTIN_SCRIPTS))


def _find(actions, action_type):
    return [action for action in actions if isinstance(action, action_type)]


# -- script interception --------------------------------------------------------


def test_script_hit_interrupts_injects_and_reports(director):
    actions = director.observe_user_final("喂？你们是不是打错了啊")
    assert isinstance(actions[0], Interrupt)
    say = _find(actions, Say)[0]
    assert say.source == "script"
    assert "再见" in say.text
    notify = _find(actions, Notify)[0]
    assert notify.event == "script.hit"
    assert notify.payload["category"] == "身份否认"
    report = _find(actions, Report)[0]
    assert report.status == "已完成"
    assert report.result == "不感兴趣"
    assert report.reason == "固定话术匹配：打错了"


def test_end_call_script_mutes_input_and_schedules_hangup(director):
    actions = director.observe_user_final("打错了")
    assert _find(actions, MuteInput)
    hangup = _find(actions, ScheduleHangup)[0]
    assert hangup.delay_seconds == estimate_speech_seconds(
        _find(actions, Say)[0].text
    )
    assert director.state == "closing"


def test_non_ending_script_keeps_conversation_alive(director):
    actions = director.observe_user_final("我听不太清楚")
    assert _find(actions, Say)
    assert not _find(actions, MuteInput)
    assert not _find(actions, ScheduleHangup)
    assert director.state == "talking"


def test_miss_leaves_the_model_to_answer(director):
    assert director.observe_user_final("你们这个活动具体是什么") == []
    assert director.observe_user_final("嗯嗯") == []


# -- farewell guard ---------------------------------------------------------------


def test_farewell_word_starts_closing_and_adjudication(director):
    actions = director.observe_assistant_done("好的，那祝您生活愉快，再见。")
    assert _kinds(actions) == ["MuteInput", "StartAdjudication"]
    assert director.state == "closing"
    assert director.adjudication_pending is True


def test_goodbye_played_schedules_hangup_with_margin(director):
    director.observe_assistant_done("再见。")
    hangup = _find(director.on_goodbye_played(), ScheduleHangup)[0]
    assert hangup.delay_seconds == 0.5


def test_no_farewell_keeps_talking(director):
    assert director.observe_assistant_done("好的，我给您介绍一下。") == []


# -- turn limit --------------------------------------------------------------------


def test_turn_limit_forces_farewell_and_adjudication():
    director = CallDirector(matcher=ScriptMatcher(BUILTIN_SCRIPTS), max_turns=2)
    director.observe_assistant_done("第一句回复。")
    actions = director.observe_assistant_done("第二句回复。")
    say = _find(actions, Say)[0]
    assert say.source == "farewell"
    assert "再见" in say.text
    assert _find(actions, Interrupt)
    assert _find(actions, StartAdjudication)
    assert director.state == "closing"


# -- silence guard --------------------------------------------------------------------


def test_silence_timeout_reports_not_interested_without_adjudication(director):
    actions = director.on_silence_timeout()
    say = _find(actions, Say)[0]
    assert say.source == "farewell"
    assert "再见" in say.text
    report = _find(actions, Report)[0]
    assert report.result == "不感兴趣"
    assert report.reason == "用户沉默超过60秒"
    assert not _find(actions, StartAdjudication)
    assert _find(actions, ScheduleHangup)


def test_silence_timeout_ignored_after_closing(director):
    director.observe_user_final("打错了")  # enters closing
    assert director.on_silence_timeout() == []


# -- adjudication and reporting -------------------------------------------------------


def test_adjudication_result_is_reported_once(director):
    director.observe_assistant_done("好的，再见。")
    result = AdjudicationResult(verdict="感兴趣", reason="客户询问参与方式", model="qwen-flash")
    actions = director.apply_adjudication(result)
    report = _find(actions, Report)[0]
    assert report.status == "已完成"
    assert report.result == "感兴趣"
    assert director.apply_adjudication(result) == []
    assert director.finish() == []


def test_adjudication_failure_falls_back_to_not_interested(director):
    director.observe_assistant_done("好的，再见。")
    report = _find(director.apply_adjudication(None), Report)[0]
    assert report.result == "不感兴趣"
    assert report.reason == "终裁失败"


def test_script_report_blocks_later_adjudication_report(director):
    director.observe_user_final("别再给我打电话了")
    director.adjudication_pending = True
    result = AdjudicationResult(verdict="感兴趣", reason="", model="m")
    assert director.apply_adjudication(result) == []


def test_finish_without_report_defaults_to_neutral(director):
    report = _find(director.finish(), Report)[0]
    assert report.status == "已完成"
    assert report.result == "中立"
    assert report.reason == "客户提前挂断"


def test_abnormal_finish_marks_failure(director):
    report = _find(director.finish(abnormal=True), Report)[0]
    assert report.status == "失败"


def test_finish_while_adjudication_pending_reports_nothing(director):
    director.observe_assistant_done("再见。")
    assert director.finish() == []
    assert director.adjudication_pending is True


# -- dialogue transcript ----------------------------------------------------------------


def test_dialogue_collects_opening_turns_and_injections(director):
    director.start("您好，打扰您一分钟。")
    director.observe_user_final("你们是什么活动？")
    director.observe_assistant_done("是新品试用回访。")
    director.observe_user_final("打错了")
    dialogue = director.build_dialogue()
    assert dialogue[0] == ("客服", "您好，打扰您一分钟。")
    assert ("客户", "你们是什么活动？") in dialogue
    assert ("客服", "是新品试用回访。") in dialogue
    # The fixed script reply is recorded for adjudication.
    assert dialogue[-1][0] == "客服"
    assert "再见" in dialogue[-1][1]
