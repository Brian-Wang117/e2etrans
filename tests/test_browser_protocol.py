"""Validation rules for the browser JSON envelope protocol."""

import base64
import json

import pytest

from app.realtime.browser_protocol import (
    BrowserProtocolError,
    parse_client_event,
    server_event,
)

SCENARIOS = frozenset({"product_intro"})
LIMITS = dict(max_message_bytes=131_072, max_audio_bytes=32_000)


def envelope(event_type, seq, payload, **overrides):
    document = {
        "v": 1,
        "type": event_type,
        "event_id": f"evt-{seq}",
        "session_id": None,
        "turn_id": None,
        "seq": seq,
        "ts_ms": 1_700_000_000_000,
        "payload": payload,
    }
    document.update(overrides)
    return json.dumps(document)


def parse(raw):
    return parse_client_event(raw, allowed_scenarios=SCENARIOS, **LIMITS)


def test_session_start_parses():
    event = parse(envelope("session.start", 1, {"scenario_id": "product_intro"}))
    assert event.type == "session.start"
    assert event.payload["scenario_id"] == "product_intro"


def test_unknown_scenario_is_rejected():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("session.start", 1, {"scenario_id": "nope"}))


def test_session_start_accepts_phone_input_mode():
    event = parse(
        envelope(
            "session.start",
            1,
            {"scenario_id": "product_intro", "input_mode": "audio"},
        )
    )
    assert event.payload["input_mode"] == "audio"


def test_session_start_defaults_to_push_to_talk():
    event = parse(envelope("session.start", 1, {"scenario_id": "product_intro"}))
    assert event.payload.get("input_mode") is None


def test_session_start_rejects_unknown_input_mode():
    with pytest.raises(BrowserProtocolError):
        parse(
            envelope(
                "session.start",
                1,
                {"scenario_id": "product_intro", "input_mode": "video"},
            )
        )


def test_unknown_event_type_is_rejected():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("session.hack", 1, {}))


def test_wrong_version_is_rejected():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("ping", 1, {}, v=2))


def test_invalid_seq_is_rejected():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("ping", 0, {}))
    with pytest.raises(BrowserProtocolError):
        parse(envelope("ping", True, {}))


def test_unknown_payload_keys_are_rejected():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("session.end", 1, {"extra": 1}))


def test_audio_append_parses_and_decodes():
    pcm = b"\x01\x02" * 640
    payload = {
        "encoding": "pcm_s16le",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 40,
        "audio_b64": base64.b64encode(pcm).decode(),
    }
    event = parse(envelope("input_audio.append", 2, payload))
    assert event.audio == pcm


def test_audio_duration_mismatch_is_rejected():
    pcm = b"\x01\x02" * 640
    payload = {
        "encoding": "pcm_s16le",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 20,
        "audio_b64": base64.b64encode(pcm).decode(),
    }
    with pytest.raises(BrowserProtocolError):
        parse(envelope("input_audio.append", 2, payload))


def test_audio_wrong_encoding_is_rejected():
    payload = {
        "encoding": "opus",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 40,
        "audio_b64": base64.b64encode(b"\x00" * 1280).decode(),
    }
    with pytest.raises(BrowserProtocolError):
        parse(envelope("input_audio.append", 2, payload))


def test_oversized_message_flags_oversized():
    raw = envelope("user.text.submit", 1, {"text": "x" * 100}) + " " * 200_000
    with pytest.raises(BrowserProtocolError) as excinfo:
        parse(raw)
    assert excinfo.value.oversized is True


def test_text_bounds_are_enforced():
    with pytest.raises(BrowserProtocolError):
        parse(envelope("user.text.submit", 1, {"text": "   "}))
    with pytest.raises(BrowserProtocolError):
        parse(envelope("user.text.submit", 1, {"text": "x" * 4001}))


def test_server_event_shape():
    event = server_event(
        "asr.partial", session_id="s", seq=7, payload={"text": "hi"}, turn_id="t-1"
    )
    assert event["v"] == 1
    assert event["seq"] == 7
    assert event["event_id"] == "server-7"
    assert event["session_id"] == "s"
    assert event["turn_id"] == "t-1"
    assert event["payload"] == {"text": "hi"}
