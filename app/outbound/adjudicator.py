"""End-of-call interest adjudication via the DashScope text model.

Runs after the call ends, off the audio path: it classifies the whole
transcript into 感兴趣 / 不感兴趣 / 中立 with a short reason. Failures raise
:class:`AdjudicationError`; the caller is responsible for the safe fallback
(中立) so an upstream outage can never lose the call record.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

import httpx

from app.outbound.script_library import (
    VERDICT_INTERESTED,
    VERDICT_NEUTRAL,
    VERDICT_NOT_INTERESTED,
    VERDICTS,
)

MAX_DIALOGUE_CHARS = 8000
MAX_REASON_CHARS = 200

_SYSTEM_PROMPT = (
    "你是电话营销质检员。根据完整通话记录，判断客户对本次营销活动的态度，"
    "只能输出 JSON：{\"result\": \"感兴趣|不感兴趣|中立\", \"reason\": \"一句话理由\"}。"
    "客户主动询问活动细节、表示想了解或愿意尝试记为感兴趣；"
    "明确拒绝、要求不要再打记为不感兴趣；其余情况记为中立。不要输出其他内容。"
)

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


class AdjudicationError(RuntimeError):
    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class AdjudicationResult:
    verdict: str
    reason: str
    model: str


class OutboundAdjudicator:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not model or not base_url:
            raise ValueError("api_key, model and base_url are required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def adjudicate(self, dialogue: list[tuple[str, str]]) -> AdjudicationResult:
        """Classify one transcript. ``dialogue`` is (speaker, text) pairs."""
        if not dialogue:
            raise AdjudicationError("dialogue is empty", category="malformed")
        lines = [f"{speaker}: {text}" for speaker, text in dialogue if text.strip()]
        transcript = "\n".join(lines)
        if len(transcript) > MAX_DIALOGUE_CHARS:
            transcript = transcript[:MAX_DIALOGUE_CHARS]
        body = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise AdjudicationError("adjudication timed out", category="timeout") from error
        except httpx.TransportError as error:
            raise AdjudicationError(
                "adjudication transport failed", category="transport"
            ) from error
        if response.status_code in {401, 403}:
            raise AdjudicationError("adjudication is not authorized", category="auth")
        if response.status_code == 429:
            raise AdjudicationError("adjudication hit a rate limit", category="rate_limit")
        if response.status_code >= 400:
            raise AdjudicationError("adjudication request failed", category="transport")
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> AdjudicationResult:
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AdjudicationError(
                "adjudication response is malformed", category="malformed"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise AdjudicationError(
                "adjudication response is malformed", category="malformed"
            )
        document = self._extract_json(content)
        raw_verdict = document.get("result", document.get("verdict", ""))
        verdict = self._normalize_verdict(str(raw_verdict))
        reason = str(document.get("reason", "")).strip()[:MAX_REASON_CHARS]
        return AdjudicationResult(verdict=verdict, reason=reason, model=self._model)

    @staticmethod
    def _extract_json(content: str) -> dict[str, object]:
        candidate = content.strip()
        fence = _JSON_FENCE_PATTERN.search(candidate)
        if fence:
            candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            match = _JSON_OBJECT_PATTERN.search(candidate)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        raise AdjudicationError(
            "adjudication response is malformed", category="malformed"
        )

    @staticmethod
    def _normalize_verdict(raw: str) -> str:
        raw = raw.strip()
        if raw in VERDICTS:
            return raw
        # Order matters: "不感兴趣" contains "感兴趣" as a substring.
        if VERDICT_NOT_INTERESTED in raw:
            return VERDICT_NOT_INTERESTED
        if VERDICT_INTERESTED in raw:
            return VERDICT_INTERESTED
        return VERDICT_NEUTRAL

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
