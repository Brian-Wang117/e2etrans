"""Temporary diagnostic: full Doubao handshake probe (not part of the app)."""

import asyncio
import uuid

from app.config import load_env_file, settings_from_env
from app.realtime import doubao_protocol as protocol

load_env_file()
settings = settings_from_env().realtime


def start_session_payload() -> dict:
    extra = {
        "strict_audit": False,
        "audit_response": "",
        "recv_timeout": 10,
        "input_mod": settings.input_mode,
    }
    if settings.model:
        extra["model"] = settings.model
    return {
        "asr": {"extra": {"end_smooth_window_ms": 1500}},
        "tts": {
            "speaker": settings.speaker,
            "extra": {},
            "audio_config": {
                "channel": 1,
                "format": "pcm_s16le",
                "sample_rate": settings.output_sample_rate,
            },
        },
        "dialog": {
            "bot_name": "EN Customer Care",
            "system_role": "You are a professional outbound customer-service agent.",
            "speaking_style": "Natural, calm, warm, and professional.",
            "location": {"city": "北京"},
            "extra": extra,
        },
    }


async def main() -> None:
    from websockets.asyncio.client import connect

    session_id = uuid.uuid4().hex
    headers = {
        "X-Api-App-ID": settings.app_id or "",
        "X-Api-Access-Key": settings.access_key or "",
        "X-Api-Resource-Id": settings.resource_id,
        "X-Api-App-Key": settings.app_key or "",
        "X-Api-Connect-Id": session_id,
    }
    async with asyncio.timeout(30):
        async with connect(
            settings.ws_url, additional_headers=headers, ping_interval=None
        ) as socket:
            await socket.send(protocol.encode_event(protocol.EVENT_START_CONNECTION, {}))
            for _ in range(4):
                raw = await asyncio.wait_for(socket.recv(), timeout=10)
                frame = protocol.decode_frame(raw)
                print("frame:", frame.message_type, frame.event, frame.error_code, frame.payload)
                if frame.event == protocol.EVENT_CONNECTION_STARTED:
                    break
            await socket.send(
                protocol.encode_event(
                    protocol.EVENT_START_SESSION,
                    start_session_payload(),
                    session_id=session_id,
                )
            )
            for _ in range(4):
                raw = await asyncio.wait_for(socket.recv(), timeout=10)
                frame = protocol.decode_frame(raw)
                print("frame:", frame.message_type, frame.event, frame.error_code, frame.payload)
                if frame.event in (protocol.EVENT_SESSION_STARTED, protocol.EVENT_SESSION_FAILED):
                    break


asyncio.run(main())
