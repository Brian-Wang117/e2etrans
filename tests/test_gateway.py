"""End-to-end gateway tests driven by a fake Doubao client and websocket."""

import asyncio
import base64
import json

from starlette.websockets import WebSocketDisconnect

from app.realtime import doubao_protocol as protocol
from app.realtime.gateway import RealtimeGateway

SCENARIOS = {"product_intro": "Hello! How can I help you today?"}


def envelope(event_type, seq, payload):
    return json.dumps(
        {
            "v": 1,
            "type": event_type,
            "event_id": f"evt-{seq}",
            "session_id": None,
            "turn_id": None,
            "seq": seq,
            "ts_ms": 1_700_000_000_000,
            "payload": payload,
        }
    )


class FakeWebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_code = None
        self.close_reason = None

    def feed(self, message):
        self.incoming.put_nowait(message)

    def feed_disconnect(self):
        self.incoming.put_nowait(None)

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect(code=1006)
        return message

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self, code=1000, reason=None):
        self.close_code = code
        self.close_reason = reason


class FakeDoubao:
    def __init__(self, session_id):
        self.session_id = session_id
        self.connected = False
        self.greetings = []
        self.audio = bytearray()
        self.end_asr_calls = 0
        self.text_queries = []
        self.interrupts = 0
        self.finished = False
        self.closed = False
        self.frames = asyncio.Queue()

    async def connect(self, *, send_greeting=False):
        self.connected = True

    async def say_hello(self, text):
        self.greetings.append(text)

    async def send_audio(self, audio):
        self.audio.extend(audio)

    async def end_asr(self):
        self.end_asr_calls += 1

    async def send_text_query(self, text):
        self.text_queries.append(text)

    async def interrupt(self):
        self.interrupts += 1

    async def receive(self):
        return await self.frames.get()

    async def finish(self):
        self.finished = True

    async def close(self):
        self.closed = True

    def emit(self, event, payload):
        self.frames.put_nowait(
            protocol.DoubaoFrame(
                message_type=protocol.SERVER_FULL_RESPONSE,
                flags=protocol.FLAG_WITH_EVENT,
                event=event,
                session_id=self.session_id,
                payload=payload,
            )
        )


def make_gateway(settings, repository, doubao, seen=None):
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
        scenarios=SCENARIOS,
    )


def find(sent, event_type):
    return [event for event in sent if event["type"] == event_type]


async def wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


async def test_full_voice_turn_end_to_end(repository, settings):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(envelope("session.start", 1, {"scenario_id": "product_intro"}))
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        ready = find(websocket.sent, "session.ready")[0]
        session_id = ready["session_id"]
        assert doubao.connected is True
        assert doubao.greetings == [SCENARIOS["product_intro"]]
        assert ready["payload"]["subtitles"] == "disabled"

        pcm = b"\x01\x02" * 640
        websocket.feed(
            envelope(
                "input_audio.append",
                2,
                {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "duration_ms": 40,
                    "audio_b64": base64.b64encode(pcm).decode(),
                },
            )
        )
        await wait_until(lambda: len(doubao.audio) == len(pcm))
        websocket.feed(envelope("input_audio.commit", 3, {}))
        await wait_until(lambda: doubao.end_asr_calls == 1)

        doubao.emit(451, {"results": [{"text": "你好", "is_interim": True}]})
        await wait_until(lambda: find(websocket.sent, "asr.partial"))
        doubao.emit(451, {"results": [{"text": "你好", "is_interim": False}]})
        await wait_until(lambda: find(websocket.sent, "asr.final"))
        doubao.emit(459, {})
        await wait_until(lambda: len(find(websocket.sent, "turn.completed")) == 1)

        doubao.emit(550, {"content": "Hello"})
        doubao.emit(550, {"content": "Hello there"})
        await wait_until(lambda: len(find(websocket.sent, "assistant.text.delta")) == 2)
        deltas = [e["payload"]["delta"] for e in find(websocket.sent, "assistant.text.delta")]
        assert deltas == ["Hello", " there"]
        doubao.emit(559, {})
        await wait_until(lambda: find(websocket.sent, "assistant.text.done"))
        done = find(websocket.sent, "assistant.text.done")[0]
        assert done["payload"]["text"] == "Hello there"

        doubao.emit(350, {})
        doubao.emit(352, b"\x00\x01" * 480)
        await wait_until(lambda: find(websocket.sent, "assistant.audio.chunk"))
        chunk = find(websocket.sent, "assistant.audio.chunk")[0]["payload"]
        assert chunk["chunk_seq"] == 1
        assert chunk["sample_rate_hz"] == settings.output_sample_rate
        assert base64.b64decode(chunk["audio_b64"]) == b"\x00\x01" * 480
        doubao.emit(359, {})
        await wait_until(lambda: find(websocket.sent, "assistant.audio.done"))
        await wait_until(lambda: len(find(websocket.sent, "turn.completed")) == 2)

        websocket.feed(envelope("ping", 4, {}))
        await wait_until(lambda: find(websocket.sent, "pong"))
        websocket.feed(envelope("ping", 4, {}))  # stale seq: must be ignored
        websocket.feed(envelope("session.end", 5, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()

    assert doubao.finished is True
    assert websocket.close_code == 1000
    assert find(websocket.sent, "session.ended")
    session = repository.get_session(session_id)
    assert session["ended_at"] is not None
    speakers = [turn["speaker"] for turn in session["turns"]]
    assert speakers == ["tester", "agent"]
    tester_turn, agent_turn = session["turns"]
    assert tester_turn["source_text"] == "你好"
    assert agent_turn["source_text"] == "Hello there"


async def test_text_submit_path(repository, settings):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(envelope("session.start", 1, {"scenario_id": "product_intro"}))
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        session_id = find(websocket.sent, "session.ready")[0]["session_id"]

        websocket.feed(envelope("user.text.submit", 2, {"text": "hello"}))
        await wait_until(lambda: doubao.text_queries == ["hello"])
        await wait_until(lambda: len(find(websocket.sent, "turn.completed")) == 1)

        websocket.feed(envelope("session.end", 3, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()

    session = repository.get_session(session_id)
    assert [turn["speaker"] for turn in session["turns"]] == ["tester"]
    assert session["turns"][0]["source_text"] == "hello"


async def test_response_cancel_interrupts_active_generation(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(envelope("session.start", 1, {"scenario_id": "product_intro"}))
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        doubao.emit(550, {"content": "Hello"})
        await wait_until(lambda: find(websocket.sent, "assistant.text.delta"))

        websocket.feed(envelope("response.cancel", 2, {"response_id": "response-1"}))
        await wait_until(lambda: find(websocket.sent, "response.cancelled"))
        assert doubao.interrupts == 1

        # Late audio for the cancelled generation must be dropped.
        doubao.emit(352, b"\x00\x01" * 480)
        await asyncio.sleep(0.1)
        assert find(websocket.sent, "assistant.audio.chunk") == []

        websocket.feed(envelope("session.end", 3, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_non_start_first_message_closes_1008(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    websocket.feed(envelope("ping", 1, {}))
    await asyncio.wait_for(gateway.run(websocket), timeout=10)
    assert websocket.close_code == 1008
    assert doubao.connected is False


async def test_browser_disconnect_closes_upstream(repository, settings):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(envelope("session.start", 1, {"scenario_id": "product_intro"}))
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        websocket.feed_disconnect()
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
    assert doubao.closed is True
    assert doubao.finished is False


async def test_phone_input_mode_reaches_doubao_factory(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_gateway(settings, repository, doubao, seen)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(
            envelope(
                "session.start",
                1,
                {"scenario_id": "product_intro", "input_mode": "audio"},
            )
        )
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        assert seen["input_mode"] == "audio"
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_default_input_mode_reaches_doubao_factory(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_gateway(settings, repository, doubao, seen)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(envelope("session.start", 1, {"scenario_id": "product_intro"}))
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        assert seen["input_mode"] == "push_to_talk"
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_streaming_asr_barge_in_interrupts_playback(
    repository, settings
):
    """Streaming clients never commit audio, so recognized user speech
    (interim or final) while the assistant is speaking must interrupt
    immediately — interim fires as soon as the customer starts talking."""
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(
            envelope(
                "session.start",
                1,
                {"scenario_id": "product_intro", "input_mode": "audio"},
            )
        )
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Interim ASR while the assistant is idle must not interrupt.
        doubao.emit(451, {"results": [{"text": "嗯", "is_interim": True}]})
        await wait_until(lambda: find(websocket.sent, "asr.partial"))
        await asyncio.sleep(0.1)
        assert doubao.interrupts == 0

        # Assistant is mid-reply (generation active).
        doubao.emit(550, {"content": "Hello"})
        await wait_until(lambda: find(websocket.sent, "assistant.text.delta"))

        # Streaming audio chunks must NOT interrupt by themselves.
        pcm = b"\x01\x02" * 320
        websocket.feed(
            envelope(
                "input_audio.append",
                2,
                {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "duration_ms": 20,
                    "audio_b64": base64.b64encode(pcm).decode(),
                },
            )
        )
        await wait_until(lambda: len(doubao.audio) == len(pcm))
        await asyncio.sleep(0.1)
        assert doubao.interrupts == 0

        # Interim ASR while speaking -> immediate barge-in + flush event.
        doubao.emit(451, {"results": [{"text": "等", "is_interim": True}]})
        await wait_until(lambda: doubao.interrupts == 1)
        await wait_until(lambda: find(websocket.sent, "response.cancelled"))

        websocket.feed(envelope("session.end", 3, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_barge_in_stays_armed_during_playback_tail(
    repository, settings
):
    """Upstream TTS streams faster than realtime playback: 359 closes the
    generation while the caller still hears seconds of queued audio. Barge-in
    must stay armed for that audible tail."""
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(
            envelope(
                "session.start",
                1,
                {"scenario_id": "product_intro", "input_mode": "audio"},
            )
        )
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # 2 seconds of TTS audio (24kHz PCM16), then upstream ends the
        # generation while playback is still running.
        doubao.emit(550, {"content": "Hello"})
        doubao.emit(352, b"\x00\x01" * 48000)
        doubao.emit(359, {})
        await wait_until(lambda: find(websocket.sent, "assistant.audio.done"))
        assert doubao.interrupts == 0

        # Customer speech during the audible tail -> still interrupts even
        # though the upstream generation is already closed.
        doubao.emit(451, {"results": [{"text": "等", "is_interim": True}]})
        await wait_until(lambda: doubao.interrupts == 1)
        await wait_until(lambda: find(websocket.sent, "response.cancelled"))

        websocket.feed(envelope("session.end", 3, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_residual_asr_after_barge_in_does_not_kill_fresh_reply(
    repository, settings
):
    """After a barge-in, residual ASR of the SAME customer utterance (and the
    short grace window right after) must not re-interrupt the model's fresh
    reply; a new utterance interrupts normally again."""
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway(settings, repository, doubao)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(
            envelope(
                "session.start",
                1,
                {"scenario_id": "product_intro", "input_mode": "audio"},
            )
        )
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Assistant speaking; customer barge-in (same ASR turn).
        doubao.emit(550, {"content": "Hello"})
        await wait_until(lambda: find(websocket.sent, "assistant.text.delta"))
        doubao.emit(451, {"results": [{"text": "等一下", "is_interim": True}]})
        await wait_until(lambda: doubao.interrupts == 1)

        # Turn finalizes; the model's fresh reply starts.
        doubao.emit(459, {})
        doubao.emit(550, {"content": "您好，请问有什么可以帮您？"})
        await wait_until(
            lambda:
            sum(
                1
                for event in websocket.sent
                if event.get("type") == "assistant.text.delta"
            )
            >= 2
        )

        # Residual final of the SAME utterance arrives late: suppressed by
        # turn-scoped protection, the fresh reply survives.
        doubao.emit(451, {"results": [{"text": "等一下我说完", "is_interim": False}]})
        await asyncio.sleep(0.1)
        assert doubao.interrupts == 1

        # Out of the grace window a NEW utterance interrupts normally again.
        await asyncio.sleep(0.85)
        doubao.emit(451, {"results": [{"text": "喂", "is_interim": True}]})
        await wait_until(lambda: doubao.interrupts == 2)

        websocket.feed(envelope("session.end", 3, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
