"""Pure encoder/decoder for the Volcengine realtime-dialogue binary framing.

This module performs no network I/O. Every bound is checked before parsing so a
malformed or oversized frame raises :class:`FrameProtocolError` with a fixed,
sanitized message that never embeds payload content.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from typing import Any

CLIENT_FULL_REQUEST = 0x1
CLIENT_AUDIO_ONLY_REQUEST = 0x2
SERVER_FULL_RESPONSE = 0x9
SERVER_AUDIO_RESPONSE = 0xB
SERVER_ERROR_RESPONSE = 0xF

FLAG_WITH_EVENT = 0x4
SERIALIZATION_RAW = 0x0
SERIALIZATION_JSON = 0x1
COMPRESSION_NONE = 0x0
COMPRESSION_GZIP = 0x1

# Client -> server events.
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200
EVENT_SAY_HELLO = 300
EVENT_END_ASR = 400
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CLIENT_INTERRUPT = 515

# Server -> client events.
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559
EVENT_DIALOG_ERROR = 599

# Events framed without a session-id field.
_CONNECTION_EVENTS = frozenset(
    {
        EVENT_START_CONNECTION,
        EVENT_FINISH_CONNECTION,
        EVENT_CONNECTION_STARTED,
        EVENT_CONNECTION_FAILED,
        EVENT_CONNECTION_FINISHED,
    }
)

# Upstream packet size for 20ms of 16kHz mono PCM16LE.
AUDIO_PACKET_BYTES = 640


class FrameProtocolError(ValueError):
    pass


def _gunzip_bounded(data: bytes, limit: int) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        output = decoder.decompress(data, limit + 1)
        if len(output) > limit or decoder.unconsumed_tail:
            raise FrameProtocolError("payload decompression exceeds limit")
        output += decoder.flush(max(limit + 1 - len(output), 0))
    except zlib.error as error:
        raise FrameProtocolError("payload compression is invalid") from error
    if len(output) > limit:
        raise FrameProtocolError("payload decompression exceeds limit")
    if not decoder.eof or decoder.unused_data:
        raise FrameProtocolError("payload compression is invalid")
    return output


@dataclass(frozen=True)
class DoubaoFrame:
    message_type: int
    flags: int
    event: int | None
    session_id: str | None
    payload: bytes | dict[str, Any]
    sequence: int | None = None
    error_code: int | None = None


def _header(message_type: int, serialization: int, compression: int) -> bytes:
    return bytes(
        (
            0x11,
            (message_type << 4) | FLAG_WITH_EVENT,
            (serialization << 4) | compression,
            0x00,
        )
    )


def encode_event(
    event: int,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    frame = bytearray(_header(CLIENT_FULL_REQUEST, SERIALIZATION_JSON, COMPRESSION_NONE))
    frame.extend(event.to_bytes(4, "big", signed=False))
    if event not in _CONNECTION_EVENTS:
        if not session_id:
            raise FrameProtocolError("session_id is required for session events")
        encoded_session = session_id.encode()
        frame.extend(len(encoded_session).to_bytes(4, "big"))
        frame.extend(encoded_session)
    frame.extend(len(body).to_bytes(4, "big"))
    frame.extend(body)
    return bytes(frame)


def encode_audio(session_id: str, pcm: bytes) -> bytes:
    if not session_id or not pcm or len(pcm) % 2:
        raise FrameProtocolError("audio requires a session and even PCM16 bytes")
    encoded_session = session_id.encode()
    frame = bytearray(
        _header(CLIENT_AUDIO_ONLY_REQUEST, SERIALIZATION_RAW, COMPRESSION_NONE)
    )
    frame.extend(EVENT_TASK_REQUEST.to_bytes(4, "big"))
    frame.extend(len(encoded_session).to_bytes(4, "big"))
    frame.extend(encoded_session)
    frame.extend(len(pcm).to_bytes(4, "big"))
    frame.extend(pcm)
    return bytes(frame)


def decode_frame(
    data: bytes,
    *,
    max_frame_bytes: int = 262_144,
    max_decompressed_bytes: int = 262_144,
) -> DoubaoFrame:
    if max_frame_bytes <= 0 or max_decompressed_bytes <= 0:
        raise ValueError("frame limits must be positive")
    if isinstance(data, str):
        raise FrameProtocolError("frame must be binary")
    if len(data) > max_frame_bytes:
        raise FrameProtocolError("frame exceeds limit")
    if len(data) < 8:
        raise FrameProtocolError("frame is truncated")
    version, header_words = data[0] >> 4, data[0] & 0x0F
    if version != 1 or header_words < 1:
        raise FrameProtocolError("frame header is unsupported")
    cursor = header_words * 4
    if cursor > len(data):
        raise FrameProtocolError("frame header is truncated")
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    sequence = None
    event = None
    error_code = None

    def take_int(*, signed: bool = False) -> int:
        nonlocal cursor
        if cursor + 4 > len(data):
            raise FrameProtocolError("frame optional field is truncated")
        value = int.from_bytes(data[cursor : cursor + 4], "big", signed=signed)
        cursor += 4
        return value

    if message_type == SERVER_ERROR_RESPONSE:
        error_code = take_int()
    else:
        sequence_flags = flags & 0x03
        if sequence_flags in {0x01, 0x02, 0x03}:
            sequence = take_int(signed=True)
    if flags & FLAG_WITH_EVENT:
        event = take_int()

    session_id = None
    if event is not None and event in _CONNECTION_EVENTS:
        # Some server variants echo an explicit session field (often the
        # connect id) even on connection events. Consume it only when the
        # remaining bytes then form an exact payload; otherwise treat the
        # frame as having no session field.
        if cursor + 8 <= len(data):
            session_size = int.from_bytes(data[cursor : cursor + 4], "big")
            if session_size >= 0 and cursor + 8 + session_size <= len(data):
                possible_size = int.from_bytes(
                    data[cursor + 4 + session_size : cursor + 8 + session_size],
                    "big",
                )
                if cursor + 8 + session_size + possible_size == len(data):
                    if session_size > 0:
                        try:
                            session_id = data[
                                cursor + 4 : cursor + 4 + session_size
                            ].decode("utf-8")
                        except UnicodeDecodeError:
                            session_id = None
                    cursor += 4 + session_size
    elif event is not None:
        session_size = take_int()
        if session_size <= 0 or cursor + session_size > len(data):
            raise FrameProtocolError("session id is invalid")
        try:
            session_id = data[cursor : cursor + session_size].decode("utf-8")
        except UnicodeDecodeError as error:
            raise FrameProtocolError("session id is invalid") from error
        cursor += session_size

    payload_size = take_int()
    if payload_size < 0 or cursor + payload_size != len(data):
        raise FrameProtocolError("payload size does not match frame")
    payload_bytes = data[cursor:]
    if compression == COMPRESSION_GZIP:
        payload_bytes = _gunzip_bounded(payload_bytes, max_decompressed_bytes)
    elif compression != COMPRESSION_NONE:
        raise FrameProtocolError("payload compression is unsupported")
    elif len(payload_bytes) > max_decompressed_bytes:
        raise FrameProtocolError("payload exceeds limit")
    payload: bytes | dict[str, Any]
    if serialization == SERIALIZATION_JSON:
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FrameProtocolError("payload JSON is invalid") from error
        if not isinstance(payload, dict):
            raise FrameProtocolError("payload JSON must be an object")
    elif serialization == SERIALIZATION_RAW:
        payload = payload_bytes
    else:
        raise FrameProtocolError("payload serialization is unsupported")
    return DoubaoFrame(
        message_type=message_type,
        flags=flags,
        event=event,
        session_id=session_id,
        payload=payload,
        sequence=sequence,
        error_code=error_code,
    )
