"""Byte-exact tests for the Doubao v3 binary framing codec."""

import gzip

import pytest

from app.realtime import doubao_protocol as protocol
from app.realtime.doubao_protocol import (
    AUDIO_PACKET_BYTES,
    FrameProtocolError,
    decode_frame,
    encode_audio,
    encode_event,
)


def test_start_connection_is_exact_byte_vector():
    frame = encode_event(protocol.EVENT_START_CONNECTION, {})
    assert list(frame) == [17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125]


def test_session_event_requires_session_id():
    with pytest.raises(FrameProtocolError):
        encode_event(protocol.EVENT_START_SESSION, {})


def test_session_event_layout_roundtrips():
    frame = encode_event(
        protocol.EVENT_START_SESSION, {"user": {"uid": "测试"}}, session_id="sess-1"
    )
    decoded = decode_frame(frame)
    assert decoded.message_type == protocol.CLIENT_FULL_REQUEST
    assert decoded.event == protocol.EVENT_START_SESSION
    assert decoded.session_id == "sess-1"
    assert decoded.payload == {"user": {"uid": "测试"}}


def test_audio_frame_roundtrips():
    pcm = bytes(range(256)) * 5  # 1280 bytes, one 40ms browser chunk
    assert AUDIO_PACKET_BYTES == 640
    frame = encode_audio("sess-1", pcm)
    decoded = decode_frame(frame)
    assert decoded.message_type == protocol.CLIENT_AUDIO_ONLY_REQUEST
    assert decoded.event == protocol.EVENT_TASK_REQUEST
    assert decoded.payload == pcm


def test_audio_frame_rejects_odd_bytes():
    with pytest.raises(FrameProtocolError):
        encode_audio("sess-1", b"\x01\x02\x03")


def _server_frame(
    event,
    payload_bytes,
    *,
    session_id="sess-1",
    message_type=protocol.SERVER_FULL_RESPONSE,
    serialization=protocol.SERIALIZATION_JSON,
    compression=protocol.COMPRESSION_NONE,
    error_code=None,
):
    flags = protocol.FLAG_WITH_EVENT
    header = bytes(
        (
            0x11,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        )
    )
    parts = [header]
    if message_type == protocol.SERVER_ERROR_RESPONSE:
        parts.append(error_code.to_bytes(4, "big"))
    parts.append(event.to_bytes(4, "big"))
    if event not in protocol._CONNECTION_EVENTS:
        encoded_session = session_id.encode()
        parts.append(len(encoded_session).to_bytes(4, "big"))
        parts.append(encoded_session)
    parts.append(len(payload_bytes).to_bytes(4, "big"))
    parts.append(payload_bytes)
    return b"".join(parts)


def test_decode_gzip_server_frame():
    payload = gzip.compress('{"results":[{"text":"你好","is_interim":false}]}'.encode())
    frame = _server_frame(
        protocol.EVENT_ASR_RESPONSE,
        payload,
        compression=protocol.COMPRESSION_GZIP,
    )
    decoded = decode_frame(frame)
    assert decoded.event == protocol.EVENT_ASR_RESPONSE
    assert decoded.payload == {"results": [{"text": "你好", "is_interim": False}]}


def test_decode_error_frame_carries_error_code():
    frame = _server_frame(
        protocol.EVENT_DIALOG_ERROR,
        b'{"message":"boom"}',
        message_type=protocol.SERVER_ERROR_RESPONSE,
        error_code=5000,
    )
    decoded = decode_frame(frame)
    assert decoded.error_code == 5000
    assert decoded.event == protocol.EVENT_DIALOG_ERROR
    assert decoded.payload == {"message": "boom"}


def test_decode_raw_audio_frame():
    frame = _server_frame(
        protocol.EVENT_TTS_RESPONSE,
        b"\x00\x01\x02\x03",
        serialization=protocol.SERIALIZATION_RAW,
    )
    decoded = decode_frame(frame)
    assert decoded.payload == b"\x00\x01\x02\x03"


def test_truncated_frame_raises():
    frame = _server_frame(protocol.EVENT_CHAT_ENDED, b"{}")
    with pytest.raises(FrameProtocolError):
        decode_frame(frame[:-1])


def test_frame_size_bound_is_enforced():
    frame = _server_frame(protocol.EVENT_CHAT_ENDED, b"{}")
    with pytest.raises(FrameProtocolError):
        decode_frame(frame, max_frame_bytes=len(frame) - 1)


def test_gzip_bomb_is_rejected():
    bomb = gzip.compress(b"\x00" * 10_000)
    frame = _server_frame(
        protocol.EVENT_CHAT_RESPONSE, bomb, compression=protocol.COMPRESSION_GZIP
    )
    with pytest.raises(FrameProtocolError):
        decode_frame(frame, max_decompressed_bytes=1_000)


def test_connection_frame_with_echoed_session_field():
    # Real servers echo the connect id as a session field even on event 50.
    session = b"diag-1"
    payload = b"{}"
    flags = protocol.FLAG_WITH_EVENT
    header = bytes((0x11, (protocol.SERVER_FULL_RESPONSE << 4) | flags, 0x10, 0x00))
    frame = b"".join(
        (
            header,
            protocol.EVENT_CONNECTION_STARTED.to_bytes(4, "big"),
            len(session).to_bytes(4, "big"),
            session,
            len(payload).to_bytes(4, "big"),
            payload,
        )
    )
    decoded = decode_frame(frame)
    assert decoded.event == protocol.EVENT_CONNECTION_STARTED
    assert decoded.session_id == "diag-1"
    assert decoded.payload == {}


def test_connection_frame_without_session_field():
    flags = protocol.FLAG_WITH_EVENT
    header = bytes((0x11, (protocol.SERVER_FULL_RESPONSE << 4) | flags, 0x10, 0x00))
    frame = header + (50).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"{}"
    decoded = decode_frame(frame)
    assert decoded.event == protocol.EVENT_CONNECTION_STARTED
    assert decoded.session_id is None
    assert decoded.payload == {}


def test_payload_size_mismatch_raises():
    frame = bytearray(_server_frame(protocol.EVENT_CHAT_ENDED, b"{}"))
    frame[-4] += 1  # corrupt the declared payload length field
    with pytest.raises(FrameProtocolError):
        decode_frame(bytes(frame))
