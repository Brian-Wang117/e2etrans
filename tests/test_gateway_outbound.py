"""Integration tests: outbound call engine wired into the realtime gateway."""

import asyncio

import pytest

from app.config import OutboundSettings
from app.outbound import call_director
from app.outbound.adjudicator import AdjudicationResult
from app.realtime import doubao_protocol as protocol
from app.realtime.gateway import OUTBOUND_SCENARIO, RealtimeGateway
from tests.test_gateway import FakeDoubao, FakeWebSocket, envelope, find, wait_until

OUTBOUND_SCENARIOS = {OUTBOUND_SCENARIO: "默认开场白"}


class FakeAdjudicator:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def adjudicate(self, dialogue):
        self.calls.append(list(dialogue))
        return self._result

    async def aclose(self):
        pass


def make_outbound_gateway(
    settings, repository, doubao, adjudicator=None, seen=None,
    outbound_settings=None,
):
    def factory(session_id, input_mode, persona=None, speaker=None):
        if seen is not None:
            seen["session_id"] = session_id
            seen["input_mode"] = input_mode
            seen["persona"] = persona
            seen["speaker"] = speaker
        return doubao

    return RealtimeGateway(
        settings=settings,
        repository=repository,
        doubao_factory=factory,
        translator=None,
        scenarios=OUTBOUND_SCENARIOS,
        outbound_settings=outbound_settings or OutboundSettings(enabled=True),
        adjudicator=adjudicator,
    )


def start_payload(opening_text="您好，这里是活动回访，耽误您一分钟可以吗？", gender=None, speaker=None):
    payload = {"scenario_id": OUTBOUND_SCENARIO, "opening_text": opening_text}
    if gender is not None:
        payload["gender"] = gender
    if speaker is not None:
        payload["speaker"] = speaker
    return envelope("session.start", 1, payload)


@pytest.fixture(autouse=True)
def fast_speech_estimates(monkeypatch):
    """Shrink TTS playback estimates so hang-ups happen quickly in tests."""
    monkeypatch.setattr(call_director, "SPEECH_SECONDS_PER_CHAR", 0.001)
    monkeypatch.setattr(call_director, "SPEECH_BUFFER_SECONDS", 0.05)
    monkeypatch.setattr(call_director, "GOODBYE_MARGIN_SECONDS", 0.05)


async def test_outbound_session_forces_audio_mode_and_persona(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_outbound_gateway(
        settings, repository, doubao, seen=seen
    )
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        assert seen["input_mode"] == "audio"
        assert seen["persona"] is not None
        assert seen["persona"].bot_name
        assert doubao.greetings[0].startswith("您好，这里是活动回访")
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_script_hit_intercepts_reports_and_hangs_up(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_outbound_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        doubao.emit(
            451, {"results": [{"text": "你们是不是打错了？", "is_interim": False}]}
        )
        await wait_until(lambda: doubao.interrupts >= 1)
        # The fixed reply is injected straight into TTS.
        script_reply = doubao.greetings[-1]
        assert "再见" in script_reply
        await wait_until(lambda: find(websocket.sent, "script.hit"))

        doubao.emit(459, {})
        # One-shot report: result.reported exactly once, stored once.
        await wait_until(lambda: find(websocket.sent, "result.reported"))
        reported = find(websocket.sent, "result.reported")[0]
        assert reported["payload"]["result"] == "不感兴趣"

        # Scheduled hang-up ends the session cleanly without client help.
        await asyncio.wait_for(task, timeout=10)
        results = await asyncio.to_thread(
            repository.list_sessions
        )
        session_id = seen_session_id(results)
        call_result = await asyncio.to_thread(
            repository.get_call_result, session_id
        )
        assert call_result["result"] == "不感兴趣"
        assert call_result["reason"].startswith("固定话术匹配")
        assert call_result["status"] == "已完成"
    finally:
        if not task.done():
            task.cancel()


def seen_session_id(sessions):
    assert len(sessions) == 1
    return sessions[0]["id"]


TWO_SECONDS_PCM_24K = b"\x00\x01" * 48000  # 2s of 24kHz PCM16


async def test_hangup_waits_for_farewell_playback_tail(
    repository, settings, monkeypatch
):
    """The 359-based goodbye timer must not hang up while the farewell audio
    is still queued in the browser (TTS synthesizes faster than realtime)."""
    # Realistic-ish estimate for the farewell so the injected timer outlives
    # the assertions below (the autouse fixture shrinks it for other tests).
    monkeypatch.setattr(call_director, "SPEECH_SECONDS_PER_CHAR", 0.05)
    monkeypatch.setattr(call_director, "SPEECH_BUFFER_SECONDS", 2.0)

    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_outbound_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # end_call script hit: mute input + hang-up scheduled after the
        # estimated farewell duration.
        doubao.emit(
            451, {"results": [{"text": "你们是不是打错了？", "is_interim": False}]}
        )
        await wait_until(lambda: find(websocket.sent, "script.hit"))
        doubao.emit(459, {})

        # Farewell TTS: 2 seconds of audio queued, then synthesis ends.
        doubao.emit(350, {})
        doubao.emit(352, TWO_SECONDS_PCM_24K)
        doubao.emit(359, {})
        await wait_until(lambda: find(websocket.sent, "assistant.audio.done"))

        # The goodbye timer (short margin) must NOT cut the 2s playback tail.
        await asyncio.sleep(1.0)
        assert not task.done(), "hung up before the farewell finished playing"
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_residual_asr_after_mute_does_not_interrupt_farewell(
    repository, settings
):
    """After the farewell starts (input muted), in-flight residual ASR of
    audio uploaded before the mute must not barge-in and truncate it."""
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_outbound_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Natural farewell: assistant reply contains a farewell word ->
        # director closes the call (mute input + adjudication).
        doubao.emit(451, {"results": [{"text": "活动怎么参加", "is_interim": False}]})
        doubao.emit(459, {})
        doubao.emit(550, {"content": "稍后短信发您链接，那先这样，再见。"})
        doubao.emit(559, {})
        await wait_until(lambda: find(websocket.sent, "assistant.text.done"))

        # Farewell audio plays; residual ASR arrives while it is audible.
        doubao.emit(352, TWO_SECONDS_PCM_24K)
        doubao.emit(451, {"results": [{"text": "嗯不用了", "is_interim": False}]})
        await asyncio.sleep(0.1)
        assert doubao.interrupts == 0, "residual ASR interrupted the farewell"
        assert not find(websocket.sent, "response.cancelled")

        doubao.emit(359, {})
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_natural_farewell_runs_adjudication_then_hangs_up(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    adjudicator = FakeAdjudicator(
        AdjudicationResult(verdict="感兴趣", reason="客户询问参与方式", model="qwen-flash")
    )
    gateway = make_outbound_gateway(
        settings, repository, doubao, adjudicator=adjudicator
    )
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Customer asks a question (no script hit).
        doubao.emit(451, {"results": [{"text": "活动怎么参加", "is_interim": False}]})
        doubao.emit(459, {})
        # Assistant replies naturally, ending with a farewell word.
        doubao.emit(550, {"content": "稍后短信发您链接，那先这样，再见。"})
        doubao.emit(559, {})
        await wait_until(lambda: adjudicator.calls)
        await wait_until(lambda: find(websocket.sent, "result.reported"))
        reported = find(websocket.sent, "result.reported")[0]
        assert reported["payload"]["result"] == "感兴趣"

        # Farewell TTS finishes -> hang-up is scheduled.
        doubao.emit(359, {})
        await asyncio.wait_for(task, timeout=10)

        # Muted input after farewell: further audio is dropped.
        assert len(doubao.audio) == 0
        sessions = await asyncio.to_thread(repository.list_sessions)
        call_result = await asyncio.to_thread(
            repository.get_call_result, sessions[0]["id"]
        )
        assert call_result["result"] == "感兴趣"
        assert call_result["end_reason"] == "正常结束"
    finally:
        if not task.done():
            task.cancel()


async def test_script_report_is_not_overwritten_by_teardown(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_outbound_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        doubao.emit(451, {"results": [{"text": "我要投诉", "is_interim": False}]})
        await wait_until(lambda: find(websocket.sent, "result.reported"))
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
        sessions = await asyncio.to_thread(repository.list_sessions)
        call_result = await asyncio.to_thread(
            repository.get_call_result, sessions[0]["id"]
        )
        assert call_result["reason"].startswith("固定话术匹配")
    finally:
        if not task.done():
            task.cancel()


def speaker_settings():
    return OutboundSettings(
        enabled=True,
        tts_speaker_male="zh_male_mock",
        tts_speaker_female="zh_female_mock",
    )


async def test_gender_crosses_tts_speaker(repository, settings):
    # Male customer hears the female voice and vice versa.
    for gender, expected in (("male", "zh_female_mock"), ("female", "zh_male_mock")):
        websocket = FakeWebSocket()
        doubao = FakeDoubao("pending")
        seen = {}
        gateway = make_outbound_gateway(
            settings, repository, doubao, seen=seen,
            outbound_settings=speaker_settings(),
        )
        task = asyncio.create_task(gateway.run(websocket))
        try:
            websocket.feed(start_payload(gender=gender))
            await wait_until(lambda: doubao.connected)
            await wait_until(lambda: find(websocket.sent, "session.ready"))
            assert seen["speaker"] == expected
            websocket.feed(envelope("session.end", 2, {}))
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()


async def test_missing_gender_falls_back_to_default_speaker(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_outbound_gateway(
        settings, repository, doubao, seen=seen,
        outbound_settings=speaker_settings(),
    )
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload())
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        # None keeps DoubaoRealtimeClient on settings.speaker (DOUBAO_TTS_SPEAKER).
        assert seen["speaker"] is None
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_explicit_clone_speaker_wins_over_gender_crossing(
    repository, settings
):
    # A workbench-selected clone voice overrides gender crossing, and the
    # persona gains the anti-recitation guard for the sampling text.
    from app.outbound.persona import CLONE_GUARD

    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_outbound_gateway(
        settings, repository, doubao, seen=seen,
        outbound_settings=speaker_settings(),
    )
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(
            start_payload(gender="male", speaker="S_heWZZwa62")
        )
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        assert seen["speaker"] == "S_heWZZwa62"
        assert CLONE_GUARD in seen["persona"].system_role
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
