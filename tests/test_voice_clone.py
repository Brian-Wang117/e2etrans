"""Voice-clone relay tests: header/body shape, upstream error mapping,
and the FastAPI endpoint wiring (credentials gate + HTTP error mapping)."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import OutboundSettings, RealtimeSettings, Settings
from app.main import create_app
from app.voice_clone import (
    CLONE_TEXT,
    VoiceCloneError,
    VoiceCloneRelay,
)

APP_ID = "1234567890"
ACCESS_KEY = "test-access-key"
SPEAKER = "S_mockslot"


def make_relay(handler) -> VoiceCloneRelay:
    return VoiceCloneRelay(
        app_id=APP_ID,
        access_key=ACCESS_KEY,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def ok_handler(payload=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload or {"BaseResp": {"StatusCode": 0}})

    return handler, calls


async def test_upload_requires_slot_prefix():
    relay = make_relay(lambda request: httpx.Response(200, json={}))
    with pytest.raises(VoiceCloneError, match="S_"):
        await relay.upload(speaker_id="not_a_slot", audio_b64="AAAA")
    with pytest.raises(VoiceCloneError, match="缺少采样音频"):
        await relay.upload(speaker_id=SPEAKER, audio_b64="")


async def test_upload_posts_training_body_and_headers():
    handler, calls = ok_handler()
    relay = make_relay(handler)
    result = await relay.upload(speaker_id=SPEAKER, audio_b64="QUJD")
    assert result["BaseResp"]["StatusCode"] == 0

    request = calls[0]
    assert str(request.url).endswith("/api/v1/mega_tts/audio/upload")
    # Volcengine quirk: "Bearer" and the key are joined by a semicolon.
    assert request.headers["Authorization"] == f"Bearer; {ACCESS_KEY}"
    assert request.headers["Resource-Id"] == "seed-icl-2.0"

    body = json.loads(request.content)
    assert body["appid"] == APP_ID
    assert body["speaker_id"] == SPEAKER
    assert body["model_type"] == 4
    assert body["source"] == 2
    assert body["language"] == 0
    sample = body["audios"][0]
    assert sample["audio_bytes"] == "QUJD"
    assert sample["audio_format"] == "wav"
    # Neutral prose, never business copy, is the training text.
    assert sample["text"] == CLONE_TEXT


async def test_status_uses_status_path_and_same_headers():
    handler, calls = ok_handler({"BaseResp": {"StatusCode": 0}, "status": 2})
    relay = make_relay(handler)
    result = await relay.status(speaker_id=SPEAKER)
    assert result["status"] == 2
    assert str(calls[0].url).endswith("/api/v1/mega_tts/status")
    assert calls[0].headers["Resource-Id"] == "seed-icl-2.0"


async def test_wer_error_maps_to_reread_guidance():
    def handler(request):
        return httpx.Response(
            200,
            json={"BaseResp": {"StatusCode": 1109, "StatusMessage": "WERError"}},
        )

    relay = make_relay(handler)
    with pytest.raises(VoiceCloneError) as excinfo:
        await relay.upload(speaker_id=SPEAKER, audio_b64="QUJD")
    assert "对照文本重录" in str(excinfo.value)
    assert excinfo.value.code == 1109


async def test_license_message_flags_foreign_slot():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "BaseResp": {
                    "StatusCode": 9999,
                    "StatusMessage": "license not authorized for speaker",
                }
            },
        )

    relay = make_relay(handler)
    with pytest.raises(VoiceCloneError) as excinfo:
        await relay.status(speaker_id=SPEAKER)
    assert "不属于当前 AppID" in str(excinfo.value)


async def test_unknown_error_falls_back_to_upstream_message():
    def handler(request):
        return httpx.Response(
            200,
            json={"BaseResp": {"StatusCode": 42, "StatusMessage": "boom"}},
        )

    relay = make_relay(handler)
    with pytest.raises(VoiceCloneError, match="boom"):
        await relay.status(speaker_id=SPEAKER)


def test_relay_requires_credentials():
    with pytest.raises(VoiceCloneError, match="DOUBAO_APP_ID"):
        VoiceCloneRelay(app_id="", access_key="")


# -- endpoint wiring --------------------------------------------------------------


def make_app(tmp_path: Path, *, with_credentials: bool) -> TestClient:
    kwargs = {"provider": "doubao"}
    if with_credentials:
        kwargs["app_id"] = APP_ID
        kwargs["access_key"] = ACCESS_KEY
    realtime = RealtimeSettings(**kwargs)
    settings = Settings(
        database_path=tmp_path / "sessions.db",
        realtime=realtime,
        outbound=OutboundSettings(enabled=False),
    )
    return TestClient(create_app(settings))


def test_meta_reports_disabled_without_credentials(tmp_path):
    with make_app(tmp_path, with_credentials=False) as client:
        response = client.get("/api/voice-clone/meta")
        assert response.status_code == 200
        payload = response.json()
        assert payload["enabled"] is False
        assert payload["clone_text"] == CLONE_TEXT
        assert payload["sample_seconds"] > 0


def test_endpoints_require_credentials(tmp_path):
    with make_app(tmp_path, with_credentials=False) as client:
        assert client.post(
            "/api/voice-clone/upload", json={"speaker_id": SPEAKER}
        ).status_code == 503
        assert client.post(
            "/api/voice-clone/status", json={"speaker_id": SPEAKER}
        ).status_code == 503


def test_upload_maps_validation_and_upstream_errors(tmp_path, monkeypatch):
    import app.main as main_module

    original_relay = main_module.VoiceCloneRelay

    def relay_factory(**kwargs):
        def handler(request):
            return httpx.Response(
                200,
                json={"BaseResp": {"StatusCode": 1109, "StatusMessage": "WERError"}},
            )

        kwargs["client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return original_relay(**kwargs)

    monkeypatch.setattr(main_module, "VoiceCloneRelay", relay_factory)
    with make_app(tmp_path, with_credentials=True) as client:
        # S_ validation fails before any network call -> 400 mapped message.
        response = client.post(
            "/api/voice-clone/upload",
            json={"speaker_id": "bad", "audio_wav_b64": "QUJD"},
        )
        assert response.status_code == 400
        assert "S_" in response.json()["detail"]

        # Upstream WER error -> 400 with human-readable guidance.
        response = client.post(
            "/api/voice-clone/upload",
            json={"speaker_id": SPEAKER, "audio_wav_b64": "QUJD"},
        )
        assert response.status_code == 400
        assert "对照文本重录" in response.json()["detail"]


def test_status_endpoint_reports_ready(tmp_path, monkeypatch):
    import app.main as main_module

    original_relay = main_module.VoiceCloneRelay

    def relay_factory(**kwargs):
        def handler(request):
            return httpx.Response(
                200,
                json={"BaseResp": {"StatusCode": 0}, "speaker_id": SPEAKER, "status": 2},
            )

        kwargs["client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return original_relay(**kwargs)

    monkeypatch.setattr(main_module, "VoiceCloneRelay", relay_factory)
    with make_app(tmp_path, with_credentials=True) as client:
        response = client.post(
            "/api/voice-clone/status", json={"speaker_id": SPEAKER}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is True
        assert payload["status"] == 2


def test_upstream_network_error_maps_to_502(tmp_path, monkeypatch):
    import app.main as main_module

    original_relay = main_module.VoiceCloneRelay

    def relay_factory(**kwargs):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        kwargs["client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return original_relay(**kwargs)

    monkeypatch.setattr(main_module, "VoiceCloneRelay", relay_factory)
    with make_app(tmp_path, with_credentials=True) as client:
        response = client.post(
            "/api/voice-clone/upload",
            json={"speaker_id": SPEAKER, "audio_wav_b64": "QUJD"},
        )
        assert response.status_code == 502
        assert response.json()["detail"].startswith("上游请求失败")
