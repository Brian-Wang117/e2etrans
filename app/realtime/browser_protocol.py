"""Validated browser WebSocket protocol (JSON + base64 audio).

The parser is pure and stateless: it validates one raw text message against
byte/message/audio bounds and the fixed envelope schema. Sequence monotonicity
across messages belongs to :mod:`app.realtime.state`.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass

MAX_TEXT_CHARS = 4000
MAX_OPENING_CHARS = 200
MAX_BACKGROUND_CHARS = 500
MAX_BOT_NAME_CHARS = 20
MAX_SPEAKING_STYLE_CHARS = 200

_CLIENT_EVENT_TYPES = frozenset(
    {
        "session.start",
        "input_audio.append",
        "input_audio.commit",
        "user.text.submit",
        "response.cancel",
        "session.end",
        "ping",
    }
)


class BrowserProtocolError(ValueError):
    def __init__(self, message: str, *, oversized: bool = False) -> None:
        super().__init__(message)
        self.oversized = oversized


@dataclass(frozen=True)
class ClientEvent:
    type: str
    event_id: str
    seq: int
    payload: dict[str, object]
    audio: bytes | None = None


def parse_client_event(
    raw_message: str,
    *,
    max_message_bytes: int,
    max_audio_bytes: int,
    allowed_scenarios: frozenset[str],
) -> ClientEvent:
    if not isinstance(raw_message, str):
        raise BrowserProtocolError("message must be text")
    if len(raw_message.encode("utf-8")) > max_message_bytes:
        raise BrowserProtocolError("message exceeds size bound", oversized=True)
    try:
        document = json.loads(raw_message)
    except json.JSONDecodeError as error:
        raise BrowserProtocolError("message JSON is invalid") from error
    if not isinstance(document, dict):
        raise BrowserProtocolError("message must be a JSON object")

    event_type = document.get("type")
    if event_type not in _CLIENT_EVENT_TYPES:
        raise BrowserProtocolError("event type is unknown")
    if document.get("v") != 1:
        raise BrowserProtocolError("protocol version is unsupported")
    event_id = document.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        raise BrowserProtocolError("event_id is invalid")
    seq = document.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        raise BrowserProtocolError("seq must be a positive integer")
    for key in ("session_id", "turn_id"):
        value = document.get(key)
        if value is not None and not isinstance(value, str):
            raise BrowserProtocolError(f"{key} is invalid")
    if not isinstance(document.get("ts_ms"), int):
        raise BrowserProtocolError("ts_ms is invalid")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise BrowserProtocolError("payload must be an object")

    audio: bytes | None = None
    if event_type == "session.start":
        _require_keys(
            payload,
            {"scenario_id"},
            optional={
                "input_mode",
                "opening_text",
                "business_background",
                "customer_id",
                "gender",
                "speaker",
                "bot_name",
                "speaking_style",
                "template_id",
            },
        )
        scenario = payload["scenario_id"]
        if not isinstance(scenario, str) or scenario not in allowed_scenarios:
            raise BrowserProtocolError("scenario_id is not allowed")
        input_mode = payload.get("input_mode", "push_to_talk")
        if not isinstance(input_mode, str) or input_mode not in {"push_to_talk", "audio"}:
            raise BrowserProtocolError("input_mode is not allowed")
        opening = payload.get("opening_text")
        if opening is not None and (
            not isinstance(opening, str) or len(opening) > MAX_OPENING_CHARS
        ):
            raise BrowserProtocolError("opening_text is invalid")
        background = payload.get("business_background")
        if background is not None and (
            not isinstance(background, str) or len(background) > MAX_BACKGROUND_CHARS
        ):
            raise BrowserProtocolError("business_background is invalid")
        customer_id = payload.get("customer_id")
        if customer_id is not None and (
            isinstance(customer_id, bool) or not isinstance(customer_id, int)
        ):
            raise BrowserProtocolError("customer_id is invalid")
        gender = payload.get("gender")
        if gender is not None and (
            not isinstance(gender, str) or gender not in {"", "male", "female"}
        ):
            raise BrowserProtocolError("gender is invalid")
        speaker = payload.get("speaker")
        # Explicit clone voice (workbench selector) wins over gender crossing;
        # only cloned S_ ids are accepted through this channel.
        if speaker is not None and (
            not isinstance(speaker, str)
            or len(speaker) > 64
            or (speaker != "" and not speaker.startswith("S_"))
        ):
            raise BrowserProtocolError("speaker is invalid")
        # Campaign-template persona overrides; both fall back to server config
        # when absent/empty.
        bot_name = payload.get("bot_name")
        if bot_name is not None and (
            not isinstance(bot_name, str) or len(bot_name) > MAX_BOT_NAME_CHARS
        ):
            raise BrowserProtocolError("bot_name is invalid")
        speaking_style = payload.get("speaking_style")
        if speaking_style is not None and (
            not isinstance(speaking_style, str)
            or len(speaking_style) > MAX_SPEAKING_STYLE_CHARS
        ):
            raise BrowserProtocolError("speaking_style is invalid")
        template_id = payload.get("template_id")
        if template_id is not None and (
            isinstance(template_id, bool)
            or not isinstance(template_id, int)
            or template_id <= 0
        ):
            raise BrowserProtocolError("template_id is invalid")
    elif event_type == "input_audio.append":
        _require_keys(
            payload,
            {"encoding", "sample_rate_hz", "channels", "duration_ms", "audio_b64"},
        )
        if payload["encoding"] != "pcm_s16le":
            raise BrowserProtocolError("audio encoding is unsupported")
        if payload["sample_rate_hz"] != 16000:
            raise BrowserProtocolError("audio sample rate must be 16000")
        if payload["channels"] != 1:
            raise BrowserProtocolError("audio channels must be 1")
        audio_b64 = payload["audio_b64"]
        if not isinstance(audio_b64, str) or not audio_b64:
            raise BrowserProtocolError("audio_b64 is invalid")
        estimated = (len(audio_b64) * 3) // 4
        if estimated > max_audio_bytes:
            raise BrowserProtocolError("audio exceeds size bound", oversized=True)
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise BrowserProtocolError("audio base64 is invalid") from error
        if not audio or len(audio) % 2 or len(audio) > max_audio_bytes:
            raise BrowserProtocolError(
                "audio must be even PCM16 bytes within the bound",
                oversized=len(audio) > max_audio_bytes,
            )
        duration_ms = payload["duration_ms"]
        if not isinstance(duration_ms, int) or duration_ms != len(audio) * 1000 // 32000:
            raise BrowserProtocolError("audio duration_ms does not match PCM size")
    elif event_type == "input_audio.commit":
        _require_keys(payload, set())
    elif event_type == "user.text.submit":
        _require_keys(payload, {"text"})
        text = payload["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise BrowserProtocolError("text is invalid")
    elif event_type == "response.cancel":
        _require_keys(payload, {"response_id"})
        response_id = payload["response_id"]
        if not isinstance(response_id, str) or not response_id or len(response_id) > 64:
            raise BrowserProtocolError("response_id is invalid")
    elif event_type == "session.end":
        _require_keys(payload, set())
    elif event_type == "ping":
        _require_keys(payload, set(), optional={"client_ts_ms"})

    return ClientEvent(type=event_type, event_id=event_id, seq=seq, payload=payload, audio=audio)


def _require_keys(
    payload: dict[str, object], required: set[str], *, optional: set[str] | None = None
) -> None:
    allowed = required | (optional or set())
    extra = set(payload) - allowed
    if extra:
        raise BrowserProtocolError("payload has unknown keys")
    missing = required - set(payload)
    if missing:
        raise BrowserProtocolError("payload is missing keys")


def server_event(
    event_type: str,
    *,
    session_id: str,
    seq: int,
    payload: dict[str, object],
    turn_id: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "type": event_type,
        "event_id": f"server-{seq}",
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": seq,
        "ts_ms": int(time.time() * 1000),
        "payload": payload,
    }


def error_event(
    *,
    session_id: str | None,
    seq: int,
    code: str,
    message: str,
    retryable: bool,
) -> dict[str, object]:
    return server_event(
        "error",
        session_id=session_id or "",
        seq=seq,
        payload={"code": code, "message": message, "retryable": retryable},
    )
