# Doubao End-to-End Voice Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing FastAPI demo so Chinese microphone audio is streamed to Doubao Realtime, the browser plays Doubao's English speech in real time, and Qwen produces bilingual review subtitles without blocking speech.

**Architecture:** Keep the existing REST/SQLite review path and add an in-process FastAPI WebSocket gateway. Vendor-specific binary framing is isolated in a Doubao adapter, Qwen is an independent final-text subtitle adapter, and the browser uses AudioWorklet plus Web Audio for 16kHz PCM input and 24kHz PCM output.

**Tech Stack:** Python 3.11+, FastAPI/Starlette WebSocket, `websockets`, `httpx`, SQLite, browser Web Audio/AudioWorklet, vanilla ES modules, pytest, Node built-in test runner.

---

## Implementation constraints

- Follow the confirmed design in `docs/superpowers/specs/2026-08-02-doubao-e2e-voice-demo-design.md`.
- Never copy credentials from chat into source, tests, fixtures, logs, commands, or Git.
- Only use newly rotated credentials supplied through local environment variables for live verification.
- Keep `PROVIDER_MODE=mock|ark` behavior working. Add the realtime path alongside it.
- Do not silently fall back from Doubao to Mock, browser ASR, or browser TTS.
- Use Doubao O2.0 model `1.2.1.1` with an enabled English voice. The first documented candidate for manual setup is `en_female_dacey_uranus_bigtts`; the actual account entitlement remains a live-test prerequisite.
- Request output as `pcm_s16le`, 24kHz, mono. Send input as PCM16LE, 16kHz, mono, reframed to 20ms/640-byte upstream packets.
- Use Doubao `push_to_talk` mode in the MVP. The existing microphone button starts streaming; the second click sends EndASR. Starting a new capture during playback sends ClientInterrupt before new audio.
- Translate only final ASR and final assistant text with Qwen `qwen-flash`.
- Preserve the current security rule that public API payloads expose only safe relative audio references.

## File map

New backend files:

- `app/realtime/__init__.py`: package marker and public realtime interfaces.
- `app/realtime/doubao_protocol.py`: pure encoder/decoder for Volcengine WebSocket v3 frames.
- `app/realtime/doubao.py`: authenticated upstream connection and Doubao session commands.
- `app/realtime/qwen.py`: final-text subtitle translation adapter.
- `app/realtime/browser_protocol.py`: validation and construction of browser WebSocket events.
- `app/realtime/state.py`: pure realtime state and duplicate/late-event decisions.
- `app/realtime/audio.py`: bounded PCM-to-WAV writers and user-audio segmentation.
- `app/realtime/persistence.py`: maps completed realtime items to existing `sessions`/`turns`.
- `app/realtime/gateway.py`: per-browser-session orchestration and backpressure.

New frontend files:

- `app/static/pcm.js`: testable resampling and PCM conversion helpers.
- `app/static/pcm-worklet.js`: microphone AudioWorklet processor.
- `app/static/realtime-audio.js`: microphone lifecycle and 24kHz playback queue.
- `app/static/realtime.js`: browser WebSocket protocol client.
- `app/static/realtime-ui-state.js`: pure provisional-turn and cancellation reducer.
- `tests/js/pcm.test.mjs`: Node tests for audio helpers.
- `tests/js/realtime-audio.test.mjs`: Node tests for capture/playback lifecycle.
- `tests/js/realtime.test.mjs`: Node tests for event ordering and stale-connection rejection.
- `tests/js/realtime-ui-state.test.mjs`: Node tests for streaming UI state.

Modified files:

- `app/config.py`: secret-safe realtime and subtitle configuration.
- `app/main.py`: realtime dependency wiring, health capability, and WebSocket route.
- `app/storage.py`: descriptor-safe atomic WAV writes and bounded source-audio reads.
- `app/static/app.js`: choose REST or realtime call flow and render normalized events.
- `app/static/index.html`: realtime capability copy and module preloads if needed.
- `app/static/styles.css`: streaming/translation status states.
- `tests/conftest.py`: disabled realtime defaults for existing tests.
- `tests/test_config.py`, `tests/test_api.py`, `tests/test_static_contract.py`: updated contracts.
- `.env.example`, `requirements.txt`, `README.md`: safe configuration and runbook.

## Task 1: Add secret-safe realtime configuration

**Files:**
- Create: `tests/test_realtime_config.py`
- Modify: `app/config.py`
- Modify: `tests/conftest.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_realtime_config.py` with focused environment isolation:

```python
from dataclasses import asdict

import pytest

from app.config import Settings


REALTIME_KEYS = (
    "REALTIME_PROVIDER",
    "DOUBAO_REALTIME_WS_URL",
    "DOUBAO_APP_ID",
    "DOUBAO_ACCESS_KEY",
    "DOUBAO_APP_KEY",
    "DOUBAO_RESOURCE_ID",
    "DOUBAO_MODEL",
    "DOUBAO_TTS_SPEAKER",
    "DOUBAO_INPUT_SAMPLE_RATE",
    "DOUBAO_OUTPUT_SAMPLE_RATE",
    "DOUBAO_CONNECT_TIMEOUT_SECONDS",
    "DOUBAO_RECV_TIMEOUT_SECONDS",
    "DOUBAO_MAX_UPSTREAM_FRAME_BYTES",
    "DOUBAO_MAX_DECOMPRESSED_BYTES",
    "WS_MAX_MESSAGE_BYTES",
    "WS_MAX_BUFFER_BYTES",
    "WS_MAX_SESSION_SECONDS",
    "WS_MAX_CONCURRENT_SESSIONS",
    "WS_HEARTBEAT_SECONDS",
    "DASHSCOPE_API_KEY",
    "QWEN_SUBTITLE_ENABLED",
    "QWEN_SUBTITLE_MODEL",
    "QWEN_TIMEOUT_SECONDS",
    "DASHSCOPE_BASE_URL",
    "WS_ALLOWED_ORIGINS",
)


def clear_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REALTIME_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_realtime_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_realtime(monkeypatch)
    settings = Settings.from_env()
    assert settings.realtime.provider == "disabled"
    assert settings.realtime.enabled is False


def test_doubao_requires_all_secrets_and_english_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_realtime(monkeypatch)
    monkeypatch.setenv("REALTIME_PROVIDER", "doubao")
    with pytest.raises(ValueError, match="DOUBAO_APP_ID"):
        Settings.from_env()


def test_qwen_requires_api_key_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_realtime(monkeypatch)
    monkeypatch.setenv("QWEN_SUBTITLE_ENABLED", "true")
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        Settings.from_env()


def test_secret_values_are_not_represented(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_realtime(monkeypatch)
    values = {
        "REALTIME_PROVIDER": "doubao",
        "DOUBAO_APP_ID": "private-test-app-id",
        "DOUBAO_ACCESS_KEY": "private-test-access-key",
        "DOUBAO_APP_KEY": "private-test-app-key",
        "DOUBAO_TTS_SPEAKER": "en_female_dacey_uranus_bigtts",
        "QWEN_SUBTITLE_ENABLED": "true",
        "DASHSCOPE_API_KEY": "private-test-qwen-key",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    rendered = repr(settings)
    assert "private-test-access-key" not in rendered
    assert "private-test-qwen-key" not in rendered
    assert asdict(settings.realtime)["model"] == "1.2.1.1"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOUBAO_CONNECT_TIMEOUT_SECONDS", "0"),
        ("DOUBAO_MAX_UPSTREAM_FRAME_BYTES", "-1"),
        ("WS_MAX_CONCURRENT_SESSIONS", "not-an-int"),
        ("QWEN_TIMEOUT_SECONDS", "nan"),
    ],
)
def test_realtime_numeric_bounds_are_strict(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    clear_realtime(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_realtime_config.py -q
```

Expected: collection or assertion failure because `Settings.realtime` does not exist.

- [ ] **Step 3: Add the realtime settings model and validation**

In `app/config.py`, add a nested frozen dataclass with secret fields excluded from `repr`:

```python
import math
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive finite number") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


@dataclass(frozen=True)
class RealtimeSettings:
    provider: str = "disabled"
    ws_url: str = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
    app_id: str | None = field(default=None, repr=False)
    access_key: str | None = field(default=None, repr=False)
    app_key: str | None = field(default=None, repr=False)
    resource_id: str = "volc.speech.dialog"
    model: str = "1.2.1.1"
    speaker: str | None = None
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    connect_timeout_seconds: float = 15.0
    receive_timeout_seconds: float = 10.0
    max_message_bytes: int = 131_072
    max_buffer_bytes: int = 1_048_576
    max_upstream_frame_bytes: int = 262_144
    max_decompressed_bytes: int = 262_144
    max_session_seconds: int = 1800
    max_concurrent_sessions: int = 4
    heartbeat_seconds: float = 15.0
    subtitle_enabled: bool = False
    dashscope_api_key: str | None = field(default=None, repr=False)
    subtitle_model: str = "qwen-flash"
    subtitle_timeout_seconds: float = 10.0
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )

    @property
    def enabled(self) -> bool:
        return self.provider == "doubao"
```

Add `_realtime_from_env()` and call it from `Settings.from_env()`:

```python
def _realtime_from_env() -> RealtimeSettings:
    provider = os.getenv("REALTIME_PROVIDER", "disabled").strip().lower()
    if provider not in {"disabled", "doubao"}:
        raise ValueError("REALTIME_PROVIDER must be 'disabled' or 'doubao'")
    values = {
        "DOUBAO_APP_ID": os.getenv("DOUBAO_APP_ID"),
        "DOUBAO_ACCESS_KEY": os.getenv("DOUBAO_ACCESS_KEY"),
        "DOUBAO_APP_KEY": os.getenv("DOUBAO_APP_KEY"),
        "DOUBAO_TTS_SPEAKER": os.getenv("DOUBAO_TTS_SPEAKER"),
    }
    if provider == "doubao":
        missing = next((name for name, value in values.items() if not value), None)
        if missing:
            raise ValueError(f"{missing} is required when REALTIME_PROVIDER is doubao")
        if not values["DOUBAO_TTS_SPEAKER"].startswith("en_"):
            raise ValueError("DOUBAO_TTS_SPEAKER must be an enabled English voice")
    subtitle_enabled = _env_bool("QWEN_SUBTITLE_ENABLED", False)
    qwen_key = os.getenv("DASHSCOPE_API_KEY")
    if subtitle_enabled and not qwen_key:
        raise ValueError("DASHSCOPE_API_KEY is required when QWEN_SUBTITLE_ENABLED is true")
    origins = tuple(
        item.strip()
        for item in os.getenv(
            "WS_ALLOWED_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000",
        ).split(",")
        if item.strip()
    )
    ws_url = os.getenv(
        "DOUBAO_REALTIME_WS_URL",
        "wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
    )
    model = os.getenv("DOUBAO_MODEL", "1.2.1.1")
    input_rate = _env_int("DOUBAO_INPUT_SAMPLE_RATE", 16000)
    output_rate = _env_int("DOUBAO_OUTPUT_SAMPLE_RATE", 24000)
    if provider == "doubao" and not ws_url.startswith("wss://"):
        raise ValueError("DOUBAO_REALTIME_WS_URL must use wss")
    if provider == "doubao" and model != "1.2.1.1":
        raise ValueError("DOUBAO_MODEL must be the O2.0 model 1.2.1.1")
    if input_rate != 16000:
        raise ValueError("DOUBAO_INPUT_SAMPLE_RATE must be 16000")
    if output_rate != 24000:
        raise ValueError("DOUBAO_OUTPUT_SAMPLE_RATE must be 24000")
    if not origins:
        raise ValueError("WS_ALLOWED_ORIGINS must not be empty")
    return RealtimeSettings(
        provider=provider,
        ws_url=ws_url,
        app_id=values["DOUBAO_APP_ID"],
        access_key=values["DOUBAO_ACCESS_KEY"],
        app_key=values["DOUBAO_APP_KEY"],
        resource_id=os.getenv("DOUBAO_RESOURCE_ID", "volc.speech.dialog"),
        model=model,
        speaker=values["DOUBAO_TTS_SPEAKER"],
        input_sample_rate=input_rate,
        output_sample_rate=output_rate,
        connect_timeout_seconds=_env_float("DOUBAO_CONNECT_TIMEOUT_SECONDS", 15.0),
        receive_timeout_seconds=_env_float("DOUBAO_RECV_TIMEOUT_SECONDS", 10.0),
        max_message_bytes=_env_int("WS_MAX_MESSAGE_BYTES", 131_072),
        max_buffer_bytes=_env_int("WS_MAX_BUFFER_BYTES", 1_048_576),
        max_upstream_frame_bytes=_env_int(
            "DOUBAO_MAX_UPSTREAM_FRAME_BYTES", 262_144
        ),
        max_decompressed_bytes=_env_int(
            "DOUBAO_MAX_DECOMPRESSED_BYTES", 262_144
        ),
        max_session_seconds=_env_int("WS_MAX_SESSION_SECONDS", 1800),
        max_concurrent_sessions=_env_int("WS_MAX_CONCURRENT_SESSIONS", 4),
        heartbeat_seconds=_env_float("WS_HEARTBEAT_SECONDS", 15.0),
        subtitle_enabled=subtitle_enabled,
        dashscope_api_key=qwen_key,
        subtitle_model=os.getenv("QWEN_SUBTITLE_MODEL", "qwen-flash"),
        subtitle_timeout_seconds=_env_float("QWEN_TIMEOUT_SECONDS", 10.0),
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
        allowed_origins=origins,
    )
```

All validation messages contain variable names but never echo supplied values.

Add `realtime: RealtimeSettings = field(default_factory=RealtimeSettings)` as the final `Settings` field so existing direct constructors remain valid. Update the fixture in `tests/conftest.py` only if dataclass ordering requires an explicit `RealtimeSettings()`.

- [ ] **Step 4: Add the WebSocket dependency and safe example variables**

Append this dependency to `requirements.txt`:

```text
websockets>=14,<16
```

Install the locked project requirements before importing the new client:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Expected: installation exits zero and installs a compatible `websockets` release.

Append empty or public-only configuration names to `.env.example`:

```dotenv
REALTIME_PROVIDER=disabled
DOUBAO_REALTIME_WS_URL=wss://openspeech.bytedance.com/api/v3/realtime/dialogue
DOUBAO_APP_ID=
DOUBAO_ACCESS_KEY=
DOUBAO_RESOURCE_ID=volc.speech.dialog
DOUBAO_APP_KEY=
DOUBAO_MODEL=1.2.1.1
DOUBAO_TTS_SPEAKER=
DOUBAO_INPUT_SAMPLE_RATE=16000
DOUBAO_OUTPUT_SAMPLE_RATE=24000
DOUBAO_CONNECT_TIMEOUT_SECONDS=15
DOUBAO_RECV_TIMEOUT_SECONDS=10
DOUBAO_MAX_UPSTREAM_FRAME_BYTES=262144
DOUBAO_MAX_DECOMPRESSED_BYTES=262144
QWEN_SUBTITLE_ENABLED=false
DASHSCOPE_API_KEY=
QWEN_SUBTITLE_MODEL=qwen-flash
QWEN_TIMEOUT_SECONDS=10
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
WS_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
WS_MAX_MESSAGE_BYTES=131072
WS_MAX_BUFFER_BYTES=1048576
WS_MAX_SESSION_SECONDS=1800
WS_MAX_CONCURRENT_SESSIONS=4
WS_HEARTBEAT_SECONDS=15
```

- [ ] **Step 5: Run configuration and existing tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_realtime_config.py -q
.venv/bin/python -m pytest tests/test_api.py -q
```

Expected: both commands pass; no secret value appears in failure output.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/config.py tests/conftest.py tests/test_realtime_config.py requirements.txt .env.example
git commit -m "feat: add realtime voice configuration"
```

## Task 2: Implement the Doubao binary protocol codec

**Files:**
- Create: `app/realtime/__init__.py`
- Create: `app/realtime/doubao_protocol.py`
- Create: `tests/test_doubao_protocol.py`

- [ ] **Step 1: Write exact-byte and round-trip tests**

Create tests anchored to the official no-compression StartConnection frame:

```python
import json

import pytest

from app.realtime.doubao_protocol import (
    CLIENT_AUDIO_ONLY_REQUEST,
    EVENT_START_CONNECTION,
    EVENT_TTS_RESPONSE,
    FrameProtocolError,
    decode_frame,
    encode_audio,
    encode_event,
)


def test_start_connection_matches_official_byte_vector() -> None:
    encoded = encode_event(EVENT_START_CONNECTION, {})
    assert list(encoded) == [17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125]


def test_audio_request_contains_event_session_and_raw_pcm() -> None:
    pcm = b"\x01\x00\x02\x00"
    encoded = encode_audio("session-1", pcm)
    decoded = decode_frame(encoded)
    assert decoded.message_type == CLIENT_AUDIO_ONLY_REQUEST
    assert decoded.event == 200
    assert decoded.session_id == "session-1"
    assert decoded.payload == pcm


def test_decodes_server_tts_pcm_frame() -> None:
    session = b"session-1"
    pcm = b"\x00\x00\xff\x7f"
    frame = bytes([0x11, 0xB4, 0x00, 0x00])
    frame += EVENT_TTS_RESPONSE.to_bytes(4, "big")
    frame += len(session).to_bytes(4, "big") + session
    frame += len(pcm).to_bytes(4, "big") + pcm
    decoded = decode_frame(frame)
    assert decoded.event == EVENT_TTS_RESPONSE
    assert decoded.session_id == "session-1"
    assert decoded.payload == pcm


def test_rejects_truncated_payload() -> None:
    with pytest.raises(FrameProtocolError, match="optional field"):
        decode_frame(bytes([0x11, 0x94, 0x10, 0x00, 0, 0, 0, 50]))
```

Also add `test_rejects_frame_over_raw_limit` and
`test_rejects_gzip_expansion_over_limit`. The latter builds a valid gzip JSON
frame whose compressed bytes fit below `max_frame_bytes` but whose expanded
body exceeds `max_decompressed_bytes`. Keep the StartConnection and TTS tests
as exact-byte tests; any additional sanitized provider fixture must contain the
official/current bytes and expected parsed shape, never invented provider IDs
or credentials.

Add exact tests for sequence flags `0x1`, `0x2`, and `0x3`. Add connection
response fixtures for both supported encodings: event followed directly by
payload size, and event followed by an explicit zero-length session ID then
payload size. Both must decode without confusing the zero session length for
an empty payload.

- [ ] **Step 2: Run the codec tests and verify they fail**

```bash
.venv/bin/python -m pytest tests/test_doubao_protocol.py -q
```

Expected: import failure because `app.realtime.doubao_protocol` does not exist.

- [ ] **Step 3: Implement the pure codec**

Create constants, a frozen frame type, bounded readers, and exact big-endian encoding in `app/realtime/doubao_protocol.py`:

```python
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

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200
EVENT_SAY_HELLO = 300
EVENT_END_ASR = 400
EVENT_CLIENT_INTERRUPT = 515
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

_CONNECTION_EVENTS = {
    EVENT_START_CONNECTION,
    EVENT_FINISH_CONNECTION,
    EVENT_CONNECTION_STARTED,
    EVENT_CONNECTION_FAILED,
    EVENT_CONNECTION_FINISHED,
}


class FrameProtocolError(ValueError):
    pass


def _gunzip_bounded(data: bytes, limit: int) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        output = decoder.decompress(data, limit + 1)
        if len(output) > limit or decoder.unconsumed_tail:
            raise FrameProtocolError("payload decompression exceeds limit")
        output += decoder.flush(limit + 1 - len(output))
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
    return bytes((0x11, (message_type << 4) | FLAG_WITH_EVENT,
                  (serialization << 4) | compression, 0x00))


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
    frame = bytearray(_header(CLIENT_AUDIO_ONLY_REQUEST, SERIALIZATION_RAW, COMPRESSION_NONE))
    frame.extend(EVENT_TASK_REQUEST.to_bytes(4, "big"))
    frame.extend(len(encoded_session).to_bytes(4, "big"))
    frame.extend(encoded_session)
    frame.extend(len(pcm).to_bytes(4, "big"))
    frame.extend(pcm)
    return bytes(frame)
```

Complete `decode_frame()` with strict cursor bounds, event/session extraction, payload-size equality, optional gzip decompression, UTF-8 JSON decoding, and safe `FrameProtocolError` messages that never embed payload content. Treat server message types `0x9` and `0xB` as response frames, and `0xF` as `error_code + payload_size + payload`.

Use this bounded implementation structure:

```python
def decode_frame(
    data: bytes,
    *,
    max_frame_bytes: int = 262_144,
    max_decompressed_bytes: int = 262_144,
) -> DoubaoFrame:
    if max_frame_bytes <= 0 or max_decompressed_bytes <= 0:
        raise ValueError("frame limits must be positive")
    if isinstance(data, bytes) and len(data) > max_frame_bytes:
        raise FrameProtocolError("frame exceeds limit")
    if not isinstance(data, bytes) or len(data) < 8:
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
        value = int.from_bytes(data[cursor:cursor + 4], "big", signed=signed)
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
        # Some server variants include an explicit zero-length session field
        # on connection events. Consume it only when the following payload
        # size makes the remaining frame exact.
        if cursor + 8 <= len(data) and int.from_bytes(data[cursor:cursor + 4], "big") == 0:
            possible_size = int.from_bytes(data[cursor + 4:cursor + 8], "big")
            if cursor + 8 + possible_size == len(data):
                cursor += 4
    elif event is not None:
        session_size = take_int()
        if session_size <= 0 or cursor + session_size > len(data):
            raise FrameProtocolError("session id is invalid")
        try:
            session_id = data[cursor:cursor + session_size].decode("utf-8")
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
    if serialization == SERIALIZATION_JSON:
        try:
            payload: bytes | dict[str, Any] = json.loads(payload_bytes.decode("utf-8"))
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
```

The decoder must enforce the raw-frame limit before cursor parsing and the
expanded-payload limit before UTF-8/JSON parsing. It must never call
`gzip.decompress()` or another unbounded convenience decoder.

- [ ] **Step 4: Run codec tests**

```bash
.venv/bin/python -m pytest tests/test_doubao_protocol.py -q
```

Expected: all codec tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/realtime/__init__.py app/realtime/doubao_protocol.py tests/test_doubao_protocol.py
git commit -m "feat: implement Doubao realtime framing"
```

## Task 3: Implement the authenticated Doubao realtime client

**Files:**
- Create: `app/realtime/doubao.py`
- Create: `tests/test_doubao_client.py`

- [ ] **Step 1: Write fake-socket client tests**

The fake socket must record headers and frames without opening the network:

```python
import asyncio
import base64
import json

import pytest

from app.config import RealtimeSettings
from app.realtime.doubao import DoubaoRealtimeClient
from app.realtime.doubao_protocol import decode_frame


class FakeSocket:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = asyncio.Queue()
        for response in responses:
            self.responses.put_nowait(response)
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, value: bytes) -> None:
        self.sent.append(value)

    async def recv(self) -> bytes:
        return await self.responses.get()

    async def close(self) -> None:
        self.closed = True


def server_json_frame(
    event: int,
    payload: dict[str, object],
    *,
    session_id: str | None,
) -> bytes:
    encoded_payload = json.dumps(payload, separators=(",", ":")).encode()
    frame = bytearray((0x11, 0x94, 0x10, 0x00))
    frame.extend(event.to_bytes(4, "big"))
    if session_id is not None:
        encoded_session = session_id.encode()
        frame.extend(len(encoded_session).to_bytes(4, "big"))
        frame.extend(encoded_session)
    frame.extend(len(encoded_payload).to_bytes(4, "big"))
    frame.extend(encoded_payload)
    return bytes(frame)


def enabled_settings() -> RealtimeSettings:
    return RealtimeSettings(
        provider="doubao",
        app_id="test-app",
        access_key="test-access",
        app_key="test-app-key",
        speaker="en_female_dacey_uranus_bigtts",
        subtitle_enabled=False,
    )


@pytest.mark.asyncio
async def test_start_sends_connection_session_and_english_greeting() -> None:
    socket = FakeSocket([
        server_json_frame(50, {}, session_id=None),
        server_json_frame(150, {"dialog_id": "dialog-1"}, session_id="session-1"),
    ])
    captured: dict[str, object] = {}

    async def connector(
        url: str, headers: dict[str, str], *, max_size: int
    ) -> FakeSocket:
        assert max_size == enabled_settings().max_upstream_frame_bytes
        captured.update(url=url, headers=headers)
        return socket

    client = DoubaoRealtimeClient(
        enabled_settings(), connector=connector, session_id="session-1"
    )
    await client.connect(send_greeting=False)
    await client.say_hello("Hello, this is the customer care team.")
    frames = [decode_frame(item) for item in socket.sent]
    assert [frame.event for frame in frames] == [1, 100, 300]
    assert frames[1].payload["dialog"]["extra"]["model"] == "1.2.1.1"
    assert frames[1].payload["tts"]["audio_config"]["format"] == "pcm_s16le"
    assert frames[2].payload["content"].startswith("Hello")
    assert captured["headers"]["X-Api-Access-Key"] == "test-access"
```

Mark every async test with `@pytest.mark.asyncio`. Add these focused cases in
the same module:
`test_send_audio_splits_1280_bytes_into_two_640_byte_task_requests`,
`test_end_asr_zero_pads_one_final_even_partial_to_640_bytes_before_event_400`,
`test_chat_text_query_uses_verified_event_501_fixture`,
`test_interrupt_sends_event_515`,
`test_finish_waits_for_152_sends_2_waits_for_52_and_closes`,
`test_text_upstream_frame_is_rejected_safely`, and
`test_upstream_failure_does_not_include_headers_or_payload`. Build lifecycle
responses with `server_json_frame()` so they remain independent from the
production encoder. Event 501 is the separately named `ChatTextQuery`
operation verified in the current official Python example: assert its JSON
payload is exactly `{"content": text}` and its session framing matches the
other session events.

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_doubao_client.py -q
```

Expected: import failure because `DoubaoRealtimeClient` does not exist.

- [ ] **Step 3: Implement the client and injectable connector**

Implement a production connector with the current `websockets` asyncio API:

```python
from typing import Protocol

from websockets.asyncio.client import connect


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
    return await connect(
        url,
        additional_headers=headers,
        ping_interval=None,
        max_size=max_size,
    )
```

The injected connector has that same keyword-only `max_size` signature. Never
pass `max_size=None`; pass `settings.max_upstream_frame_bytes`, then pass both
that value and `settings.max_decompressed_bytes` into `decode_frame()`.

Build headers only inside the client:

```python
def _headers(self) -> dict[str, str]:
    return {
        "X-Api-App-ID": self.settings.app_id or "",
        "X-Api-Access-Key": self.settings.access_key or "",
        "X-Api-Resource-Id": self.settings.resource_id,
        "X-Api-App-Key": self.settings.app_key or "",
        "X-Api-Connect-Id": self.connection_id,
    }
```

Construct the StartSession payload with non-null `asr.extra`, `tts.extra`, and `dialog.extra`:

```python
def _start_session_payload(self) -> dict[str, object]:
    return {
        "asr": {"extra": {"end_smooth_window_ms": 1500}},
        "tts": {
            "speaker": self.settings.speaker,
            "extra": {},
            "audio_config": {
                "channel": 1,
                "format": "pcm_s16le",
                "sample_rate": self.settings.output_sample_rate,
            },
        },
        "dialog": {
            "bot_name": "English Customer Care",
            "system_role": (
                "You are a professional outbound customer-service agent. "
                "Understand the user's Chinese speech, but always reply in English. "
                "Be concise, ask one question at a time, and never claim a real phone call."
            ),
            "speaking_style": "Natural American English, calm, warm, and professional.",
            "location": {"city": "北京"},
            "extra": {
                "strict_audit": False,
                "audit_response": "",
                "recv_timeout": int(self.settings.receive_timeout_seconds),
                "input_mod": "push_to_talk",
                "model": self.settings.model,
            },
        },
    }
```

Use exactly one socket reader for the lifetime of a connected client. Start
`_reader_loop()` immediately after the socket is created. It alone calls
`socket.recv()`, rejects `str` messages with a fixed protocol error, decodes
bounded binary frames, resolves lifecycle acknowledgement futures for events
50, 150, 152, and 52, and puts all non-lifecycle frames into a bounded
`asyncio.Queue`. `receive()` only awaits that decoded-frame queue; it never
calls `socket.recv()` itself. Queue overflow, decode failure, provider error,
and reader termination fail all pending acknowledgement futures and surface a
fixed-category `DoubaoUpstreamError` without headers, raw bodies, or payload
text.

`connect()` creates the 50/150 futures before sending events 1 and 100 and
waits for each with `asyncio.timeout(settings.receive_timeout_seconds)`.
`say_hello()` sends event 300 with an English scenario greeting.
`send_audio()` buffers and emits only complete 640-byte packets. To keep that
contract exact, `end_asr()` zero-pads one retained non-empty, even PCM16 partial
packet to exactly 640 bytes, sends it once, clears the buffer, and then sends
event 400; those padding bytes are transport-only and are never written to the
user WAV. Odd input is rejected. `send_text_query()` is the isolated,
official-example-backed ChatTextQuery event 501 operation described above.
`interrupt()` sends 515 before a new push-to-talk input.

`finish()` creates the 152 future, sends 102, awaits the reader-resolved future
with the same bounded timeout, creates the 52 future, sends 2, and awaits 52.
In `finally` it closes the socket,
cancels and awaits the reader task, and unblocks queued consumers exactly once.
It must never call `recv()` or race the reader loop.

Do not log `_headers()`, raw frames, or upstream bodies. Expose the safe `X-Tt-Logid` if the installed library makes response headers available, but never treat it as proof of successful speech.

- [ ] **Step 4: Run client and protocol tests**

```bash
.venv/bin/python -m pytest tests/test_doubao_client.py tests/test_doubao_protocol.py -q
```

Expected: all tests pass without network access.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/realtime/doubao.py tests/test_doubao_client.py
git commit -m "feat: add Doubao realtime client"
```

## Task 4: Add the Qwen final-subtitle adapter

**Files:**
- Create: `app/realtime/qwen.py`
- Create: `tests/test_qwen_subtitles.py`

- [ ] **Step 1: Write request, parsing, and failure tests**

Use `httpx.MockTransport`:

```python
import json

import httpx
import pytest

from app.realtime.qwen import SubtitleTranslationError, QwenSubtitleTranslator


@pytest.mark.asyncio
async def test_translates_final_text_without_answering_it() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "请问现在方便通话吗？"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = QwenSubtitleTranslator(
            api_key="private-test-key",
            model="qwen-flash",
            base_url="https://dashscope.test/compatible-mode/v1",
            client=client,
        )
        result = await translator.translate(
            "Is now a good time to talk?", source_language="en", target_language="zh"
        )
    assert result.text == "请问现在方便通话吗？"
    assert captured["authorization"] == "Bearer private-test-key"
    assert captured["body"]["temperature"] == 0


@pytest.mark.asyncio
async def test_rate_limit_is_safe_and_structured() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, text="private upstream body")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        translator = QwenSubtitleTranslator(
            api_key="private-test-key",
            model="qwen-flash",
            base_url="https://dashscope.test/compatible-mode/v1",
            client=client,
        )
        with pytest.raises(SubtitleTranslationError, match="rate limit") as failure:
            await translator.translate("Hello", source_language="en", target_language="zh")
    assert "private upstream body" not in str(failure.value)
```

Also test empty text rejection, malformed choices, Chinese-to-English direction,
timeout categorization, fixed safe categories for authentication/rate-limit/
transport failures, and `aclose()` ownership. Add a disabled-integration test
showing that `translator=None` makes no HTTP request and is reported as
"disabled", not as a translation failure.

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_qwen_subtitles.py -q
```

Expected: import failure because the Qwen subtitle module does not exist.

- [ ] **Step 3: Implement the adapter**

Create a small result and safe error type:

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SubtitleResult:
    text: str
    model: str


class SubtitleTranslationError(RuntimeError):
    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class SubtitleTranslator(Protocol):
    async def translate(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> SubtitleResult: ...

    async def aclose(self) -> None: ...
```

`QwenSubtitleTranslator` implements this protocol. Its constructor is
`QwenSubtitleTranslator(*, api_key: str, model: str, base_url: str,
timeout_seconds: float, client: httpx.AsyncClient | None = None)`. Reject a
non-positive/non-finite timeout. Post to `{base_url}/chat/completions` with a
bearer authorization header, `model`, `temperature: 0`, and two messages. Pass
an explicit `httpx.Timeout(timeout_seconds)` on every request; the app-level
setting is the only source of this value. The system message must say that the
model is a strict translator, must preserve entities and negation, and must
return only the translation. Validate `source_language,target_language`
against `{zh,en}` and cap source text at the same bound used by the browser
protocol.

Map 401/403 to `category="auth"`, 429 to `"rate_limit"`,
`httpx.TimeoutException` to `"timeout"`, other `httpx.TransportError` to
`"transport"`, and invalid JSON/choices/content to `"malformed"`. Each mapping
uses a fixed public message and chains the private exception only internally;
never include the URL, headers, response body, request body, or key in the
exception string. When subtitles are disabled, dependency injection passes
`None` instead of constructing a fake/disabled translator. Callers skip
translation, persist an empty translation with no `subtitle_error`, and emit a
safe disabled status.

- [ ] **Step 4: Run subtitle tests**

```bash
.venv/bin/python -m pytest tests/test_qwen_subtitles.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add app/realtime/qwen.py tests/test_qwen_subtitles.py
git commit -m "feat: add Qwen subtitle translator"
```

## Task 5: Define the browser protocol and realtime state reducer

**Files:**
- Create: `app/realtime/browser_protocol.py`
- Create: `app/realtime/state.py`
- Create: `tests/test_realtime_browser_protocol.py`
- Create: `tests/test_realtime_state.py`

- [ ] **Step 1: Write protocol validation tests**

```python
import base64
import json

import pytest

from app.realtime.browser_protocol import BrowserProtocolError, parse_client_event


def envelope(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "v": 1,
        "type": event_type,
        "event_id": "client-1",
        "session_id": None,
        "turn_id": None,
        "seq": 1,
        "ts_ms": 1785600000000,
        "payload": payload,
    }


def test_accepts_16k_pcm_audio() -> None:
    pcm = bytes(1280)
    event = parse_client_event(
        json.dumps(
            envelope(
                "input_audio.append",
                {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "duration_ms": 40,
                    "audio_b64": base64.b64encode(pcm).decode(),
                },
            ),
            separators=(",", ":"),
        ),
        max_message_bytes=65_536,
        max_audio_bytes=32_000,
        allowed_scenarios=frozenset({"product_intro"}),
    )
    assert event.audio == pcm


def test_rejects_wrong_sample_rate() -> None:
    with pytest.raises(BrowserProtocolError, match="sample rate"):
        parse_client_event(
            json.dumps(
                envelope(
                    "input_audio.append",
                    {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": 48000,
                        "channels": 1,
                        "duration_ms": 40,
                        "audio_b64": "AA==",
                    },
                )
            ),
            max_message_bytes=65_536,
            max_audio_bytes=32_000,
            allowed_scenarios=frozenset({"product_intro"}),
        )
```

Add tests proving the raw UTF-8 byte bound is checked before `json.loads`, and
the decoded-audio bound is checked before retaining decoded PCM. Also cover a
non-text raw value, invalid JSON/non-object JSON, unknown event types, invalid
base64, odd/empty PCM, unapproved scenario IDs, and text length. Sequence
monotonicity belongs to `RealtimeState`, not this parser.

- [ ] **Step 2: Write reducer tests for duplicate and cancelled responses**

```python
from app.realtime.state import RealtimeState


def test_client_sequence_is_strictly_monotonic() -> None:
    state = RealtimeState(session_id="session-1")
    assert state.accept_client_seq(1) is True
    assert state.accept_client_seq(1) is False
    assert state.accept_client_seq(0) is False
    assert state.accept_client_seq(2) is True


def test_cancelled_response_rejects_late_audio() -> None:
    state = RealtimeState(session_id="session-1")
    generation = state.open_response_boundary(event=550)
    assert generation == 1
    state.interrupt_for_new_input()
    assert state.accept_audio(generation=1, chunk_seq=1) is False
    assert state.open_response_boundary(event=550) is None
    state.complete_input_turn()
    assert state.open_response_boundary(event=550) == 2


def test_response_audio_sequence_is_monotonic() -> None:
    state = RealtimeState(session_id="session-1")
    generation = state.open_response_boundary(event=350)
    assert generation == 1
    assert state.accept_audio(generation=1, chunk_seq=1) is True
    assert state.accept_audio(generation=1, chunk_seq=1) is False
    assert state.accept_audio(generation=1, chunk_seq=2) is True
    state.close_response(generation=1, event=359)
    assert state.accept_audio(generation=1, chunk_seq=3) is False
```

- [ ] **Step 3: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_realtime_browser_protocol.py tests/test_realtime_state.py -q
```

Expected: import failures for both new modules.

- [ ] **Step 4: Implement validated envelopes and pure state**

`browser_protocol.py` must expose:

```python
@dataclass(frozen=True)
class ClientEvent:
    type: str
    event_id: str
    seq: int
    payload: dict[str, object]
    audio: bytes | None = None


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
```

The parser API is exact:

```python
def parse_client_event(
    raw_message: str,
    *,
    max_message_bytes: int,
    max_audio_bytes: int,
    allowed_scenarios: frozenset[str],
) -> ClientEvent: ...
```

Require `str`, compute `len(raw_message.encode("utf-8"))`, and reject an
oversized message before JSON or base64 work. Then require one JSON object with
the exact envelope keys/types. Allow only `session.start`,
`input_audio.append`, `input_audio.commit`, `user.text.submit`,
`response.cancel`, `session.end`, and `ping`. Audio validation estimates the
decoded size before calling `base64.b64decode(..., validate=True)`, then
requires PCM16LE/16kHz/mono and an even nonzero decoded size no larger than
`max_audio_bytes`. It also requires `duration_ms == len(audio) * 1000 / 32000`
for the integer 40ms chunks used by the browser. `session.start` has only an
allowed `scenario_id`; `user.text.submit` has one bounded non-empty `text`;
`response.cancel` has one non-empty local `response_id`; commit/end payloads
are empty; and `ping` may contain only its client timestamp. Unknown payload
keys are rejected. `parse_client_event()` validates that `seq` is a positive
integer but does not retain cross-message state.

Define and test the public server payloads; do not leak or require provider
payload identifiers:

- `session.ready`: `{"scenario_id": str, "provider": "doubao",
  "created_at": str}`.
- `asr.partial|final`: `{"text": str}` with a stable provisional top-level
  `turn_id`.
- `user.translation.done`: `{"text": str, "model": str}`;
  `user.translation.unavailable` uses `{"reason": "disabled" | "failed"}`.
- `assistant.text.delta|done`: `{"response_id": str, "delta" | "text": str}`.
- `assistant.translation.done`: `{"response_id": str, "text": str,
  "model": str}`; `assistant.translation.unavailable` uses only
  `{"response_id": str, "reason": "disabled" | "failed"}`.
- `assistant.audio.chunk`: `{"response_id": str, "chunk_seq": int,
  "encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1,
  "audio_b64": str}`.
- `assistant.audio.done`: `{"response_id": str}`.
- `turn.completed`: `{"turn": <the existing safe REST turn shape>}`;
  `response.cancelled`: `{"response_id": str}`.
- `pong`: `{"client_event_id": str}`. Every accepted `ping` produces exactly
  one `pong` and is otherwise side-effect free.
- `error`: `{"code": str, "message": str, "retryable": bool}` using only
  fixed safe codes/messages.

`state.py` owns connection state, `accept_client_seq()`, the next server
sequence, optional deduplication by a verified binary-frame sequence (never an
invented JSON `question_id`/`reply_id`), a monotonically increasing local
`response_generation`, cancelled generations, and last audio chunk sequence.
`response_id(generation)` exposes only a local stable value such as
`response-1`; browser events never depend on an unverified provider ID.
`open_response_boundary(event=...)` accepts only verified Chat/TTS boundary
events 550 or 350. Repeated deltas for an open generation do not create a new
one. Event 559 marks text done but does not close audio; event 359 closes the
active generation. `interrupt_for_new_input()` invalidates the active
generation and blocks every 352 until `complete_input_turn()` (ASR 459 or a
validated text-submit boundary) arms the next response and a subsequent valid
550/350 opens it. This is the sole attribution source for TTS 352; raw 352
payloads are PCM and are never enriched with a provider `reply_id`. The reducer
contains no network or database I/O.

- [ ] **Step 5: Run protocol and reducer tests**

```bash
.venv/bin/python -m pytest tests/test_realtime_browser_protocol.py tests/test_realtime_state.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add app/realtime/browser_protocol.py app/realtime/state.py tests/test_realtime_browser_protocol.py tests/test_realtime_state.py
git commit -m "feat: define realtime browser protocol"
```

## Task 6: Add bounded WAV storage and realtime persistence

**Files:**
- Create: `app/realtime/audio.py`
- Create: `app/realtime/persistence.py`
- Create: `tests/test_realtime_audio.py`
- Create: `tests/test_realtime_persistence.py`
- Modify: `app/storage.py`

- [ ] **Step 1: Write WAV writer tests**

```python
import wave

import pytest

from app.realtime.audio import AudioLimitError, PcmWaveSink
from app.storage import AudioStorageError, AudioStore


def test_pcm_sink_writes_atomic_mono_wav(tmp_path) -> None:
    store = AudioStore(tmp_path / "audio")
    target = store.open_realtime_wav("session-1", "voice")
    sink = PcmWaveSink(target, sample_rate=24000, max_bytes=32)
    sink.write(b"\x00\x00\xff\x7f")
    completed = sink.finalize()
    assert completed == store.root / "session-1" / "voice.wav"
    assert not (store.root / "session-1" / "voice.wav.part").exists()
    with wave.open(str(completed), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 24000
        assert reader.readframes(2) == b"\x00\x00\xff\x7f"


def test_pcm_sink_stops_at_bound(tmp_path) -> None:
    store = AudioStore(tmp_path / "audio")
    sink = PcmWaveSink(
        store.open_realtime_wav("session-1", "voice"),
        sample_rate=16000,
        max_bytes=2,
    )
    with pytest.raises(AudioLimitError):
        sink.write(b"\x00\x00\x00\x00")
    sink.abort()
```

Also add three adversarial tests: a symlink in place of the session directory,
a symlink in place of `voice.wav.part`, and a symlink in place of the final
`voice.wav`. Each operation must raise `AudioStorageError`, must not alter the
symlink target, and must close/unlink only descriptors/names it created. Add a
test that an existing regular final file is never overwritten.

- [ ] **Step 2: Write persistence ordering and translation-failure tests**

Use a temporary `Repository` and `AudioStore`. The core assertion is:

```python
from app.realtime.qwen import SubtitleResult, SubtitleTranslationError


class ControlledTranslator:
    def __init__(
        self,
        *,
        results: dict[tuple[str, str, str], str],
        failures: set[tuple[str, str, str]],
    ) -> None:
        self.results = results
        self.failures = failures
        self.calls: list[tuple[str, str, str]] = []
        self.closed = False

    async def translate(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> SubtitleResult:
        key = (text, source_language, target_language)
        self.calls.append(key)
        if key in self.failures:
            raise SubtitleTranslationError(
                "translation failed", category="transport"
            )
        return SubtitleResult(text=self.results[key], model="qwen-flash")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_turn_order_and_subtitle_failure_are_preserved(tmp_path) -> None:
    repository = Repository(tmp_path / "sessions.db")
    audio_store = AudioStore(tmp_path / "audio")
    session = repository.create_session("product_intro", "doubao")
    translator = ControlledTranslator(
        results={("你好", "zh", "en"): "Hello"},
        failures={("Hello.", "en", "zh")},
    )
    persistence = RealtimePersistence(
        session_id=session["id"],
        repository=repository,
        audio_store=audio_store,
        translator=translator,
        translation_model="qwen-flash",
    )
    await persistence.finalize_user(text="你好", audio_path=None, input_kind="audio")
    persistence.append_assistant_text(generation=1, text="Hello.")
    persistence.mark_assistant_text_done(generation=1)
    persistence.mark_assistant_audio_done(generation=1, audio_path=None)
    await persistence.drain(timeout_seconds=1)
    turns = repository.get_session(session["id"])["turns"]
    assert [turn["speaker"] for turn in turns] == ["tester", "agent"]
    assert turns[0]["translated_text"] == "Hello"
    assert turns[1]["source_text"] == "Hello."
    assert turns[1]["translated_text"] == ""
    assert turns[1]["error_code"] == "subtitle_error"
```

Add `test_assistant_waits_for_text_and_audio_latches_when_559_precedes_352_359`,
`test_text_submit_persists_one_user_turn_without_audio`,
`test_none_translator_is_disabled_without_subtitle_error`, and
`test_abort_cancels_bounded_drain_and_removes_only_owned_parts`. The latch test
must call text-done before writing/finalizing audio and assert that exactly one
complete assistant turn is inserted only after audio-done.

- [ ] **Step 3: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_realtime_audio.py tests/test_realtime_persistence.py -q
```

Expected: import failures for the new realtime storage modules.

- [ ] **Step 4: Implement safe realtime audio paths and atomic WAV writes**

Do not add a path-returning `AudioStore.new_audio_path()` API. Add an
`open_realtime_wav(session_id: str, audio_id: str) -> AtomicWavTarget` API that
opens and owns the destination safely:

```python
@dataclass
class AtomicWavTarget:
    directory_fd: int
    part_fd: int
    part_name: str
    final_name: str
    final_path: Path
```

Validate both IDs first. Ensure the configured root exists, open it with
`O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, create the session directory relative
to that descriptor with mode `0700` if absent, and reopen it relative to the
root descriptor with `O_DIRECTORY | O_NOFOLLOW`. Create the deterministic
`<audio_id>.wav.part` relative to the verified session descriptor using
`O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` and mode `0600`. Preserve the
directory descriptor and part descriptor in `AtomicWavTarget`; on every error,
close them and unlink only the exact part name created by this call.

`PcmWaveSink` accepts only an `AtomicWavTarget`, wraps a duplicate/owned part
descriptor with `os.fdopen(..., "w+b")`, and gives that file object—not a
pathname—to `wave.open()`. Enforce even PCM and byte-count bounds. On
`finalize()`, close the WAV header, flush/fsync/close the file, use
`os.stat(final_name, dir_fd=..., follow_symlinks=False)` to reject any existing
regular file or symlink, and atomically `os.rename(part_name, final_name,
src_dir_fd=..., dst_dir_fd=...)` while holding the store's per-session publish
lock. The `0700` directory plus this lock protects the checked publish within
the process. Then fsync/close the directory descriptor. `abort()` is
idempotent, closes owned resources, and unlinks only `part_name` relative to
that descriptor. Never use `wave.open(path)`, `Path.replace()`, or
`os.replace()` on an unverified path.

Implement a bounded push-to-talk `InputAudioSegmenter`. The first `input_audio.append` in a new client turn opens a 16kHz sink, every validated chunk is written once, `input_audio.commit` marks the segment complete, `ASREnded` finalizes it, and `abort()` removes only its explicit `.part` file.

- [ ] **Step 5: Implement sequential final-turn persistence**

`RealtimePersistence` exposes `finalize_user(text, audio_path, input_kind)`,
`append_assistant_text(generation, text)`,
`mark_assistant_text_done(generation)`,
`append_assistant_pcm(generation, pcm)`,
`mark_assistant_audio_done(generation, audio_path)`,
`interrupt_assistant(generation)`, `drain(timeout_seconds)`, and `abort()`.
`input_kind` is exactly `"audio" | "text"`; text input persists the supplied
Chinese text as a real user turn with `source_audio_path=None` and does not
fabricate ASR. One async persistence tail preserves conversational order while
Qwen requests run without blocking the upstream receive loop.

Assistant state has independent text-done and audio-done latches because Chat
559 may arrive before any TTS 352/359. Persist an assistant generation exactly
once only when both latches are set; 352 appends audio to the open safe sink,
559 only closes text, and 359 finalizes audio and sets audio-done. Interruption
marks the generation interrupted, rejects later chunks, and finalizes or
aborts its owned sink according to whether complete audio exists.

The persisted mapping is exact:

```python
repository.add_turn(
    session_id,
    speaker="tester",
    source_language="zh",
    target_language="en",
    source_text=final_asr,
    translated_text=translated_or_empty,
    source_audio_path=str(user_wav) if user_wav else None,
    model="doubao_asr;translation_model=qwen-flash",
    latency_ms=translation_latency_ms,
    interrupted=False,
)
```

For assistant turns use `speaker="agent"`, source `en`, target `zh`, and the
English response WAV as `source_audio_path`. If translation fails, insert the
turn and immediately call `mark_turn_error(turn_id, "subtitle_error")`. If
`translator is None`, do not schedule translation, leave
`translated_text=""`, omit the `translation_model=...` suffix, and do not mark
an error; report subtitles as disabled to the browser. A bounded
`drain(timeout_seconds)` either completes the tail or raises a fixed timeout;
`abort()` cancels/awaits the tail and aborts every incomplete owned sink.
Gateway hangup must await this drain/finalization before calling
`repository.end_session()`, with `abort()` as the bounded-timeout fallback.

- [ ] **Step 6: Run storage tests**

```bash
.venv/bin/python -m pytest tests/test_realtime_audio.py tests/test_realtime_persistence.py tests/test_storage.py -q
```

Expected: all tests pass, including existing deletion and traversal protections.

- [ ] **Step 7: Commit Task 6**

```bash
git add app/storage.py app/realtime/audio.py app/realtime/persistence.py tests/test_realtime_audio.py tests/test_realtime_persistence.py
git commit -m "feat: persist realtime voice turns"
```

## Task 7: Build the per-session realtime gateway

**Files:**
- Create: `app/realtime/gateway.py`
- Create: `tests/test_realtime_gateway.py`

- [ ] **Step 1: Write an end-to-end fake gateway test**

The test drives a fake browser socket, fake Doubao client, fake translator, SQLite, and WAV storage. Define deterministic fakes with the same public methods used by the gateway:

```python
import asyncio
import base64
import json
from collections.abc import Callable
from typing import Protocol

import pytest
from starlette.websockets import WebSocketState

from app.config import RealtimeSettings
from app.realtime.doubao_protocol import DoubaoFrame
from app.realtime.gateway import RealtimeGateway
from app.realtime.qwen import SubtitleResult
from app.storage import AudioStore, Repository


def client_event(event_type: str, seq: int, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": event_type,
            "event_id": f"client-{seq}",
            "session_id": None,
            "turn_id": None,
            "seq": seq,
            "ts_ms": 1785600000000 + seq,
            "payload": payload,
        },
        separators=(",", ":"),
    )


def start_event(scenario: str, seq: int) -> str:
    return client_event("session.start", seq, {"scenario_id": scenario})


def audio_event(audio: bytes, seq: int) -> str:
    return client_event(
        "input_audio.append",
        seq,
        {
            "encoding": "pcm_s16le",
            "sample_rate_hz": 16000,
            "channels": 1,
            "duration_ms": 40,
            "audio_b64": base64.b64encode(audio).decode(),
        },
    )


def commit_event(seq: int) -> str:
    return client_event("input_audio.commit", seq, {})


def text_event(text: str, seq: int) -> str:
    return client_event("user.text.submit", seq, {"text": text})


def end_event(seq: int) -> str:
    return client_event("session.end", seq, {})


def upstream_event(event: int, payload: dict[str, object]) -> DoubaoFrame:
    return DoubaoFrame(0x9, 0x4, event, "upstream-session", payload)


def upstream_audio(payload: bytes) -> DoubaoFrame:
    return DoubaoFrame(0xB, 0x4, 352, "upstream-session", payload)


class BrowserSocketProtocol(Protocol):
    client_state: WebSocketState

    async def receive_text(self) -> str: ...
    async def send_json(self, event: dict[str, object]) -> None: ...
    async def close(self, code: int, reason: str = "") -> None: ...


class DoubaoClientProtocol(Protocol):
    async def connect(self, *, send_greeting: bool = False) -> None: ...
    async def say_hello(self, text: str) -> None: ...
    async def send_audio(self, audio: bytes) -> None: ...
    async def end_asr(self) -> None: ...
    async def send_text_query(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def receive(self) -> DoubaoFrame: ...
    async def finish(self) -> None: ...


DoubaoClientFactory = Callable[[str], DoubaoClientProtocol]


class FakeBrowserSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.changed = asyncio.Event()
        self.client_state = WebSocketState.CONNECTED
        self.close_code: int | None = None

    def push(self, event: str) -> None:
        self.incoming.put_nowait(event)

    async def receive_text(self) -> str:
        return await self.incoming.get()

    async def send_json(self, event: dict[str, object]) -> None:
        self.sent.append(event)
        self.changed.set()

    async def wait_for_type(self, event_type: str, count: int = 1) -> None:
        while sum(item["type"] == event_type for item in self.sent) < count:
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), timeout=1)

    async def close(self, code: int, reason: str = "") -> None:
        del reason
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED


class FakeDoubaoClient:
    def __init__(
        self,
        *,
        after_hello: list[DoubaoFrame] | None = None,
        after_end_asr: list[DoubaoFrame],
        after_text_query: list[DoubaoFrame] | None = None,
    ) -> None:
        self.events: asyncio.Queue[DoubaoFrame] = asyncio.Queue()
        self.after_hello = after_hello or []
        self.after_end_asr = after_end_asr
        self.after_text_query = after_text_query or []
        self.audio: list[bytes] = []
        self.commands: list[str] = []

    async def connect(self, *, send_greeting: bool = False) -> None:
        assert send_greeting is False
        self.commands.append("connect")

    async def say_hello(self, text: str) -> None:
        assert text.startswith("Hello")
        self.commands.append("hello")
        for event in self.after_hello:
            self.events.put_nowait(event)

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def end_asr(self) -> None:
        self.commands.append("end_asr")
        for event in self.after_end_asr:
            self.events.put_nowait(event)

    async def send_text_query(self, text: str) -> None:
        self.commands.append(f"text:{text}")
        for event in self.after_text_query:
            self.events.put_nowait(event)

    async def interrupt(self) -> None:
        self.commands.append("interrupt")

    async def receive(self) -> DoubaoFrame:
        return await self.events.get()

    async def finish(self) -> None:
        self.commands.append("finish")


class FakeTranslator:
    def __init__(self, results: dict[tuple[str, str, str], str]) -> None:
        self.results = results

    async def translate(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> SubtitleResult:
        translated = self.results[(text, source_language, target_language)]
        return SubtitleResult(text=translated, model="qwen-flash")

    async def aclose(self) -> None:
        return None


def enabled_settings() -> RealtimeSettings:
    return RealtimeSettings(
        provider="doubao",
        app_id="test-app",
        access_key="test-access",
        app_key="test-app-key",
        speaker="en_female_dacey_uranus_bigtts",
        subtitle_enabled=True,
        dashscope_api_key="test-qwen",
        max_message_bytes=131_072,
        max_buffer_bytes=1_048_576,
        max_upstream_frame_bytes=262_144,
        max_decompressed_bytes=262_144,
    )


def make_gateway(
    tmp_path,
    *,
    doubao: FakeDoubaoClient,
    translator: FakeTranslator | None,
) -> RealtimeGateway:
    repository = Repository(tmp_path / "sessions.db")
    audio_store = AudioStore(tmp_path / "audio")
    return RealtimeGateway(
        settings=enabled_settings(),
        repository=repository,
        audio_store=audio_store,
        doubao_factory=lambda session_id: doubao,
        translator=translator,
        scenarios={"product_intro": "Hello, this is the customer care team."},
    )
```

`enabled_settings()` uses O2.0 model `1.2.1.1`, `push_to_talk`, and finite
raw/decompressed/message/buffer limits.
`start_event`, `audio_event`, `commit_event`, `text_event`, and `end_event`
return raw JSON text for the version-1 envelopes from Task 5 with monotonically
increasing client sequence values. `upstream_event(event, payload)` returns
`DoubaoFrame(0x9, 0x4, event, "upstream-session", payload)`, while
`upstream_audio(payload)` uses message type `0xB`, event 352, and raw bytes.
The fake deliberately enqueues successful response events only after
`end_asr()` or `send_text_query()`; construction must not preload them and make
the orchestration pass without a committed input.

The end-to-end assertion is:

```python
@pytest.mark.asyncio
async def test_chinese_audio_produces_bilingual_turns_and_english_audio(tmp_path) -> None:
    browser = FakeBrowserSocket()
    doubao = FakeDoubaoClient(
        after_end_asr=[
            upstream_event(450, {}),
            upstream_event(451, {"results": [{"text": "你好", "is_interim": False}]}),
            upstream_event(459, {}),
            upstream_event(550, {"content": "Hello."}),
            upstream_event(559, {}),
            upstream_event(350, {}),
            upstream_audio(b"\x00\x00" * 120),
            upstream_event(359, {}),
        ]
    )
    translator = FakeTranslator({("你好", "zh", "en"): "Hello", ("Hello.", "en", "zh"): "你好。"})
    gateway = make_gateway(tmp_path, doubao=doubao, translator=translator)
    browser.push(start_event("product_intro", seq=1))
    browser.push(audio_event(bytes(1280), seq=2))
    browser.push(commit_event(seq=3))
    running = asyncio.create_task(gateway.run(browser))
    await browser.wait_for_type("turn.completed", count=2)
    browser.push(end_event(seq=4))
    await asyncio.wait_for(running, timeout=1)
    session = gateway.repository.list_sessions()[0]
    complete = gateway.repository.get_session(session["id"])
    assert [(turn["speaker"], turn["source_text"], turn["translated_text"]) for turn in complete["turns"]] == [
        ("tester", "你好", "Hello"),
        ("agent", "Hello.", "你好。"),
    ]
    assert any(event["type"] == "assistant.audio.chunk" for event in browser.sent)
```

Add an equally complete `user.text.submit` test: it pushes `session.start`, then
`text_event("你好", seq=2)`, verifies `send_text_query()` is called, releases the
fake's text response frames, observes both persisted bilingual turns, and ends
with seq 3. Also test Qwen failure not stopping audio, 559-before-352/359,
an English `SayHello` response flowing before the first user input,
cancel-before-late-audio and new-boundary recovery, raw-message/frame/backpressure
close codes, normal completion cancelling sibling loops, client disconnect
cleanup, upstream error sanitization, `ping` producing exactly one `pong`,
maximum-session timeout cleanup, connect failure ending the created repository
session, and two independent sessions.

The normalizer gets separate fixture tests using sanitized exact bytes from
current official examples. Those fixtures—not hand-enriched fake payloads—are
the authority for fields and delta/snapshot behavior. Normalize 451 final or
partial text from its verified results schema; 459 closes the current local
user turn; 550 applies its verified delta-or-snapshot rule to a local text
accumulator; 559 sets text-done; 352 is raw PCM for the active local generation;
and 359 sets audio-done. Events 450, 550, 559, 350, and 352 must never be
assumed to contain `question_id` or `reply_id`. If a provider ID is ever used,
the corresponding sanitized official byte fixture must first prove it.

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_realtime_gateway.py -q
```

Expected: import failure because the gateway does not exist.

- [ ] **Step 3: Implement bounded orchestration**

The constructor and dependency types are exact:

```python
from collections.abc import Mapping
from dataclasses import dataclass


class RealtimeGateway:
    def __init__(
        self,
        *,
        settings: RealtimeSettings,
        repository: Repository,
        audio_store: AudioStore,
        doubao_factory: DoubaoClientFactory,
        translator: SubtitleTranslator | None,
        scenarios: Mapping[str, str],
    ) -> None: ...
```

The factory receives the newly created local repository session ID and returns
one fresh `DoubaoClientProtocol`. Production constructs the real client with
that session ID; tests return their per-test fake. `RealtimeGateway.run()`
accepts `BrowserSocketProtocol` and performs this sequence:

1. Receive raw text and validate exactly one `session.start` event with the
   Task 5 byte/message parser, then accept its sequence in `RealtimeState`.
2. Create a repository session with `provider_mode="doubao"`.
3. Create/connect the upstream client; its sole internal reader resolves
   ConnectionStarted 50 and SessionStarted 150 acknowledgement futures.
4. Send `session.ready`, then trigger the scenario-specific English `SayHello`.
5. Run browser receive, decoded-upstream receive, and browser-send loops inside
   one `asyncio.TaskGroup`.
6. Make the send loop the only `websocket.send_json()` owner. Producers use a
   bounded outgoing queue with an actual decoded-audio byte counter.
7. Feed validated browser audio once to the safe input segmenter and once to
   the Doubao client, which reframes/pads transport packets to exactly 640 bytes.
8. Map `input_audio.commit` to Doubao EndASR event 400.
9. Map `user.text.submit` only through the fixture-verified ChatTextQuery event
   501 adapter, persist the supplied Chinese text as a final user turn with no
   audio, and translate it without fabricating ASR.
10. Convert upstream ASR/Chat/TTS frames through the fixture-backed normalizer
    and the local turn/generation state described below.
11. Send base64 `assistant.audio.chunk` immediately, before waiting for Qwen.
12. Map `response.cancel`/new push-to-talk interruption to at most one
    ClientInterrupt 515 before new audio and invalidate the old generation.
13. On hangup/disconnect, terminate sibling loops, finish upstream with its
   102/152/2/52 acknowledgement flow, drain/finalize persistence, then end the
   SQLite session exactly once. Use abort only after the bounded drain fails.
14. Wrap the active TaskGroup in `asyncio.timeout(max_session_seconds)` and map
    expiry to one safe terminal error. If a repository session was created,
    every later connect/protocol/timeout path ends it exactly once even when no
    turn was persisted. An accepted `ping` bypasses upstream and enqueues one
    `pong` through the normal bounded send loop.

Use a concrete outgoing item, queue, and counter; there is no free variable
named `queued_audio_bytes`:

```python
@dataclass(frozen=True)
class OutgoingItem:
    event: dict[str, object]
    audio_bytes: int = 0


class GatewayOverload(RuntimeError):
    pass


async def _enqueue(self, event: dict[str, object], *, audio_bytes: int = 0) -> None:
    async with self._outgoing_lock:
        if (
            self._outgoing.full()
            or self._queued_audio_bytes + audio_bytes > self.settings.max_buffer_bytes
        ):
            raise GatewayOverload("browser output queue is full")
        self._outgoing.put_nowait(OutgoingItem(event, audio_bytes))
        self._queued_audio_bytes += audio_bytes


async def _browser_send_loop(self, websocket: BrowserSocketProtocol) -> None:
    while True:
        item = await self._outgoing.get()
        try:
            await websocket.send_json(item.event)
        finally:
            async with self._outgoing_lock:
                self._queued_audio_bytes -= item.audio_bytes
            self._outgoing.task_done()
```

Construct `_outgoing` as `asyncio.Queue(maxsize=128)` (or a smaller tested
positive setting-derived bound), initialize `_queued_audio_bytes = 0`, and
count the decoded PCM byte length for every queued audio event. A full item
queue also overloads even when it contains only text/control events.

`asyncio.TaskGroup` does not cancel siblings when one task returns normally.
Wrap each long-running loop so a normal return raises a private
`_SessionStopped` sentinel, catch it with `except* _SessionStopped`, and let the
TaskGroup cancel/await the other loops. Unexpected errors still propagate to
the sanitized failure path. The gateway's upstream loop calls only
`doubao.receive()` (the decoded bounded queue); the client's internal reader is
the sole owner of raw `recv()`, including lifecycle acknowledgements.

```python
class _SessionStopped(Exception):
    pass


async def _stop_when_done(awaitable) -> None:
    await awaitable
    raise _SessionStopped


try:
    async with asyncio.TaskGroup() as group:
        group.create_task(_stop_when_done(self._browser_receive_loop(websocket)))
        group.create_task(_stop_when_done(self._upstream_receive_loop()))
        group.create_task(_stop_when_done(self._browser_send_loop(websocket)))
except* _SessionStopped:
    pass
```

Apply response attribution exactly as follows:

- 451 updates/emits verified ASR partial/final text for the current local user
  turn; 459 finalizes that turn and calls `state.complete_input_turn()`.
- A validated text submit finalizes the local user turn and also arms the next
  response boundary.
- The first verified 550 or 350 boundary opens one new local
  `response_generation`; further events for that response reuse it. Accumulate
  550 using the fixture-proven delta/snapshot rule, and let 559 set only the
  text-done latch.
- Convert the internal generation to `state.response_id(generation)` on every
  browser text/audio/translation/cancel event; never expose or require a
  provider reply identifier.
- Assign raw 352 PCM only to the active, non-cancelled generation, increment a
  local chunk sequence, enqueue it immediately, and append it to that
  generation's sink. Never read a `reply_id` from 352.
- Event 359 closes the active generation's audio latch. Persistence inserts
  the assistant turn only after both 559 and 359 latches are complete.
- After sending 515, invalidate the old generation and discard every late 352.
  Do not open another generation until the new input completes and a later
  valid 550/350 boundary arrives; 359 only closes the generation that is
  currently active.

Every raw browser message is bounded before JSON/base64 parsing. Protocol
violations close with 1008, oversized messages/frames with 1009, temporary
overload with 1013, and internal/upstream failures with 1011 after one
sanitized `error` event where possible. Never include provider payloads,
headers, URLs, exception representations, or credentials in the browser error.

- [ ] **Step 4: Run gateway tests**

```bash
.venv/bin/python -m pytest tests/test_realtime_gateway.py -q
```

Expected: all fake end-to-end gateway tests pass with no cloud access.

- [ ] **Step 5: Commit Task 7**

```bash
git add app/realtime/gateway.py tests/test_realtime_gateway.py
git commit -m "feat: orchestrate realtime voice sessions"
```

## Task 8: Expose the FastAPI WebSocket route and health capability

**Files:**
- Modify: `app/main.py`
- Modify: `app/storage.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_storage.py`
- Create: `tests/test_realtime_api.py`

- [ ] **Step 1: Write route, Origin, and health tests**

```python
def test_health_reports_realtime_capability(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "provider_mode": "mock",
        "realtime_provider": "disabled",
        "subtitles": "disabled",
    }


def test_realtime_route_rejects_unapproved_origin(realtime_app) -> None:
    with TestClient(realtime_app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                "/ws/realtime", headers={"origin": "https://untrusted.example"}
            ):
                pass
    assert closed.value.code == 1008
```

Define the fixture in the same test module; do not leave `realtime_app`
implicit:

```python
def enabled_settings() -> RealtimeSettings:
    return RealtimeSettings(
        provider="doubao",
        app_id="test-app",
        access_key="test-access",
        app_key="test-app-key",
        speaker="en_female_dacey_uranus_bigtts",
        subtitle_enabled=False,
        max_concurrent_sessions=1,
    )


class StubGateway:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def run(self, websocket: WebSocket) -> None:
        start = json.loads(await websocket.receive_text())
        session = self.repository.create_session(
            start["payload"]["scenario_id"], "doubao"
        )
        await websocket.send_json(
            server_event(
                "session.ready",
                session_id=session["id"],
                seq=1,
                payload={
                    "scenario_id": session["scenario_id"],
                    "provider": "doubao",
                    "created_at": session["created_at"],
                },
            )
        )
        await websocket.receive_text()
        self.repository.end_session(session["id"])


@pytest.fixture
def realtime_app(tmp_path) -> FastAPI:
    settings = Settings(
        provider_mode="mock",
        database_path=tmp_path / "sessions.db",
        audio_dir=tmp_path / "audio",
        ark_api_key=None,
        ark_model=None,
        ark_base_url="https://ark.example.test/api/v3",
        realtime=enabled_settings(),
    )
    repository = Repository(settings.database_path)
    return create_app(
        settings,
        repository=repository,
        audio_store=AudioStore(settings.audio_dir),
        realtime_gateway_factory=lambda: StubGateway(repository),
    )
```

Add an approved-Origin WebSocket integration test that sends `session.start`,
receives `session.ready`, sends `session.end`, and verifies the returned
`session_id` through the existing REST review endpoint. Add a test that holds
one injected gateway open and verifies a connection beyond
`max_concurrent_sessions` closes with 1013 rather than waiting indefinitely.

Add safe source-WAV replay tests. Store a WAV below one session, request
`GET /audio/{session_id}/{filename}`, and assert `audio/wav` plus exact bytes.
Traversal, wrong-session, directory, oversized, and symlink targets return 404;
after session deletion the same URL returns 404. No route may serve arbitrary
extensions or a path outside `AudioStore.root`.

- [ ] **Step 2: Run and verify failure**

```bash
.venv/bin/python -m pytest tests/test_api.py tests/test_realtime_api.py -q
```

Expected: health assertion and WebSocket route tests fail.

- [ ] **Step 3: Add dependency injection and route wiring**

Extend `create_app()` with optional realtime gateway factory and subtitle
translator parameters. The gateway factory returns a fresh per-connection
gateway while sharing the app-owned Repository/AudioStore. When no test double
is supplied:

- create a Doubao factory only if `settings.realtime.enabled`;
- create Qwen translator only if `subtitle_enabled`;
- keep both disabled for default Mock tests;
- close the owned Qwen `httpx.AsyncClient` during lifespan shutdown.
- guard entry with an app-owned bounded admission counter using
  `settings.realtime.max_concurrent_sessions`; always release its slot in
  `finally`.

Add:

```python
@app.websocket("/ws/realtime")
async def realtime_voice(websocket: WebSocket) -> None:
    await websocket.accept()
    origin = websocket.headers.get("origin")
    if origin not in resolved_settings.realtime.allowed_origins:
        await websocket.close(code=1008, reason="origin is not allowed")
        return
    if not resolved_settings.realtime.enabled:
        await websocket.close(code=1013, reason="realtime voice is disabled")
        return
    gateway = realtime_gateway_factory()
    if not await realtime_admission.try_acquire():
        await websocket.close(code=1013, reason="realtime capacity is full")
        return
    try:
        await gateway.run(websocket)
    finally:
        await realtime_admission.release()
```

Return only safe capability labels from `/api/health`; never return upstream URL, app ID, keys, headers, or full speaker entitlement metadata.

Add `AudioStore.read_audio_file(session_id, filename) -> bytes`. It validates
the existing safe ID and WAV filename rules, opens the root/session/file with
directory-relative descriptors and `O_NOFOLLOW`, verifies a regular file with
`fstat`, reads at most `max_bytes + 1`, rejects oversize, and closes all
descriptors. The FastAPI route returns `Response(content=data,
media_type="audio/wav")`; it maps every unsafe/missing path to the existing
non-revealing 404. This supports replaying both saved user and assistant source
WAV files without `FileResponse` symlink/TOCTOU exposure.

- [ ] **Step 4: Run API and full backend tests**

```bash
.venv/bin/python -m pytest tests/test_api.py tests/test_realtime_api.py tests/test_storage.py -q
.venv/bin/python -m pytest -q
```

Expected: both commands pass.

- [ ] **Step 5: Commit Task 8**

```bash
git add app/main.py app/storage.py tests/test_api.py tests/test_realtime_api.py tests/test_storage.py
git commit -m "feat: expose realtime voice websocket"
```

## Task 9: Implement browser PCM capture and streaming playback

**Files:**
- Create: `app/static/pcm.js`
- Create: `app/static/pcm-worklet.js`
- Create: `app/static/realtime-audio.js`
- Create: `tests/js/pcm.test.mjs`
- Create: `tests/js/realtime-audio.test.mjs`
- Modify: `tests/test_static_contract.py`

- [ ] **Step 1: Write Node tests for PCM conversion and audio lifecycle**

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import {
  StreamingResampler,
  floatToPcm16LeBytes,
  pcm16LeBytesToFloat,
} from "../../app/static/pcm.js";

test("float conversion clamps and writes exact little-endian bytes", () => {
  assert.deepEqual(
    Array.from(floatToPcm16LeBytes(new Float32Array([-2, 0, 1]))),
    [0, 128, 0, 0, 255, 127],
  );
});

test("PCM decoding accepts an unaligned byte view", () => {
  const bytes = Uint8Array.from([99, 0, 128, 0, 0, 255, 127]).subarray(1);
  const floats = pcm16LeBytesToFloat(bytes);
  assert.equal(floats.length, 3);
  assert.ok(floats[0] <= -1);
  assert.ok(floats[2] > 0.99);
});

test("streaming resampling preserves phase across render quanta", () => {
  const resampler = new StreamingResampler(48000, 16000);
  const first = resampler.push(new Float32Array(128));
  const second = resampler.push(new Float32Array(352));
  assert.equal(first.length + second.length, 160);
});
```

Create `tests/js/realtime-audio.test.mjs` with injected fake AudioContext,
AudioWorkletNode, and mediaDevices. Cover microphone denial with output still
usable, capture reset without pre-roll, final chunk before stop resolves,
ordered 24kHz playback, the scheduled-duration queue bound, cancel, mute,
partial-start cleanup, and idempotent full cleanup.

- [ ] **Step 2: Run and verify failure**

```bash
node --test tests/js/pcm.test.mjs tests/js/realtime-audio.test.mjs
```

Expected: module-not-found failures for the two production modules.

- [ ] **Step 3: Implement pure PCM helpers**

`pcm.js` exports `floatToPcm16LeBytes`, `pcm16LeBytesToFloat`,
`StreamingResampler`, `bytesToBase64`, and `base64ToBytes`. PCM conversion uses
`DataView.setInt16(..., true)` / `getInt16(..., true)` so little-endian order
and unaligned byte views are explicit. Reject empty input, odd PCM byte counts,
and invalid rates. Base64 helpers process bounded slices rather than spreading
large arrays into one call.

Use this exact resampling contract:

```javascript
export class StreamingResampler {
  constructor(inputRate, outputRate) {
    if (inputRate <= 0 || outputRate <= 0) {
      throw new TypeError("positive sample rates are required");
    }
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    this.phase = 0;
    this.previous = null;
  }

  push(input) {
    if (!(input instanceof Float32Array) || input.length === 0) {
      throw new TypeError("non-empty Float32 input is required");
    }
    return this._consume(input);
  }
}
```

`_consume()` carries the final source sample and fractional phase across calls.
Tests at both 44.1kHz and 48kHz compare arbitrarily split input with one
continuous stream, including output count and values within `1e-5`.

- [ ] **Step 4: Implement AudioWorklet capture and bounded playback**

`pcm-worklet.js` registers `realtime-pcm-capture`. It buffers native-rate mono
floats, uses one persistent resampler, and posts 40ms/1280-byte PCM16LE chunks
as transferable `ArrayBuffer` values. It never connects microphone audio to
the destination. `{type: "capture.start", generation}` clears pre-roll;
`{type: "capture.stop", generation, flush}` emits the last complete chunk when
requested, discards an incomplete tail, and only then posts
`{type: "capture.stopped", generation}`.

`realtime-audio.js` exposes one class:

```javascript
export class RealtimeAudio {
  constructor({
    onChunk,
    onPlaybackError,
    maxScheduledSeconds = 3,
    AudioContextClass = globalThis.AudioContext,
    AudioWorkletNodeClass = globalThis.AudioWorkletNode,
    mediaDevices = globalThis.navigator?.mediaDevices,
    workletUrl = "/static/pcm-worklet.js",
  }) {
    this.onChunk = onChunk;
    this.onPlaybackError = onPlaybackError;
    this.maxScheduledSeconds = maxScheduledSeconds;
    this.AudioContextClass = AudioContextClass;
    this.AudioWorkletNodeClass = AudioWorkletNodeClass;
    this.mediaDevices = mediaDevices;
    this.workletUrl = workletUrl;
    this.context = null;
    this.stream = null;
    this.mediaSource = null;
    this.captureNode = null;
    this.capturing = false;
    this.sources = new Set();
    this.nextStartTime = 0;
  }

  async initializeOutput() {
    this.context = new this.AudioContextClass();
    await this.context.resume();
  }

  async prepareCapture() {
    await this.context.audioWorklet.addModule(this.workletUrl);
    this.stream = await this.mediaDevices.getUserMedia({
      audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
    });
    this.mediaSource = this.context.createMediaStreamSource(this.stream);
    this.captureNode = new this.AudioWorkletNodeClass(this.context, "realtime-pcm-capture");
    this.captureNode.port.onmessage = (event) => {
      if (this.capturing) this.onChunk(new Uint8Array(event.data));
    };
    this.mediaSource.connect(this.captureNode);
  }

  enqueuePcm24k(bytes) {
    const samples = pcm16LeBytesToFloat(bytes);
    const buffer = this.context.createBuffer(1, samples.length, 24000);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const startAt = Math.max(this.context.currentTime + 0.03, this.nextStartTime);
    if (startAt + buffer.duration - this.context.currentTime > this.maxScheduledSeconds) {
      this.onPlaybackError?.(new Error("audio playback queue is full"));
      return false;
    }
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    return true;
  }
}
```

Add `beginCapture()`, `endCapture({flush = true})`, `cancelPlayback()`,
`setMuted()`, a `hasPendingPlayback` getter, and idempotent `stop()`.
`beginCapture()` sends `capture.start`; `endCapture()` resolves only after the
worklet acknowledgement, so all final `onChunk` calls precede commit. A denied
microphone returns `captureAvailable=false` without closing output playback.
Queue-full is routed through `onPlaybackError`, not thrown from an event
callback. Cleanup disconnects the saved media source and node, stops tracks and
scheduled sources, and closes the context.

- [ ] **Step 5: Update static contract tests**

Make `tests/test_static_contract.py` require `pcm.js`, `pcm-worklet.js`, and `realtime-audio.js`; require `AudioWorkletNode`, 16000, 24000, and media-track cleanup. Keep current Web Speech assertions only for the explicitly retained REST fallback in `speech.js`. Add the `bufferedAmount` contract in Task 10 after `realtime.js` exists.

- [ ] **Step 6: Run JS and static tests**

```bash
node --test tests/js/pcm.test.mjs tests/js/realtime-audio.test.mjs
.venv/bin/python -m pytest tests/test_static_contract.py -q
node --check app/static/pcm.js
node --check app/static/pcm-worklet.js
node --check app/static/realtime-audio.js
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 9**

```bash
git add app/static/pcm.js app/static/pcm-worklet.js app/static/realtime-audio.js tests/js/pcm.test.mjs tests/js/realtime-audio.test.mjs tests/test_static_contract.py
git commit -m "feat: stream browser PCM audio"
```

## Task 10: Add the browser realtime client and integrate the call UI

**Files:**
- Create: `app/static/realtime.js`
- Create: `app/static/realtime-ui-state.js`
- Create: `tests/js/realtime.test.mjs`
- Create: `tests/js/realtime-ui-state.test.mjs`
- Modify: `app/static/app.js`
- Modify: `app/static/index.html`
- Modify: `app/static/styles.css`
- Modify: `tests/test_static_contract.py`

- [ ] **Step 1: Write realtime client ordering tests**

Make `RealtimeClient` accept an injected WebSocket constructor so Node tests can use a fake:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { RealtimeClient } from "../../app/static/realtime.js";

test("connect waits for open and ignores stale or duplicate events", async () => {
  const received = [];
  const sockets = [];
  class FakeSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    triggerOpen() { this.readyState = FakeSocket.OPEN; this.onopen(); }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.({code: 1000}); }
  }
  const client = new RealtimeClient({
    url: "ws://localhost/ws/realtime",
    WebSocketClass: FakeSocket,
    onEvent: (event) => received.push(event),
    idFactory: () => "event-id",
  });
  const firstConnect = client.connect();
  sockets[0].triggerOpen();
  await firstConnect;
  const oldSocket = sockets[0];
  const secondConnect = client.connect();
  sockets[1].triggerOpen();
  await secondConnect;
  oldSocket.onmessage({data: JSON.stringify({v: 1, type: "asr.partial", seq: 1, payload: {text: "old"}})});
  sockets[1].onmessage({data: JSON.stringify({v: 1, type: "session.ready", session_id: "session-1", seq: 1, payload: {created_at: "now"}})});
  sockets[1].onmessage({data: JSON.stringify({v: 1, type: "asr.partial", session_id: "session-1", turn_id: "turn-1", seq: 2, payload: {text: "new"}})});
  sockets[1].onmessage({data: JSON.stringify({v: 1, type: "asr.partial", seq: 1, payload: {text: "duplicate"}})});
  assert.deepEqual(received.map((event) => event.type), ["session.ready", "asr.partial"]);
});
```

Add tests for open timeout, close-before-open rejection, the exact
`sendAudio()` and `commitAudio()` envelopes, session binding and mismatches,
malformed JSON, stale connection generations, monotonic sequence checks,
`cancelResponse()` sequencing, heartbeat cleanup, `bufferedAmount`
backpressure, and callbacks ignored after `close()`.

Create `tests/js/realtime-ui-state.test.mjs` before production code. Its pure
reducer tests ASR partial replacement/finalization, assistant delta assembly,
late translations, cancelled response text/audio rejection, monotonic audio
chunk sequence, and `turn.completed` replacement of provisional state. It also
asserts that assistant streaming never disables microphone or text barge-in.

- [ ] **Step 2: Run and verify failure**

```bash
node --test tests/js/realtime.test.mjs tests/js/realtime-ui-state.test.mjs
```

Expected: module-not-found failures for both new modules.

- [ ] **Step 3: Implement the WebSocket client**

`RealtimeClient` is constructed with `{url, WebSocketClass, onEvent, onState,
maxBufferedBytes, heartbeatMs, openTimeoutMs, idFactory}`. It owns a connection
generation, client sequence, last accepted server sequence, heartbeat timer,
and `bufferedAmount` bound. It exposes:

```javascript
connect(): Promise<void>
startSession(scenarioId)
sendAudio(bytes): boolean
commitAudio()
submitText(text)
cancelResponse(responseId)
endSession({timeoutMs = 2000}): Promise<object | null>
close()
```

`connect()` resolves only when that generation reaches OPEN and rejects on
timeout or close. Sending before OPEN is an error and no implicit queue exists.
`session.ready` binds the top-level `session_id`; every later event must match
it and have a strictly increasing server sequence. Reconnect resets session and
both sequences. Backpressure prevents `socket.send()` and returns `false`.

`sendAudio()` uses `bytesToBase64()` and sends:

```javascript
{
  v: 1,
  type: "input_audio.append",
  event_id: crypto.randomUUID(),
  session_id: this.sessionId,
  turn_id: null,
  seq: this.nextClientSeq(),
  ts_ms: Date.now(),
  payload: {
    encoding: "pcm_s16le",
    sample_rate_hz: 16000,
    channels: 1,
    duration_ms: 40,
    audio_b64: bytesToBase64(bytes),
  },
}
```

Update `tests/test_static_contract.py` in this task to require `realtime.js`, `bufferedAmount`, connection-generation checks, `response.cancel`, and safe `textContent` rendering.

Implement `realtime-ui-state.js` as the tested pure reducer. Normalized server
payloads are fixed: `session.ready={scenario_id,provider,created_at}`;
`asr.*={text}` with a top-level
turn ID; `assistant.text.*={response_id,delta|text}`;
`assistant.audio.chunk={response_id,chunk_seq,encoding:"pcm_s16le",
sample_rate_hz:24000,channels:1,audio_b64}`; and
`turn.completed={turn:<existing REST turn shape>}`.

- [ ] **Step 4: Integrate realtime mode into `app.js`**

Keep existing REST functions for history/review and the disabled-realtime fallback. Add `state.realtime`, `state.realtimeAudio`, `state.realtimeEnabled`, and provisional subtitle cards keyed by `turn_id`.

The start flow is exact:

1. Fetch `/api/health` during initialization and set `state.realtimeEnabled` from `realtime_provider === "doubao"`.
2. When realtime is disabled, call the existing `beginCall()` REST path unchanged.
3. In the realtime start-button handler, `await audio.initializeOutput()` inside
   the user gesture, attempt `prepareCapture()` without destroying playback on
   permission failure, then `await client.connect()` and call `startSession()`.
   A realtime connection failure cleans both objects and never falls back to REST.
4. On `session.ready`, save the returned session ID, set connected state, start the timer, and enable the microphone button without sending audio yet.
5. On the first microphone click, only when `activeResponseId` and
   `audio.hasPendingPlayback` are both present, call `cancelPlayback()` and then
   `cancelResponse(activeResponseId)`; next call `beginCapture()` and forward
   every 40ms chunk. If `sendAudio()` reports backpressure, stop capture, show a
   bounded error, and end the realtime session rather than silently dropping PCM.
6. On the second microphone click, `await endCapture({flush: true})` and only
   then call `commitAudio()`; keep the connection open for the next turn.
7. Render `asr.partial` as the light provisional user line, replace it on `asr.final`, and attach `user.translation.done` when available.
8. Append `assistant.text.delta`, attach `assistant.translation.done`, and play each `assistant.audio.chunk` immediately.
9. On `turn.completed`, replace the provisional card with the existing safe `renderTurn()` representation.
10. Reject text and audio for reducer-cancelled response IDs; push-to-talk click,
    not an ASR event, is the only barge-in trigger in this MVP.
11. Realtime text submission always uses `client.submitText()` and never the
    REST turn endpoint, preventing Mock/Ark results from entering a Doubao session.
12. On hangup, discard an uncommitted capture tail, cancel playback, await
    `endSession({timeoutMs: 2000})`, and clean audio/socket in `finally`; when a
    session ID exists, fetch its existing REST review record.

Replace the existing global `busy` rule in realtime mode with independent
capture, assistant-response, and translation substates. Microphone and text
remain enabled while assistant text/audio is streaming so barge-in works.

Never insert server strings with `innerHTML`; continue using `textContent`.

- [ ] **Step 5: Update UI copy and states**

Update `index.html` and `styles.css` so the live panel states:

- “中文语音输入 → 英文客服原声”；
- Chinese translations are subtitles, not played audio;
- English audio requires an enabled O2.0 voice;
- microphone access and recording consent remain explicit;
- translation cards show `翻译中`, `翻译完成`, or `字幕翻译失败` without hiding English source text.

For a saved turn with `source_audio_path`, render a separate “回放原始录音”
control backed by an HTML `Audio` object and that same-origin safe URL. This
replays the tester's Chinese WAV or agent's English WAV. Keep any existing Web
Speech control labelled as synthetic text reading, separate from source-audio
evidence, and never imply that a live Chinese translated TTS stream exists.
Extend the JS/UI and API tests to prove the safe URL is used only when present,
an absent/404 source disables replay cleanly, and untrusted text still reaches
the DOM only via `textContent`.

- [ ] **Step 6: Update and run frontend contracts**

```bash
node --test tests/js/pcm.test.mjs tests/js/realtime-audio.test.mjs tests/js/realtime.test.mjs tests/js/realtime-ui-state.test.mjs
.venv/bin/python -m pytest tests/test_api.py tests/test_static_contract.py -q
.venv/bin/python -m pytest -q
node --check app/static/api.js
node --check app/static/app.js
node --check app/static/speech.js
node --check app/static/pcm.js
node --check app/static/pcm-worklet.js
node --check app/static/realtime-audio.js
node --check app/static/realtime.js
node --check app/static/realtime-ui-state.js
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 10**

```bash
git add app/static/realtime.js app/static/realtime-ui-state.js app/static/app.js app/static/index.html app/static/styles.css tests/js/realtime.test.mjs tests/js/realtime-ui-state.test.mjs tests/test_static_contract.py
git commit -m "feat: connect call UI to realtime voice"
```

## Task 11: Document, verify, and perform bounded live smoke testing

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-08-02-doubao-e2e-voice-demo-design.md` only if implementation evidence requires a factual correction
- Create: `tests/test_secret_scan.py`

- [ ] **Step 1: Write a repository secret regression test**

The test checks tracked source/config/docs for credential-shaped assignments while allowing empty examples:

```python
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".html", ".css", ".md", ".txt", ".example",
    ".json", ".yaml", ".yml", ".toml", ".ini",
}
CREDENTIAL_NAMES = (
    "DOUBAO_ACCESS_KEY",
    "DOUBAO_APP_KEY",
    "DASHSCOPE_API_KEY",
    "XUNFEI_STT_API_KEY",
    "ARK_API_KEY",
)
SAFE_VALUES = {"", "your-api-key", "test-key", "[redacted]"}
ASSIGNMENT = re.compile(
    rf"(?i)\b({'|'.join(CREDENTIAL_NAMES)})\b\s*[:=]\s*['\"]?([^\s,'\"}}]*)"
)
BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._-]{16,})")


def is_safe_fixture(value: str) -> bool:
    return value in SAFE_VALUES or value.startswith(
        ("test-", "private-test-", "example-", "your-")
    )


def test_tracked_project_text_has_no_live_credentials() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode().split("\0")
    for relative in tracked:
        if not relative:
            continue
        path = ROOT / relative
        if path == Path(__file__).resolve() or path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in ASSIGNMENT.finditer(line):
                assert is_safe_fixture(match.group(2)), (
                    f"credential-shaped assignment in {relative}:{line_number}"
                )
            for match in BEARER.finditer(line):
                assert is_safe_fixture(match.group(1)), (
                    f"credential-shaped bearer token in {relative}:{line_number}"
                )
```

- [ ] **Step 2: Run and verify the secret test before documentation changes**

```bash
.venv/bin/python -m pytest tests/test_secret_scan.py -q
```

Expected: pass. If it fails, remove only the credential-like value introduced by this branch; do not print it.

- [ ] **Step 3: Update the README runbook**

Document three explicit modes:

1. `REALTIME_PROVIDER=disabled`, `PROVIDER_MODE=mock`: offline UI and review regression.
2. `REALTIME_PROVIDER=doubao`, `QWEN_SUBTITLE_ENABLED=true`: real Chinese speech to English speech plus bilingual subtitles.
3. REST Ark mode: retained legacy text-only comparison.

Include this live setup template with empty values and no shell history containing secrets:

```dotenv
REALTIME_PROVIDER=doubao
DOUBAO_APP_ID=
DOUBAO_ACCESS_KEY=
DOUBAO_APP_KEY=
DOUBAO_RESOURCE_ID=volc.speech.dialog
DOUBAO_MODEL=1.2.1.1
DOUBAO_TTS_SPEAKER=en_female_dacey_uranus_bigtts
QWEN_SUBTITLE_ENABLED=true
DASHSCOPE_API_KEY=
QWEN_SUBTITLE_MODEL=qwen-flash
```

Explain that the Access Key field contains the console Access Token, English voices require O2.0 entitlement, old chat-exposed credentials must be rotated, and the app fails explicitly rather than falling back.

- [ ] **Step 4: Run complete automated verification**

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
node --test tests/js/pcm.test.mjs tests/js/realtime-audio.test.mjs tests/js/realtime.test.mjs tests/js/realtime-ui-state.test.mjs
node --check app/static/api.js
node --check app/static/app.js
node --check app/static/speech.js
node --check app/static/pcm.js
node --check app/static/pcm-worklet.js
node --check app/static/realtime-audio.js
node --check app/static/realtime.js
node --check app/static/realtime-ui-state.js
git diff --check
```

Expected: every command exits zero. Record the exact pytest count and warnings in the final handoff.

- [ ] **Step 5: Run the offline browser smoke test**

Start the server in Mock/disabled mode:

```bash
PROVIDER_MODE=mock REALTIME_PROVIDER=disabled .venv/bin/python run.py
```

In the in-app browser, verify start, one text turn, hangup, review, rating, JSON/CSV links, and delete. Confirm console logs contain no uncaught errors.

- [ ] **Step 6: Run the live browser smoke test only with rotated local credentials**

Load the user's local `.env` without showing its contents, start the server, and verify:

1. `session.ready` follows real Doubao SessionStarted event 150.
2. The English SayHello produces ChatResponse 550 and TTSResponse 352.
3. Chinese speech produces ASRResponse 451 and ASREnded 459.
4. The browser plays 24kHz PCM16LE while Qwen translations arrive independently.
5. Pressing the microphone button during playback clears the local queue and sends ClientInterrupt 515 before new audio; ASRInfo 450 confirms the new user turn.
6. Hangup sends FinishSession 102, receives SessionFinished 152, sends
   FinishConnection 2, and receives ConnectionFinished 52.
7. The final REST session contains both source/translation directions and safe
   WAV references; the safe audio endpoint replays the saved Chinese input and
   English output bytes.
8. Browser network payloads, logs, SQLite, exports, and Git contain no credentials.

If rotated credentials or O2.0 voice entitlement are unavailable, mark only this live smoke step as unverified. Do not use the previously exposed values and do not claim live success from fake tests or a successful WebSocket upgrade alone.

- [ ] **Step 7: Commit Task 11**

```bash
git add README.md .env.example tests/test_secret_scan.py docs/superpowers/specs/2026-08-02-doubao-e2e-voice-demo-design.md
git commit -m "docs: add realtime voice runbook"
```

## Final acceptance checklist

- [ ] Existing Mock/Ark REST tests and review/export/delete behavior still pass.
- [ ] Doubao protocol tests use exact official event IDs and frame vectors.
- [ ] Browser sends PCM16LE 16kHz mono and backend reframes to 640-byte packets.
- [ ] Browser receives PCM16LE 24kHz mono and plays chunks in order.
- [ ] English audio playback never waits for Qwen.
- [ ] Only final Chinese/English text is translated and persisted.
- [ ] Cancelled response IDs cannot play late audio.
- [ ] Origin, size, queue, timeout, and lifecycle bounds are tested.
- [ ] All credentials stay in backend environment variables and out of artifacts.
- [ ] Real provider evidence is reported separately from fake/offline evidence.

## Post-review corrective addendum (2026-08-02)

The final whole-branch review found four correctness gaps. Complete these tasks
in order with RED -> GREEN evidence before repeating final acceptance. The
provider fixtures prove no per-reply identifier shared by events 550 and 352;
do not add or trust `reply_id`, `question_id`, a timing window, or any other
unverified attribution field.

### Task 12: Preserve authoritative subtitles for interrupted turns

**Files:**
- Modify: `tests/js/realtime-ui-state.test.mjs`
- Modify: `app/static/realtime-ui-state.js`

- [ ] Add a reducer regression in which `local.response.cancelled` is followed
  by late text/audio, `assistant.translation.done`, and an authoritative
  `turn.completed`. Assert that late text/audio remain rejected, the turn stays
  `interrupted=true`, text/audio status remains `cancelled`, and the persisted
  source plus translation replace empty provisional values.
- [ ] Run `node --test tests/js/realtime-ui-state.test.mjs` and verify the new
  assertions fail because translation is currently discarded.
- [ ] Allow translation completion/unavailable state for a cancelled response
  while continuing to reject text/audio; when `turn.completed` arrives, prefer
  its persisted source and translation without clearing interruption status.
- [ ] Re-run the focused Node test and then the complete Node suite.

### Task 13: Bound successful Qwen output and fail open

**Files:**
- Modify: `tests/test_qwen_subtitles.py`
- Modify: `tests/test_realtime_persistence.py`
- Modify: `app/realtime/qwen.py`

- [ ] Add an adapter test for a 4001-character HTTP 200 completion and an
  integration test that persists the original turn with an empty translation
  and `subtitle_error` after that malformed success.
- [ ] Run both focused tests and verify RED: the adapter accepts the oversized
  result and persistence raises `PersistenceError`.
- [ ] Enforce the existing persisted-text maximum in
  `QwenSubtitleTranslator._completion_content`; map oversized or malformed
  successful content to `SubtitleTranslationError(category="malformed")`.
  Keep translation optional so the original source turn persists safely.
- [ ] Re-run the two focused modules and the realtime persistence suite.

### Task 14: Drain a cancelled provider response before arming another

**Files:**
- Modify: `tests/test_realtime_state.py`
- Modify: `tests/test_realtime_gateway.py`
- Modify: `app/realtime/state.py`
- Modify: `app/realtime/gateway.py`

- [ ] Add RED cases for text and audio interruption. On cancel, retain the old
  generation's `text_done` and `audio_done` latches. During drain, discard old
  550/350/352, accept duplicate 559/359 idempotently, and release the barrier
  only after both terminal markers arrive.
- [ ] Assert text request 501 is sent only after the old 559 and 359. For audio,
  assert 515 precedes new PCM, EndASR 400 waits for the old 559 and 359, and a
  new response is armed only after the new input's 459.
- [ ] Add cases for 559-before-359, 359-before-559, a terminal marker already
  received before cancellation, duplicate markers without binary sequence,
  and a missing marker. A missing marker must hit the configured provider
  receive deadline, send no ambiguous 501/400 or PCM, and end safely; never
  infer attribution from elapsed time.
- [ ] Implement explicit `responding -> cancel_draining -> input_committed ->
  awaiting_new_response -> responding` state. Arm the new boundary before the
  actual 501 send to close the fast-response race. Unknown provider events
  continue to fail closed until a sanitized official fixture proves them.
- [ ] Run focused state/gateway tests, then all realtime Python tests.

### Task 15: Emit successful session termination only after cleanup

**Files:**
- Modify: `tests/test_realtime_gateway.py`
- Modify: `tests/js/realtime.test.mjs`
- Modify: `tests/js/realtime-controller.test.mjs`
- Modify: `app/realtime/gateway.py`
- Modify: `app/static/realtime.js`
- Modify: `app/static/realtime-controller.js`
- Modify: `app/static/app.js`

- [ ] Add a gated RED test proving strict success order:
  `client.finish -> persistence.drain -> completion flush -> repository.end ->
  send session.ended -> close 1000`. Before either gate opens, assert no
  `session.ended` and a still-connected database session.
- [ ] Add failure tests: provider finish timeout emits one sanitized
  `upstream_error`; persistence drain failure aborts owned parts and emits one
  `internal_error`; both close 1011 and never send `session.ended`. External
  cancellation must re-raise the original cancellation, complete exactly-once
  bounded cleanup, and never send a success terminal.
- [ ] Keep the unique send loop alive through terminal flush. Race queue drain
  against sender failure so a broken/blocked browser cannot deadlock cleanup.
  Keep the persistence completion consumer alive until drain and completion
  consumption finish. Browser disconnect performs backend cleanup without a
  terminal send.
- [ ] Move `session.ended` out of `_browser_loop()` into the exactly-once
  finalizer. Only a fully successful provider finish, persistence drain,
  completion flush, repository end, terminal flush sequence may emit it.
- [ ] Update the browser ending path to wait for the backend cleanup budget,
  accept terminal `error` while ending, and treat abnormal/null termination as
  failure instead of review-ready success.
- [ ] Run focused Python and Node lifecycle tests, then their full suites.

### Corrective verification

- [ ] Run `.venv/bin/python -m pytest -q` and record the exact pass/warning count.
- [ ] Run every Node test module and `node --check` every checked-in JavaScript
  source file.
- [ ] Run `.venv/bin/python -m pip check`, Python `compileall`, and
  `git diff --check` against the branch merge base.
- [ ] Repeat the local Mock REST runtime smoke test if lifecycle code changed.
- [ ] Repeat the final whole-branch specification and code-quality reviews.
- [ ] Report browser UI and real Doubao/Qwen cloud verification as UNVERIFIED
  unless each path was exercised with current, rotated credentials and actual
  provider entitlement.
