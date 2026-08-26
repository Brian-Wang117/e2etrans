"""Voice-clone relay: browser never carries credentials, the server does.

Proxies the workbench voice-sampling flow to the Volcengine mega_tts
training API (Seed-ICL 2.0, ``model_type=4``). Same "secrets stay
server-side" pattern as the Doubao realtime WS passthrough.

Two deliberate content decisions inherited from the reference design:
- ``CLONE_TEXT`` is neutral prose, never business scripts: the training
  text gets injected into the model, so business wording would leak into
  real conversations.
- Upstream numeric errors are mapped to human-readable guidance (e.g.
  1109 WER -> "录音与朗读文本差异过大，请对照文本重录").
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

OPENSPEECH_BASE = "https://openspeech.bytedance.com"
UPLOAD_PATH = "/api/v1/mega_tts/audio/upload"
STATUS_PATH = "/api/v1/mega_tts/status"

MODEL_TYPE_ICL_V2 = 4  # 声音复刻 SC2.0
RESOURCE_IDS = {MODEL_TYPE_ICL_V2: "seed-icl-2.0", 1: "seed-icl-1.0"}

CLONE_SAMPLE_SECONDS = 45

# Neutral reading material (~230 chars, ~45s at a calm pace). Not business
# copy on purpose — Volcengine injects the training text into the model.
CLONE_TEXT = (
    "清晨的阳光透过窗帘的缝隙洒进房间，木桌上的茶杯还冒着淡淡的热气。"
    "窗外的梧桐树在微风里轻轻摇晃，偶尔有鸟雀落在枝头，叫上几声又飞走了。"
    "远处的街道上传来自行车铃铛的声音，缓慢而清晰。"
    "我翻开一本书，慢慢读了起来，时间好像也跟着慢了下来。"
    "午后下了一场小雨，雨水顺着屋檐滴落，空气里有泥土和青草的味道。"
    "傍晚时分，天边出现了淡淡的晚霞，颜色从橙红慢慢变成灰蓝。"
    "这样的日子平静而踏实，让人觉得安心。"
)

# StatusCode -> human-readable guidance. Unlisted codes fall back to the
# upstream StatusMessage.
_ERROR_MESSAGES: dict[int, str] = {
    1001: "请求参数有误，请检查槽位与音频格式",
    1101: "音频上传失败，请重试",
    1102: "音频转写失败，请清晰朗读后重试",
    1103: "声纹检测失败，请确保只有一个人说话",
    1104: "声纹与名人相似度过高，无法复刻",
    1105: "获取音频数据失败，请重试",
    1108: "音频转码失败，请重新录音",
    1109: "录音与朗读文本差异过大，请对照文本重录",
    1111: "未检测到有效说话声，请靠近麦克风朗读",
    1112: "音频信噪比异常，请在安静环境重录",
    1113: "降噪处理失败，请重录",
    1114: "音频质量过低，请在安静环境重录",
    1122: "未检测到人声，请确认麦克风正常工作",
    1123: "该槽位已达上传次数限制（10 次），请更换槽位",
}

# Markers in StatusMessage pointing at slot/license ownership problems.
_LICENSE_MARKERS = ("license", "appid", "unauthorized", "permission")


class VoiceCloneError(RuntimeError):
    """Mapped, user-facing clone failure (message safe to show in the UI)."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def resource_id_for_model(model_type: int) -> str:
    try:
        return RESOURCE_IDS[model_type]
    except KeyError:
        raise VoiceCloneError(f"不支持的 model_type: {model_type}") from None


class VoiceCloneRelay:
    def __init__(
        self,
        *,
        app_id: str,
        access_key: str,
        timeout_seconds: float = 60.0,
        base_url: str = OPENSPEECH_BASE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not app_id or not access_key:
            raise VoiceCloneError("音色复刻需要 DOUBAO_APP_ID 与 DOUBAO_ACCESS_KEY")
        self._app_id = app_id
        self._access_key = access_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, model_type: int) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            # Volcengine quirk: "Bearer" and the token are joined by a
            # semicolon, not a space.
            "Authorization": f"Bearer; {self._access_key}",
            "Resource-Id": resource_id_for_model(model_type),
        }

    async def upload(
        self,
        *,
        speaker_id: str,
        audio_b64: str,
        text: str = CLONE_TEXT,
        model_type: int = MODEL_TYPE_ICL_V2,
    ) -> dict[str, object]:
        if not str(speaker_id).startswith("S_"):
            raise VoiceCloneError("音色槽位必须以 S_ 开头（从控制台音色库获取）")
        if not audio_b64:
            raise VoiceCloneError("缺少采样音频")
        body = {
            "appid": self._app_id,
            "speaker_id": speaker_id,
            "audios": [
                {"audio_bytes": audio_b64, "audio_format": "wav", "text": text}
            ],
            "source": 2,
            "language": 0,
            "model_type": model_type,
        }
        response = await self._client.post(
            f"{self._base_url}{UPLOAD_PATH}",
            headers=self._headers(model_type),
            json=body,
        )
        return self._parse(response, speaker_id=speaker_id)

    async def status(
        self,
        *,
        speaker_id: str,
        model_type: int = MODEL_TYPE_ICL_V2,
    ) -> dict[str, object]:
        if not str(speaker_id).startswith("S_"):
            raise VoiceCloneError("音色槽位必须以 S_ 开头（从控制台音色库获取）")
        response = await self._client.post(
            f"{self._base_url}{STATUS_PATH}",
            headers=self._headers(model_type),
            json={"appid": self._app_id, "speaker_id": speaker_id},
        )
        return self._parse(response, speaker_id=speaker_id)

    def _parse(self, response: httpx.Response, *, speaker_id: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        base = payload.get("BaseResp") or {}
        code = base.get("StatusCode")
        if response.status_code != 200 or code not in (0, None):
            message = str(base.get("StatusMessage") or "").strip()
            mapped = _ERROR_MESSAGES.get(int(code) if code is not None else -1)
            human = mapped or (message or f"音色复刻失败（HTTP {response.status_code}）")
            if mapped is None and any(
                marker in message.lower() for marker in _LICENSE_MARKERS
            ):
                human = f"{human}（槽位 {speaker_id} 可能不属于当前 AppID）"
            logger.warning(
                "voice clone relay rejected: code=%s message=%s", code, message
            )
            raise VoiceCloneError(human, code=code)
        return payload
