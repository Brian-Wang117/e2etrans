"""Authenticated Doubao realtime-dialogue client.

This is the only module that knows the vendor wire protocol. One internal
reader loop owns ``socket.recv()`` for the lifetime of the connection; it
resolves lifecycle acknowledgements (50/150/152/52) and enqueues every other
decoded frame into a bounded queue consumed by :meth:`receive`. Errors surface
as fixed-category :class:`DoubaoUpstreamError` values that never include
headers, raw frames, or payload text.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol

from app.config import RealtimeSettings
from app.outbound.persona import Persona
from app.realtime import doubao_protocol as protocol

logger = logging.getLogger(__name__)

_LEGACY_BOT_NAME = "EN Customer Care"


class DoubaoUpstreamError(RuntimeError):
    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class UpstreamSocketProtocol(Protocol):
    async def send(self, value: bytes) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


async def _default_connector(
    url: str,
    headers: dict[str, str],
    *,
    max_size: int,
) -> UpstreamSocketProtocol:
    from websockets.asyncio.client import connect

    return await connect(
        url,
        additional_headers=headers,
        ping_interval=None,
        max_size=max_size,
    )


class DoubaoRealtimeClient:
    def __init__(
        self,
        settings: RealtimeSettings,
        *,
        session_id: str,
        input_mode: str | None = None,
        persona: Persona | None = None,
        speaker: str | None = None,
        connector=None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.settings = settings
        self.session_id = session_id
        # Per-session override: phone-bridged calls stream continuously
        # ("audio") while the web simulator uses push-to-talk.
        self.input_mode = input_mode or settings.input_mode
        self.persona = persona
        # Per-session TTS voice override (gender-matched outbound calls).
        self.speaker = speaker or settings.speaker
        self.connection_id = str(uuid.uuid4())
        self._connector = connector or _default_connector
        self._socket: UpstreamSocketProtocol | None = None
        self._reader_task: asyncio.Task | None = None
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._acks: dict[int, asyncio.Future] = {}
        self._audio_buffer = bytearray()
        self._reader_error: DoubaoUpstreamError | None = None
        self._finished = False
        self.logid: str | None = None

    # -- connection lifecycle -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-App-ID": self.settings.app_id or "",
            "X-Api-Access-Key": self.settings.access_key or "",
            "X-Api-Resource-Id": self.settings.resource_id,
            "X-Api-App-Key": self.settings.app_key or "",
            "X-Api-Connect-Id": self.connection_id,
        }

    async def connect(self, *, send_greeting: bool = False) -> None:
        try:
            self._socket = await asyncio.wait_for(
                self._connector(
                    self.settings.ws_url,
                    self._headers(),
                    max_size=self.settings.max_upstream_frame_bytes,
                ),
                timeout=self.settings.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise DoubaoUpstreamError(
                "doubao connection timed out", category="connect"
            ) from error
        except Exception as error:
            raise DoubaoUpstreamError(
                "doubao connection failed", category="connect"
            ) from error
        logid = getattr(self._socket, "response", None)
        if logid is not None:
            headers = getattr(logid, "headers", None)
            if headers is not None:
                self.logid = headers.get("X-Tt-Logid")
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._handshake()
        if send_greeting:
            await self.say_hello("Hello, this is the English customer care team.")

    async def _handshake(self) -> None:
        await self._send_event_and_wait(
            protocol.encode_event(protocol.EVENT_START_CONNECTION, {}),
            ack_event=protocol.EVENT_CONNECTION_STARTED,
        )
        await self._send_event_and_wait(
            protocol.encode_event(
                protocol.EVENT_START_SESSION,
                self._start_session_payload(),
                session_id=self.session_id,
            ),
            ack_event=protocol.EVENT_SESSION_STARTED,
        )

    def _start_session_payload(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "strict_audit": False,
            "audit_response": "",
            "recv_timeout": max(int(self.settings.receive_timeout_seconds), 10),
            "input_mod": self.input_mode,
        }
        if self.settings.model:
            extra["model"] = self.settings.model
        return {
            "asr": {"extra": {"end_smooth_window_ms": 1500}},
            "tts": {
                "speaker": self.speaker,
                "extra": {},
                "audio_config": {
                    "channel": 1,
                    "format": "pcm_s16le",
                    "sample_rate": self.settings.output_sample_rate,
                },
            },
            "dialog": {
                "bot_name": self.persona.bot_name if self.persona else _LEGACY_BOT_NAME,
                "system_role": (
                    self.persona.system_role
                    if self.persona
                    else (
                        "You are a professional outbound customer-service agent. "
                        "Understand the user's Chinese speech, but always reply in "
                        "English. Be concise, ask one question at a time, and never "
                        "claim a real phone call."
                    )
                ),
                "speaking_style": (
                    self.persona.speaking_style
                    if self.persona
                    else "Natural, calm, warm, and professional."
                ),
                "location": {"city": "北京"},
                "extra": extra,
            },
        }

    async def _send_event_and_wait(self, frame: bytes, *, ack_event: int) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._acks[ack_event] = future
        try:
            await self._send(frame)
            async with asyncio.timeout(self.settings.receive_timeout_seconds):
                await future
        except asyncio.TimeoutError as error:
            raise DoubaoUpstreamError(
                "doubao handshake acknowledgement timed out", category="handshake"
            ) from error
        finally:
            self._acks.pop(ack_event, None)

    async def _send(self, frame: bytes) -> None:
        if self._socket is None:
            raise DoubaoUpstreamError("doubao connection is closed", category="closed")
        try:
            await self._socket.send(frame)
        except DoubaoUpstreamError:
            raise
        except Exception as error:
            raise DoubaoUpstreamError(
                "doubao upstream send failed", category="transport"
            ) from error

    # -- session commands -----------------------------------------------------

    async def say_hello(self, text: str) -> None:
        await self._send(
            protocol.encode_event(
                protocol.EVENT_SAY_HELLO, {"content": text}, session_id=self.session_id
            )
        )

    async def send_audio(self, audio: bytes) -> None:
        if not audio:
            return
        if len(audio) % 2:
            raise DoubaoUpstreamError("audio bytes must be even", category="protocol")
        self._audio_buffer.extend(audio)
        while len(self._audio_buffer) >= protocol.AUDIO_PACKET_BYTES:
            packet = bytes(self._audio_buffer[: protocol.AUDIO_PACKET_BYTES])
            del self._audio_buffer[: protocol.AUDIO_PACKET_BYTES]
            await self._send(protocol.encode_audio(self.session_id, packet))

    async def end_asr(self) -> None:
        partial = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        if partial:
            if len(partial) % 2:
                raise DoubaoUpstreamError(
                    "audio bytes must be even", category="protocol"
                )
            if len(partial) < protocol.AUDIO_PACKET_BYTES:
                partial = partial + bytes(
                    protocol.AUDIO_PACKET_BYTES - len(partial)
                )
            await self._send(protocol.encode_audio(self.session_id, partial))
        await self._send(
            protocol.encode_event(
                protocol.EVENT_END_ASR, {}, session_id=self.session_id
            )
        )

    async def send_text_query(self, text: str) -> None:
        if not text or not text.strip():
            raise DoubaoUpstreamError("text query is empty", category="protocol")
        await self._send(
            protocol.encode_event(
                protocol.EVENT_CHAT_TEXT_QUERY,
                {"content": text},
                session_id=self.session_id,
            )
        )

    async def interrupt(self) -> None:
        await self._send(
            protocol.encode_event(
                protocol.EVENT_CLIENT_INTERRUPT, {}, session_id=self.session_id
            )
        )

    async def receive(self) -> protocol.DoubaoFrame:
        item = await self._frames.get()
        if isinstance(item, DoubaoUpstreamError):
            raise item
        return item

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if self._socket is not None and self._reader_error is None:
                await self._send_event_and_wait(
                    protocol.encode_event(
                        protocol.EVENT_FINISH_SESSION, {}, session_id=self.session_id
                    ),
                    ack_event=protocol.EVENT_SESSION_FINISHED,
                )
                await self._send_event_and_wait(
                    protocol.encode_event(protocol.EVENT_FINISH_CONNECTION, {}),
                    ack_event=protocol.EVENT_CONNECTION_FINISHED,
                )
        finally:
            await self._teardown()

    async def close(self) -> None:
        self._finished = True
        await self._teardown()

    async def _teardown(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:
                pass
            self._socket = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self._fail_pending(DoubaoUpstreamError("doubao connection closed", category="closed"))
        self._release_consumers()

    # -- reader loop --------------------------------------------------------

    def _fail_pending(self, error: DoubaoUpstreamError) -> None:
        for future in list(self._acks.values()):
            if not future.done():
                future.set_exception(error)
        self._acks.clear()

    def _release_consumers(self) -> None:
        error = self._reader_error or DoubaoUpstreamError(
            "doubao connection closed", category="closed"
        )
        while True:
            try:
                self._frames.put_nowait(error)
                break
            except asyncio.QueueFull:
                try:
                    self._frames.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _reader_loop(self) -> None:
        try:
            while True:
                raw = await self._socket.recv()
                if isinstance(raw, str):
                    raise DoubaoUpstreamError(
                        "doubao upstream sent a text frame", category="protocol"
                    )
                frame = protocol.decode_frame(
                    raw,
                    max_frame_bytes=self.settings.max_upstream_frame_bytes,
                    max_decompressed_bytes=self.settings.max_decompressed_bytes,
                )
                if frame.message_type == protocol.SERVER_ERROR_RESPONSE:
                    raise DoubaoUpstreamError(
                        "doubao upstream returned an error", category="provider"
                    )
                ack = self._acks.get(frame.event) if frame.event is not None else None
                if ack is not None and not ack.done():
                    ack.set_result(None)
                    continue
                try:
                    self._frames.put_nowait(frame)
                except asyncio.QueueFull as error:
                    raise DoubaoUpstreamError(
                        "doubao upstream queue is full", category="overload"
                    ) from error
        except asyncio.CancelledError:
            raise
        except DoubaoUpstreamError as error:
            self._reader_error = error
            self._fail_pending(error)
            self._release_consumers()
        except Exception as error:
            upstream = DoubaoUpstreamError(
                "doubao upstream connection failed", category="transport"
            )
            upstream.__cause__ = error
            self._reader_error = upstream
            self._fail_pending(upstream)
            self._release_consumers()
