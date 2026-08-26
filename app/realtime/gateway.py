"""Per-browser-session realtime orchestration with bounded queues.

One gateway instance owns one call: the browser receive loop, the decoded
Doubao receive loop, and a single browser send loop run inside one TaskGroup.
Vendor frames are normalized into stable browser events; only final text is
translated and persisted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.websockets import WebSocketDisconnect

from app.config import OutboundSettings, RealtimeSettings
from app.outbound.adjudicator import AdjudicationError, OutboundAdjudicator
from app.batch.events import (
    EVENT_ACTIVITY,
    EVENT_CALL_FINISHED,
    CallEvent,
    CallEventBus,
)
from app.batch.import_parser import extract_gender
from app.outbound.call_director import (
    CallDirector,
    Interrupt,
    MuteInput,
    Notify,
    Report,
    Say,
    ScheduleHangup,
    StartAdjudication,
)
from app.outbound.persona import DEFAULT_OPENING_TEXT, build_persona
from app.outbound.script_library import BUILTIN_SCRIPTS, Script, ScriptMatcher
from app.realtime import browser_protocol
from app.realtime import doubao_protocol as protocol
from app.realtime.browser_protocol import BrowserProtocolError, server_event
from app.realtime.doubao import DoubaoUpstreamError
from app.realtime.persistence import PersistenceError, RealtimePersistence
from app.realtime.qwen import SubtitleTranslator
from app.realtime.state import RealtimeState
from app.storage import Repository

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES_PER_MESSAGE = 32_000  # one second of 16kHz PCM16 mono
OUTGOING_QUEUE_SIZE = 128
OUTBOUND_SCENARIO = "outbound_default"
ADJUDICATION_WAIT_SECONDS = 3.0

# Post-barge-in smoothing: residual ASR of the SAME customer utterance keeps
# arriving while the model already starts answering, and would immediately
# kill the fresh reply ("下一句说不完全"). Suppression is turn-scoped (the
# interrupting utterance never re-interrupts) plus a short time grace for the
# reply-start race right after the turn finalizes.
BARGE_IN_GRACE_SECONDS = 0.8

# When a hang-up is scheduled, it must wait for the audio already queued
# in the browser to play out first: upstream TTS synthesizes faster than
# realtime, so at TTS_ENDED (359) the caller still hears several seconds.
HANGUP_PLAYBACK_MARGIN = 0.5


class BrowserSocketProtocol(Protocol):
    async def receive_text(self) -> str: ...
    async def send_json(self, event: dict[str, object]) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class DoubaoClientProtocol(Protocol):
    async def connect(self, *, send_greeting: bool = False) -> None: ...
    async def say_hello(self, text: str) -> None: ...
    async def send_audio(self, audio: bytes) -> None: ...
    async def end_asr(self) -> None: ...
    async def send_text_query(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def receive(self) -> protocol.DoubaoFrame: ...
    async def finish(self) -> None: ...
    async def close(self) -> None: ...


DoubaoClientFactory = Callable[..., DoubaoClientProtocol]


class GatewayOverload(RuntimeError):
    pass


class _SessionStopped(Exception):
    pass


class _ProtocolClose(Exception):
    def __init__(self, code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class OutgoingItem:
    event: dict[str, object]
    audio_bytes: int = 0


_SHUTDOWN = OutgoingItem(event={}, audio_bytes=0)


async def _stop_when_done(awaitable) -> None:
    await awaitable
    raise _SessionStopped


class RealtimeGateway:
    def __init__(
        self,
        *,
        settings: RealtimeSettings,
        repository: Repository,
        doubao_factory: DoubaoClientFactory,
        translator: SubtitleTranslator | None,
        scenarios: Mapping[str, str],
        outbound_settings: OutboundSettings | None = None,
        adjudicator: OutboundAdjudicator | None = None,
        call_events: CallEventBus | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._doubao_factory = doubao_factory
        self._translator = translator
        self.scenarios = dict(scenarios)
        self._outbound_settings = outbound_settings or OutboundSettings()
        self._adjudicator = adjudicator
        self._call_events = call_events
        # Per-session attributes assigned in run().
        self._session_id: str | None = None
        self._scenario_id: str | None = None
        self._input_mode: str = "push_to_talk"
        self._state: RealtimeState | None = None
        self._doubao: DoubaoClientProtocol | None = None
        self._persistence: RealtimePersistence | None = None
        self._outgoing: asyncio.Queue[OutgoingItem] = asyncio.Queue(
            maxsize=OUTGOING_QUEUE_SIZE
        )
        self._outgoing_lock = asyncio.Lock()
        self._queued_audio_bytes = 0
        self._assistant_text: dict[int, str] = {}
        self._chunk_seq: dict[int, int] = {}
        self._last_asr_text = ""
        self._user_provisional_id: str | None = None
        self._user_turn_counter = 0
        self._awaiting_asr_end = False
        self._input_streaming = False
        # Audible window: upstream TTS streams faster than realtime playback,
        # so by the time 359 closes the generation the caller still hears
        # seconds of queued audio. Barge-in must stay armed until that audio
        # has actually played out (monotonic deadline, extended per chunk).
        self._audible_until = 0.0
        # Monotonic deadline until which ASR-triggered barge-in is suppressed
        # after an ASR barge-in (see BARGE_IN_GRACE_SECONDS).
        self._barge_grace_until = 0.0
        # Provisional turn id of the utterance that triggered the last ASR
        # barge-in; its residual ASR must never re-interrupt the fresh reply.
        self._barge_in_turn_id: str | None = None
        # Generation whose audio is (or was) queued in the browser; used to
        # name response.cancelled when the tail is interrupted after the
        # upstream generation already closed (359).
        self._playing_generation: int | None = None
        # [bargein] instrumentation: periodic RMS of inbound customer audio.
        self._input_probe = bytearray()
        # Outbound engine state (only populated for outbound sessions).
        self._director: CallDirector | None = None
        self._muted_input = False
        self._turn_intercepted = False
        self._pending_injections: list[str] = []
        self._turn_source_queue: list[str] = []
        self._silence_task: asyncio.Task | None = None
        self._hangup_task: asyncio.Task | None = None
        self._hangup_deadline = 0.0
        self._adjudication_task: asyncio.Task | None = None
        self._call_started_at: float | None = None
        self._stop_event = asyncio.Event()
        self._customer_id: int | None = None

    # -- outgoing queue ---------------------------------------------------------

    async def _notify(self, event_type: str, payload: dict[str, object], turn_id: str | None) -> None:
        await self._enqueue(self._make_event(event_type, payload, turn_id=turn_id))

    def _make_event(
        self, event_type: str, payload: dict[str, object], *, turn_id: str | None = None
    ) -> dict[str, object]:
        return server_event(
            event_type,
            session_id=self._session_id or "",
            seq=self._state.next_server_seq(),
            payload=payload,
            turn_id=turn_id,
        )

    async def _enqueue(self, event: dict[str, object], *, audio_bytes: int = 0) -> None:
        async with self._outgoing_lock:
            if (
                self._outgoing.full()
                or self._queued_audio_bytes + audio_bytes > self.settings.max_buffer_bytes
            ):
                raise GatewayOverload("browser output queue is full")
            self._outgoing.put_nowait(OutgoingItem(event, audio_bytes))
            self._queued_audio_bytes += audio_bytes

    # -- main entry point ---------------------------------------------------------

    async def run(self, websocket: BrowserSocketProtocol) -> None:
        close_code = 1000
        clean_hangup = False
        try:
            await self._setup(websocket)
            try:
                async with asyncio.timeout(self.settings.max_session_seconds):
                    async with asyncio.TaskGroup() as group:
                        group.create_task(
                            _stop_when_done(self._browser_receive_loop(websocket))
                        )
                        group.create_task(
                            _stop_when_done(self._upstream_receive_loop())
                        )
                        group.create_task(
                            _stop_when_done(self._browser_send_loop(websocket))
                        )
            except* _SessionStopped:
                clean_hangup = True
            except* WebSocketDisconnect:
                clean_hangup = False
            except* _ProtocolClose as group:
                failure = group.exceptions[0]
                close_code = failure.code
                await self._try_send_error(failure.error_code, failure.message, retryable=False)
            except* GatewayOverload:
                close_code = 1013
                await self._try_send_error(
                    "overloaded", "the session is too busy", retryable=True
                )
            except* asyncio.TimeoutError:
                close_code = 1011
                await self._try_send_error(
                    "session_timeout", "the session reached its time limit", retryable=False
                )
            except* DoubaoUpstreamError:
                close_code = 1011
                await self._try_send_error(
                    "upstream_error", "the voice service failed", retryable=True
                )
            except* Exception:
                logger.exception("realtime session failed unexpectedly")
                close_code = 1011
                await self._try_send_error(
                    "internal_error", "the session failed", retryable=True
                )
        except WebSocketDisconnect:
            clean_hangup = False
        except _ProtocolClose as failure:
            close_code = failure.code
            await self._try_send_error(failure.error_code, failure.message, retryable=False)
        except DoubaoUpstreamError:
            close_code = 1011
            await self._try_send_error(
                "upstream_error", "the voice service failed", retryable=True
            )
        except Exception:
            logger.exception("realtime session setup failed")
            close_code = 1011
            await self._try_send_error(
                "internal_error", "the session failed", retryable=True
            )
        finally:
            await self._finalize(websocket, clean_hangup=clean_hangup, close_code=close_code)

    async def _try_send_error(self, code: str, message: str, *, retryable: bool) -> None:
        if self._state is None:
            return
        try:
            await self._enqueue(
                server_event(
                    "error",
                    session_id=self._session_id or "",
                    seq=self._state.next_server_seq(),
                    payload={"code": code, "message": message, "retryable": retryable},
                )
            )
        except GatewayOverload:
            pass

    # -- setup ------------------------------------------------------------------

    async def _setup(self, websocket: BrowserSocketProtocol) -> None:
        raw = await websocket.receive_text()
        try:
            event = browser_protocol.parse_client_event(
                raw,
                max_message_bytes=self.settings.max_message_bytes,
                max_audio_bytes=MAX_AUDIO_BYTES_PER_MESSAGE,
                allowed_scenarios=frozenset(self.scenarios),
            )
        except BrowserProtocolError as error:
            code = 1009 if error.oversized else 1008
            raise _ProtocolClose(code, "protocol_error", "the request is invalid") from error
        if event.type != "session.start":
            raise _ProtocolClose(1008, "protocol_error", "session.start is required")
        self._state = RealtimeState(session_id="")
        if not self._state.accept_client_seq(event.seq):
            raise _ProtocolClose(1008, "protocol_error", "seq is invalid")
        self._scenario_id = str(event.payload["scenario_id"])
        # Phone-bridged sessions stream continuously (server-side VAD); the
        # web simulator keeps explicit push-to-talk turn control. Outbound
        # calls always stream (requirement 4: input_mode=audio).
        self._input_mode = str(event.payload.get("input_mode", "push_to_talk"))
        persona = None
        opening_text: str | None = None
        speaker: str | None = None
        if self._scenario_id == OUTBOUND_SCENARIO:
            self._input_mode = "audio"
            self._customer_id = event.payload.get("customer_id")
            opening_text, persona, self._director = await self._prepare_outbound(
                event.payload
            )
            explicit_speaker = str(event.payload.get("speaker") or "").strip()
            if explicit_speaker:
                # Workbench-selected clone voice wins over gender crossing.
                speaker = explicit_speaker
                if persona is not None:
                    persona = persona.with_clone_guard()
            else:
                gender = str(event.payload.get("gender") or "").strip()
                if not gender and isinstance(self._customer_id, int):
                    gender = await self._lookup_customer_gender(self._customer_id)
                speaker = self._speaker_for_gender(gender)
        session = await asyncio.to_thread(
            self.repository.create_session, self._scenario_id, "doubao"
        )
        self._session_id = session["id"]
        self._state = RealtimeState(session_id=self._session_id)
        self._state.accept_client_seq(event.seq)
        self._persistence = RealtimePersistence(
            session_id=self._session_id,
            repository=self.repository,
            translator=self._translator,
            translation_model=self.settings.subtitle_model,
            notify=self._notify,
        )
        self._doubao = self._doubao_factory(self._session_id, self._input_mode, persona, speaker)
        await self._doubao.connect(send_greeting=False)
        await self._enqueue(
            self._make_event(
                "session.ready",
                {
                    "scenario_id": self._scenario_id,
                    "provider": "doubao",
                    "created_at": session["created_at"],
                    "subtitles": "enabled" if self._persistence.subtitles_enabled else "disabled",
                },
            )
        )
        if opening_text is not None:
            self._call_started_at = time.time()
            self._director.start(opening_text)
            self._pending_injections.append(opening_text)
            self._turn_source_queue.append("opening")
            await self._doubao.say_hello(opening_text)
            self._reset_silence_guard()
        else:
            await self._doubao.say_hello(self.scenarios[self._scenario_id])

    async def _prepare_outbound(self, payload: Mapping[str, object]):
        """Resolve opening/persona/director for an outbound session."""
        outbound = self._outbound_settings
        opening = str(payload.get("opening_text") or "").strip()
        opening = opening or outbound.opening_text or DEFAULT_OPENING_TEXT
        background = str(payload.get("business_background") or "").strip()
        background = background or outbound.business_background
        # Campaign-template persona overrides; empty payload values fall back
        # to server configuration (same pattern as business_background).
        bot_name = str(payload.get("bot_name") or "").strip() or outbound.bot_name
        speaking_style = (
            str(payload.get("speaking_style") or "").strip()
            or outbound.speaking_style
        )
        persona = build_persona(
            business_background=background,
            bot_name=bot_name,
            speaking_style=speaking_style,
        )
        raw_template_id = payload.get("template_id")
        template_id = (
            raw_template_id
            if isinstance(raw_template_id, int)
            and not isinstance(raw_template_id, bool)
            else None
        )
        scripts = await self._load_scripts(template_id)
        director = CallDirector(
            matcher=ScriptMatcher(scripts),
            max_turns=outbound.max_turns,
            silence_seconds=outbound.silence_seconds,
        )
        return opening, persona, director

    async def _lookup_customer_gender(self, customer_id: int) -> str:
        """Fallback when the bridge did not forward the imported gender."""
        try:
            customer = await asyncio.to_thread(
                self.repository.get_customer, customer_id
            )
        except Exception:
            return ""
        if not customer:
            return ""
        return extract_gender(customer.get("raw_data") or {})

    def _speaker_for_gender(self, gender: str) -> str | None:
        """Cross-gender voice: male customers hear the female AI voice and
        female customers hear the male one. ``None`` keeps the default voice."""
        outbound = self._outbound_settings
        if gender == "male":
            return outbound.tts_speaker_female or None
        if gender == "female":
            return outbound.tts_speaker_male or None
        return None

    async def _load_scripts(self, template_id: int | None = None) -> tuple[Script, ...]:
        """Built-in compliance scripts always apply; a campaign template adds
        its own scripts on top (priority + trigger length break ties)."""
        try:
            scripts = await asyncio.to_thread(
                self.repository.list_scripts, "builtin"
            )
            if not scripts:
                scripts = list(BUILTIN_SCRIPTS)
        except Exception:
            logger.warning("script library load failed; using in-memory builtins")
            scripts = list(BUILTIN_SCRIPTS)
        if template_id is not None:
            try:
                template_scripts = await asyncio.to_thread(
                    self.repository.list_template_scripts, template_id
                )
            except Exception:
                logger.warning(
                    "template %s script load failed; builtins only", template_id
                )
                template_scripts = []
            scripts = [*scripts, *template_scripts]
        return tuple(scripts)

    # -- browser receive ----------------------------------------------------------

    async def _browser_receive_loop(self, websocket: BrowserSocketProtocol) -> None:
        while True:
            raw = await self._receive_or_stop(websocket)
            try:
                event = browser_protocol.parse_client_event(
                    raw,
                    max_message_bytes=self.settings.max_message_bytes,
                    max_audio_bytes=MAX_AUDIO_BYTES_PER_MESSAGE,
                    allowed_scenarios=frozenset(self.scenarios),
                )
            except BrowserProtocolError as error:
                code = 1009 if error.oversized else 1008
                raise _ProtocolClose(code, "protocol_error", "the message is invalid") from error
            if not self._state.accept_client_seq(event.seq):
                continue  # stale or duplicate client event
            await self._handle_client_event(event)

    async def _receive_or_stop(self, websocket: BrowserSocketProtocol) -> str:
        """Receive one client message, or stop cleanly when a scheduled
        hang-up (script end-call / silence farewell) fires."""
        if self._director is None:
            return await websocket.receive_text()
        receive_task = asyncio.create_task(websocket.receive_text())
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if stop_task in done:
            if receive_task in done:
                try:
                    receive_task.result()
                except Exception:
                    pass
            raise _SessionStopped
        return receive_task.result()

    async def _handle_client_event(self, event) -> None:
        if event.type == "input_audio.append":
            assert event.audio is not None
            if self._muted_input:
                return  # call is wrapping up; drop further customer audio
            if not self._input_streaming:
                self._input_streaming = True
                if self._awaiting_asr_end:
                    # A committed turn never received ASREnded; re-arm so the
                    # conversation keeps moving instead of stalling.
                    self._awaiting_asr_end = False
                    self._state.complete_input_turn()
                # Push-to-talk only: the mic press itself signals user speech.
                # Streaming ("audio") input flows continuously, so interrupting
                # on the first chunk would fire on noise; there the barge-in
                # trigger is ASR text (see _handle_upstream).
                if self._input_mode != "audio" and self._state.active_generation is not None:
                    await self._interrupt_response(send_event=False)
            await self._doubao.send_audio(event.audio)
            self._probe_input_energy(event.audio)
        elif event.type == "input_audio.commit":
            if self._muted_input:
                return
            self._input_streaming = False
            self._awaiting_asr_end = True
            await self._doubao.end_asr()
        elif event.type == "user.text.submit":
            text = str(event.payload["text"]).strip()
            if self._state.active_generation is not None:
                await self._interrupt_response(send_event=False)
            self._state.complete_input_turn()
            await self._persistence.finalize_user(
                text=text, input_kind="text", source="text"
            )
            if self._director is not None:
                self._publish_activity()
                actions = self._director.observe_user_final(text)
                if actions:
                    await self._execute_director_actions(actions)
                    self._reset_silence_guard()
                    return  # fixed script answered; do not forward to the model
            await self._doubao.send_text_query(text)
        elif event.type == "response.cancel":
            response_id = str(event.payload["response_id"])
            active = self._state.active_generation
            if (
                active is not None
                and self._state.response_id(active) == response_id
            ):
                await self._interrupt_response(send_event=True)
        elif event.type == "session.end":
            raise _SessionStopped
        elif event.type == "ping":
            await self._enqueue(
                self._make_event("pong", {"client_event_id": event.event_id})
            )
        elif event.type == "session.start":
            raise _ProtocolClose(1008, "protocol_error", "session already started")

    async def _interrupt_response(
        self, *, send_event: bool, arm_grace: bool = False
    ) -> None:
        generation = self._state.interrupt_for_new_input()
        logger.info(
            "[bargein] interrupt fired (generation=%s send_event=%s grace=%ss)",
            generation,
            send_event,
            BARGE_IN_GRACE_SECONDS if arm_grace else 0,
        )
        await self._doubao.interrupt()
        self._audible_until = 0.0
        if arm_grace:
            self._barge_grace_until = time.monotonic() + BARGE_IN_GRACE_SECONDS
        target = generation if generation is not None else self._playing_generation
        if generation is not None:
            await self._close_assistant_generation(generation, interrupted=True)
        if send_event:
            # The flush must also fire for playback-tail interrupts where the
            # upstream generation already closed — otherwise the browser
            # keeps playing the queued audio.
            await self._enqueue(
                self._make_event(
                    "response.cancelled",
                    {
                        "response_id": (
                            self._state.response_id(target)
                            if target is not None
                            else None
                        )
                    },
                )
            )
        if target is not None:
            self._playing_generation = None

    # -- upstream receive -----------------------------------------------------------

    async def _upstream_receive_loop(self) -> None:
        while True:
            frame = await self._doubao.receive()
            await self._handle_upstream(frame)

    async def _handle_upstream(self, frame: protocol.DoubaoFrame) -> None:
        event = frame.event
        payload = frame.payload
        if event == protocol.EVENT_ASR_RESPONSE:  # 451
            text, is_interim = self._parse_asr_payload(payload)
            if not text:
                return
            self._last_asr_text = text
            if self._user_provisional_id is None:
                self._user_turn_counter += 1
                self._user_provisional_id = f"turn-user-{self._user_turn_counter}"
                self._turn_intercepted = False
            if self._assistant_audible() and not self._muted_input:
                # Barge-in: any recognized user speech (interim or final)
                # while the assistant is still audible. Interim fires as
                # soon as the customer starts speaking; waiting for final
                # would keep the AI talking until the customer pauses.
                # "Audible" covers the playback tail too: upstream TTS
                # streams faster than realtime, so the generation is often
                # already closed (359) while the caller still hears audio.
                # Streaming clients never send input_audio.commit, so ASR
                # text is the only reliable interrupt trigger there.
                # send_event=True tells the browser to flush queued playback.
                # While closing (_muted_input) in-flight residual ASR of
                # audio uploaded before the mute must not kill the farewell.
                if (
                    self._user_provisional_id is not None
                    and self._user_provisional_id == self._barge_in_turn_id
                ) or time.monotonic() < self._barge_grace_until:
                    # Residual ASR of the very utterance that triggered the
                    # previous barge-in (turn-scoped), or the short grace
                    # right after it: re-interrupting here would kill the
                    # model's fresh reply right after it starts.
                    logger.info(
                        "[bargein] suppressed (same_turn=%s grace=%s) interim=%s text=%r",
                        self._user_provisional_id == self._barge_in_turn_id,
                        time.monotonic() < self._barge_grace_until,
                        is_interim,
                        text[:20],
                    )
                else:
                    self._barge_in_turn_id = self._user_provisional_id
                    await self._interrupt_response(
                        send_event=True, arm_grace=True
                    )
            logger.info(
                "[bargein] ASR interim=%s text=%r active_gen=%s audible_left=%.2fs",
                is_interim,
                text[:20],
                self._state.active_generation,
                max(0.0, self._audible_until - time.monotonic()),
            )
            event_type = "asr.partial" if is_interim else "asr.final"
            await self._enqueue(
                self._make_event(event_type, {"text": text}, turn_id=self._user_provisional_id)
            )
            if (
                self._director is not None
                and not is_interim
                and not self._turn_intercepted
            ):
                self._publish_activity()
                actions = self._director.observe_user_final(text)
                if actions:
                    self._turn_intercepted = True
                    await self._execute_director_actions(actions)
                self._reset_silence_guard()
        elif event == protocol.EVENT_ASR_ENDED:  # 459
            await self._finalize_user_turn()
        elif event == protocol.EVENT_CHAT_RESPONSE:  # 550
            generation = self._state.open_response_boundary(event=550)
            if generation is None:
                return
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, str) or not content:
                return
            accumulated = self._assistant_text.get(generation, "")
            if content.startswith(accumulated):
                delta, accumulated = content[len(accumulated):], content
            else:
                delta, accumulated = content, accumulated + content
            self._assistant_text[generation] = accumulated
            if delta:
                await self._enqueue(
                    self._make_event(
                        "assistant.text.delta",
                        {
                            "response_id": self._state.response_id(generation),
                            "delta": delta,
                        },
                    )
                )
        elif event == protocol.EVENT_CHAT_ENDED:  # 559
            generation = self._state.active_generation
            if generation is not None:
                self._state.mark_text_done(generation)
                text = self._assistant_text.get(generation, "")
                await self._enqueue(
                    self._make_event(
                        "assistant.text.done",
                        {
                            "response_id": self._state.response_id(generation),
                            "text": text,
                        },
                    )
                )
                if self._director is not None:
                    self._reset_silence_guard()
                    self._publish_activity()
                    if not self._consume_injection(text):
                        actions = self._director.observe_assistant_done(text)
                        await self._execute_director_actions(actions)
        elif event == protocol.EVENT_TTS_SENTENCE_START:  # 350
            self._state.open_response_boundary(event=350)
        elif event == protocol.EVENT_TTS_RESPONSE:  # 352
            await self._handle_audio_chunk(frame)
        elif event == protocol.EVENT_TTS_ENDED:  # 359
            generation = self._state.active_generation
            if generation is None:
                return
            self._state.close_response(generation=generation, event=359)
            await self._close_assistant_generation(generation, interrupted=False)
            logger.info(
                "[bargein] TTS_ENDED gen=%s playback_tail=%.2fs",
                generation,
                max(0.0, self._audible_until - time.monotonic()),
            )
            await self._enqueue(
                self._make_event(
                    "assistant.audio.done",
                    {"response_id": self._state.response_id(generation)},
                )
            )
            if self._director is not None:
                actions = self._director.on_goodbye_played()
                await self._execute_director_actions(actions)
        elif event == protocol.EVENT_DIALOG_ERROR:  # 599
            raise DoubaoUpstreamError("doubao dialog failed", category="provider")
        # Other lifecycle events (50/150/152/52) are consumed by the client.

    @staticmethod
    def _parse_asr_payload(payload: bytes | dict[str, Any]) -> tuple[str, bool]:
        if not isinstance(payload, dict):
            return "", False
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return "", False
        last = results[-1]
        if not isinstance(last, dict):
            return "", False
        text = last.get("text")
        if not isinstance(text, str):
            return "", False
        return text, bool(last.get("is_interim", False))

    def _assistant_audible(self) -> bool:
        """True while the caller can still hear the assistant: either a live
        generation or queued playback that has not finished playing out."""
        return (
            self._state.active_generation is not None
            or time.monotonic() < self._audible_until
        )

    # [bargein] instrumentation: log inbound customer-audio RMS once per
    # second so we can verify speech actually reaches the server during AI
    # playback.
    def _probe_input_energy(self, pcm: bytes) -> None:
        self._input_probe.extend(pcm)
        window = self.settings.input_sample_rate * 2  # one second of PCM16
        if len(self._input_probe) < window:
            return
        samples = struct.iter_unpack("<h", bytes(self._input_probe[:window]))
        values = [sample for (sample,) in samples]
        rms = (sum(v * v for v in values) / max(1, len(values))) ** 0.5
        logger.info(
            "[bargein] input_rms=%.0f audible_left=%.2fs",
            rms,
            max(0.0, self._audible_until - time.monotonic()),
        )
        del self._input_probe[:window]

    async def _handle_audio_chunk(self, frame: protocol.DoubaoFrame) -> None:
        pcm = frame.payload
        if not isinstance(pcm, bytes) or not pcm:
            return
        generation = self._state.active_generation
        if generation is None or self._state.is_cancelled(generation):
            return
        chunk_seq = self._chunk_seq.get(generation, 0) + 1
        if not self._state.accept_audio(generation=generation, chunk_seq=chunk_seq):
            return
        self._chunk_seq[generation] = chunk_seq
        self._playing_generation = generation
        # Extend the audible deadline: this chunk will play out after every
        # already-queued one (browser schedules back-to-back).
        chunk_seconds = len(pcm) / (self.settings.output_sample_rate * 2)
        now = time.monotonic()
        self._audible_until = max(now, self._audible_until) + chunk_seconds
        await self._enqueue(
            self._make_event(
                "assistant.audio.chunk",
                {
                    "response_id": self._state.response_id(generation),
                    "chunk_seq": chunk_seq,
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": self.settings.output_sample_rate,
                    "channels": 1,
                    "audio_b64": base64.b64encode(pcm).decode(),
                },
            ),
            audio_bytes=len(pcm),
        )

    async def _finalize_user_turn(self) -> None:
        self._awaiting_asr_end = False
        # Re-arm the turn-start interrupt gate for the next speech burst.
        self._input_streaming = False
        text = self._last_asr_text
        self._last_asr_text = ""
        self._state.complete_input_turn()
        if not text.strip():
            self._user_provisional_id = None
            return
        await self._persistence.finalize_user(
            text=text, input_kind="audio", source="asr"
        )
        self._user_provisional_id = None

    async def _close_assistant_generation(self, generation: int, *, interrupted: bool) -> None:
        text = self._assistant_text.pop(generation, "")
        self._chunk_seq.pop(generation, None)
        source = self._turn_source_queue.pop(0) if self._turn_source_queue else ""
        if text.strip():
            await self._persistence.complete_assistant(
                text=text,
                interrupted=interrupted,
                source=source,
            )

    # -- outbound engine ---------------------------------------------------------

    def _consume_injection(self, text: str) -> bool:
        """Drop director observation for assistant turns that replay text we
        injected ourselves (opening / fixed script / farewell)."""
        if not self._pending_injections or not text:
            return False
        expected = self._pending_injections[0]
        if text == expected or expected in text or text in expected:
            self._pending_injections.pop(0)
            return True
        return False

    def _reset_silence_guard(self) -> None:
        if self._director is None:
            return
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = asyncio.create_task(self._silence_guard())

    async def _silence_guard(self) -> None:
        await asyncio.sleep(self._director.silence_seconds)
        actions = self._director.on_silence_timeout()
        await self._execute_director_actions(actions)

    def _schedule_hangup(self, delay_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, delay_seconds)
        if self._hangup_task is not None and not self._hangup_task.done():
            if deadline <= self._hangup_deadline:
                # Never hang up EARLIER than already scheduled: the 359-based
                # goodbye timer (short margin) must not truncate the longer
                # estimate timer set when the farewell was injected.
                return
            self._hangup_task.cancel()
        self._hangup_deadline = deadline
        self._hangup_task = asyncio.create_task(
            self._hangup_after(deadline - time.monotonic())
        )

    async def _hangup_after(self, delay_seconds: float) -> None:
        await asyncio.sleep(max(0.0, delay_seconds))
        self._stop_event.set()

    async def _run_adjudication(self) -> None:
        dialogue = self._director.build_dialogue()
        result = None
        if self._adjudicator is not None:
            try:
                result = await self._adjudicator.adjudicate(dialogue)
            except Exception:
                logger.warning("outbound adjudication failed; using fallback verdict")
        actions = self._director.apply_adjudication(result)
        await self._execute_director_actions(actions)

    async def _execute_director_actions(self, actions) -> None:
        for action in actions:
            if isinstance(action, Interrupt):
                await self._interrupt_response(send_event=False)
            elif isinstance(action, Say):
                self._pending_injections.append(action.text)
                self._turn_source_queue.append(action.source)
                try:
                    await self._doubao.say_hello(action.text)
                except DoubaoUpstreamError:
                    # Degradation path (spec 6): let the model speak the text.
                    logger.warning("say_hello injection failed; using text query")
                    try:
                        await self._doubao.send_text_query(action.text)
                    except DoubaoUpstreamError:
                        logger.warning("text-query fallback also failed")
            elif isinstance(action, MuteInput):
                self._muted_input = True
                try:
                    await self._doubao.end_asr()
                except DoubaoUpstreamError:
                    pass
            elif isinstance(action, ScheduleHangup):
                # Wait for the queued playback tail too: TTS streams faster
                # than realtime, otherwise the farewell is cut mid-sentence.
                playback_tail = max(0.0, self._audible_until - time.monotonic())
                self._schedule_hangup(
                    max(action.delay_seconds, playback_tail + HANGUP_PLAYBACK_MARGIN)
                )
            elif isinstance(action, StartAdjudication):
                if self._adjudication_task is None or self._adjudication_task.done():
                    self._adjudication_task = asyncio.create_task(
                        self._run_adjudication()
                    )
            elif isinstance(action, Report):
                await self._record_call_result(action)
            elif isinstance(action, Notify):
                try:
                    await self._notify(action.event, action.payload, None)
                except GatewayOverload:
                    pass

    async def _record_call_result(self, action: Report) -> None:
        duration = None
        if self._call_started_at is not None:
            duration = round(time.time() - self._call_started_at, 1)
        for attempt in (1, 2):  # spec 6: retry once, never block hang-up
            try:
                await asyncio.to_thread(
                    self.repository.record_call_result,
                    self._session_id,
                    status=action.status,
                    result=action.result,
                    reason=action.reason,
                    end_reason=action.end_reason,
                    duration_seconds=duration,
                    customer_id=self._customer_id,
                )
                self._publish_call_finished(action, duration)
                return
            except Exception:
                logger.warning("call result report failed (attempt %s)", attempt)

    def _publish_activity(self) -> None:
        """Tell the batch scheduler there is fresh dialogue (timer reset)."""
        if self._call_events is None or self._session_id is None:
            return
        self._call_events.publish(
            CallEvent(
                kind=EVENT_ACTIVITY,
                session_id=self._session_id,
                customer_id=self._customer_id,
            )
        )

    def _publish_call_finished(self, action: Report, duration: float | None) -> None:
        if self._call_events is None or self._session_id is None:
            return
        self._call_events.publish(
            CallEvent(
                kind=EVENT_CALL_FINISHED,
                session_id=self._session_id,
                customer_id=self._customer_id,
                payload={
                    "status": action.status,
                    "result": action.result,
                    "reason": action.reason,
                    "duration_seconds": duration,
                },
            )
        )

    async def _finalize_outbound(self, *, close_code: int) -> None:
        try:
            if self._silence_task is not None:
                self._silence_task.cancel()
            if self._hangup_task is not None:
                self._hangup_task.cancel()
            if self._adjudication_task is not None and not self._adjudication_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._adjudication_task),
                        ADJUDICATION_WAIT_SECONDS,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    self._adjudication_task.cancel()
            if self._director.adjudication_pending:
                await self._execute_director_actions(
                    self._director.apply_adjudication(None)
                )
            abnormal = close_code == 1011
            await self._execute_director_actions(
                self._director.finish(abnormal=abnormal)
            )
        except Exception:
            logger.exception("outbound teardown failed")

    # -- browser send ---------------------------------------------------------------

    async def _browser_send_loop(self, websocket: BrowserSocketProtocol) -> None:
        while True:
            item = await self._outgoing.get()
            if item is _SHUTDOWN:
                self._outgoing.task_done()
                return
            try:
                await websocket.send_json(item.event)
            finally:
                async with self._outgoing_lock:
                    self._queued_audio_bytes -= item.audio_bytes
                self._outgoing.task_done()

    # -- teardown -----------------------------------------------------------------

    async def _finalize(
        self,
        websocket: BrowserSocketProtocol,
        *,
        clean_hangup: bool,
        close_code: int,
    ) -> None:
        if self._director is not None:
            await self._finalize_outbound(close_code=close_code)
        if self._doubao is not None:
            try:
                if clean_hangup:
                    async with asyncio.timeout(self.settings.receive_timeout_seconds * 2):
                        await self._doubao.finish()
                else:
                    await self._doubao.close()
            except Exception:
                try:
                    await self._doubao.close()
                except Exception:
                    pass
        if self._persistence is not None:
            try:
                await self._persistence.drain(timeout_seconds=5)
            except PersistenceError:
                logger.warning("persistence drain failed; aborting pending jobs")
            await self._persistence.abort()
        if self._session_id is not None:
            await asyncio.to_thread(self.repository.end_session, self._session_id)
        if clean_hangup and self._state is not None:
            try:
                await self._enqueue(
                    self._make_event("session.ended", {"session_id": self._session_id})
                )
            except GatewayOverload:
                pass
        try:
            while not self._outgoing.empty():
                item = self._outgoing.get_nowait()
                if item is _SHUTDOWN:
                    continue
                try:
                    await websocket.send_json(item.event)
                except Exception:
                    break
        except Exception:
            pass
        try:
            await websocket.close(code=close_code if close_code else 1000)
        except Exception:
            pass
