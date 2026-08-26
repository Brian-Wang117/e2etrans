"""Application settings with secret-safe realtime configuration.

Environment values are loaded from an optional ``.env`` / ``.env.test`` file
and the process environment. Secret fields are excluded from ``repr`` so they
never leak into logs or error output.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(root: Path = PROJECT_ROOT) -> None:
    """Populate ``os.environ`` from ``.env.test`` (base layer) and ``.env``
    (override layer) without overriding values already present in the real
    process environment."""
    for candidate in (root / ".env.test", root / ".env"):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive finite number") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


@dataclass(frozen=True)
class RealtimeSettings:
    provider: str = "disabled"
    ws_url: str = "wss://openspeech.bytedance.com/api/v3/realtime_dialogue"
    app_id: str | None = field(default=None, repr=False)
    access_key: str | None = field(default=None, repr=False)
    app_key: str | None = field(default=None, repr=False)
    resource_id: str = "volc.speech.dialog"
    model: str = ""
    speaker: str | None = None
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    input_mode: str = "push_to_talk"
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
    allowed_origins: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.provider == "doubao"


@dataclass(frozen=True)
class OutboundSettings:
    """Outbound-call engine thresholds (requirement doc section 六)."""

    enabled: bool = False
    silence_seconds: float = 60.0
    max_turns: int = 50
    opening_text: str = ""
    business_background: str = ""
    bot_name: str = ""
    speaking_style: str = ""
    # Gender-matched TTS voices: male customers hear the female voice and
    # vice versa. Empty means "fall back to DOUBAO_TTS_SPEAKER".
    tts_speaker_male: str = ""
    tts_speaker_female: str = ""


@dataclass(frozen=True)
class AuthSettings:
    """Azure AD unified web login via the tp-azure gateway.

    The Azure OAuth flow is handled by ``cluster.tpcnailab.com``; this app
    only verifies the JWT it signs and keeps a session cookie.
    """

    enabled: bool = False
    auth_server: str = "https://cluster.tpcnailab.com"
    app_name: str = "e2etrans_voice_demo"
    jwt_secret: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    session_cookie: str = "e2etrans_session"
    cookie_secure: bool = False
    frontend_url: str = ""


@dataclass(frozen=True)
class Settings:
    provider_mode: str = "mock"
    host: str = "127.0.0.1"
    port: int = 8765
    database_path: Path = PROJECT_ROOT / "data" / "sessions.db"
    # External URL prefix this app is served under (e.g. "/v2" when nginx
    # mounts it at https://obbot.tpcnailab.com/v2). Empty = served at root.
    base_path: str = ""
    realtime: RealtimeSettings = field(default_factory=RealtimeSettings)
    outbound: OutboundSettings = field(default_factory=OutboundSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)


def _realtime_from_env(default_origins: tuple[str, ...]) -> RealtimeSettings:
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
        if not str(values["DOUBAO_TTS_SPEAKER"]).startswith("en_"):
            logger.warning(
                "DOUBAO_TTS_SPEAKER is not an 'en_' voice; English customer-care "
                "playback requires an enabled English voice on the account"
            )
    subtitle_enabled = _env_bool("QWEN_SUBTITLE_ENABLED", False)
    qwen_key = os.getenv("DASHSCOPE_API_KEY") or None
    if subtitle_enabled and not qwen_key:
        raise ValueError("DASHSCOPE_API_KEY is required when QWEN_SUBTITLE_ENABLED is true")
    origins_env = os.getenv("WS_ALLOWED_ORIGINS")
    if origins_env and origins_env.strip():
        origins = tuple(item.strip() for item in origins_env.split(",") if item.strip())
    else:
        origins = default_origins
    ws_url = os.getenv(
        "DOUBAO_REALTIME_WS_URL",
        os.getenv("DOUBAO_WS_URL", "wss://openspeech.bytedance.com/api/v3/realtime_dialogue"),
    )
    input_mode = os.getenv("DOUBAO_INPUT_MODE", "push_to_talk").strip().lower()
    if input_mode not in {"push_to_talk", "audio"}:
        raise ValueError("DOUBAO_INPUT_MODE must be 'push_to_talk' or 'audio'")
    if provider == "doubao" and not ws_url.startswith("wss://"):
        raise ValueError("DOUBAO_REALTIME_WS_URL must use wss")
    input_rate = _env_int("DOUBAO_INPUT_SAMPLE_RATE", 16000)
    output_rate = _env_int("DOUBAO_OUTPUT_SAMPLE_RATE", 24000)
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
        model=os.getenv("DOUBAO_MODEL", "").strip(),
        speaker=values["DOUBAO_TTS_SPEAKER"],
        input_sample_rate=input_rate,
        output_sample_rate=output_rate,
        input_mode=input_mode,
        connect_timeout_seconds=_env_float("DOUBAO_CONNECT_TIMEOUT_SECONDS", 15.0),
        receive_timeout_seconds=_env_float("DOUBAO_RECV_TIMEOUT_SECONDS", 10.0),
        max_message_bytes=_env_int("WS_MAX_MESSAGE_BYTES", 131_072),
        max_buffer_bytes=_env_int("WS_MAX_BUFFER_BYTES", 1_048_576),
        max_upstream_frame_bytes=_env_int("DOUBAO_MAX_UPSTREAM_FRAME_BYTES", 262_144),
        max_decompressed_bytes=_env_int("DOUBAO_MAX_DECOMPRESSED_BYTES", 262_144),
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


def _outbound_from_env() -> OutboundSettings:
    return OutboundSettings(
        enabled=_env_bool("OUTBOUND_ENABLED", False),
        silence_seconds=_env_float("OUTBOUND_SILENCE_SECONDS", 60.0),
        max_turns=_env_int("OUTBOUND_MAX_TURNS", 50),
        opening_text=os.getenv("OUTBOUND_OPENING_TEXT", "").strip(),
        business_background=os.getenv("OUTBOUND_BUSINESS_BACKGROUND", "").strip(),
        bot_name=os.getenv("OUTBOUND_BOT_NAME", "").strip(),
        speaking_style=os.getenv("OUTBOUND_SPEAKING_STYLE", "").strip(),
        tts_speaker_male=os.getenv("OUTBOUND_TTS_SPEAKER_MALE", "").strip(),
        tts_speaker_female=os.getenv("OUTBOUND_TTS_SPEAKER_FEMALE", "").strip(),
    )


def _auth_from_env() -> AuthSettings:
    enabled = _env_bool("ENABLE_UNIFIED_AUTH", False)
    auth_server = (
        os.getenv("AUTH_SERVER", "https://cluster.tpcnailab.com").strip().rstrip("/")
    )
    app_name = os.getenv("APP_NAME", "e2etrans_voice_demo").strip()
    jwt_secret = os.getenv("AZURE_JWT_SECRET", "")
    session_secret = os.getenv("SESSION_SECRET_KEY", "")
    if enabled:
        if not jwt_secret:
            raise ValueError("AZURE_JWT_SECRET is required when ENABLE_UNIFIED_AUTH is true")
        if not session_secret:
            raise ValueError("SESSION_SECRET_KEY is required when ENABLE_UNIFIED_AUTH is true")
        if not app_name:
            raise ValueError("APP_NAME must not be empty when ENABLE_UNIFIED_AUTH is true")
    return AuthSettings(
        enabled=enabled,
        auth_server=auth_server,
        app_name=app_name,
        jwt_secret=jwt_secret,
        session_secret=session_secret,
        session_cookie=os.getenv("AUTH_SESSION_COOKIE", "e2etrans_session").strip()
        or "e2etrans_session",
        cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
        frontend_url=os.getenv("FRONTEND_URL", "").strip().rstrip("/"),
    )


def settings_from_env() -> Settings:
    load_env_file()
    provider_mode = os.getenv("PROVIDER_MODE", "mock").strip().lower()
    if provider_mode not in {"mock", "ark"}:
        raise ValueError("PROVIDER_MODE must be 'mock' or 'ark'")
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _env_int("PORT", 8765)
    base_path = os.getenv("BASE_PATH", "").strip()
    if base_path:
        if not base_path.startswith("/"):
            raise ValueError("BASE_PATH must start with '/'")
        base_path = base_path.rstrip("/")
    default_origins = (
        f"http://{host}:{port}",
        f"http://localhost:{port}",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )
    data_root = PROJECT_ROOT / "data"
    return Settings(
        provider_mode=provider_mode,
        host=host,
        port=port,
        database_path=Path(os.getenv("DATABASE_PATH", str(data_root / "sessions.db"))),
        base_path=base_path,
        realtime=_realtime_from_env(default_origins),
        outbound=_outbound_from_env(),
        auth=_auth_from_env(),
    )
