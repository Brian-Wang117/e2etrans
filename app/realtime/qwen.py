"""Qwen final-text subtitle translation adapter (DashScope compatible mode).

Qwen is a side channel: it never participates in the Doubao dialogue control
loop and never blocks the first English audio packet. Failures are reported
with fixed safe categories and never echo upstream bodies, URLs, or keys.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import httpx

MAX_SOURCE_CHARS = 4000
MAX_RESULT_CHARS = 4000

_SYSTEM_PROMPT = (
    "You are a strict professional translator. Translate the user message "
    "between Chinese and English. Preserve numbers, amounts, dates, proper "
    "nouns and negation exactly. Do not explain, answer, expand, or add any "
    "content. Return only the translation text itself."
)


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


class QwenSubtitleTranslator:
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

    async def translate(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
    ) -> SubtitleResult:
        if not isinstance(text, str) or not text.strip():
            raise SubtitleTranslationError(
                "subtitle source text is empty", category="malformed"
            )
        if source_language not in {"zh", "en"} or target_language not in {"zh", "en"}:
            raise SubtitleTranslationError(
                "subtitle language pair is unsupported", category="malformed"
            )
        if len(text) > MAX_SOURCE_CHARS:
            raise SubtitleTranslationError(
                "subtitle source text is too long", category="malformed"
            )
        direction = (
            "Chinese to English"
            if source_language == "zh"
            else "English to Chinese"
        )
        body = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": f"{_SYSTEM_PROMPT} Direction: {direction}."},
                {"role": "user", "content": text},
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
            raise SubtitleTranslationError(
                "subtitle translation timed out", category="timeout"
            ) from error
        except httpx.TransportError as error:
            raise SubtitleTranslationError(
                "subtitle translation transport failed", category="transport"
            ) from error
        if response.status_code in {401, 403}:
            raise SubtitleTranslationError(
                "subtitle translation is not authorized", category="auth"
            )
        if response.status_code == 429:
            raise SubtitleTranslationError(
                "subtitle translation hit a rate limit", category="rate_limit"
            )
        if response.status_code >= 400:
            raise SubtitleTranslationError(
                "subtitle translation request failed", category="transport"
            )
        content = self._completion_content(response)
        return SubtitleResult(text=content, model=self._model)

    def _completion_content(self, response: httpx.Response) -> str:
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise SubtitleTranslationError(
                "subtitle translation response is malformed", category="malformed"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise SubtitleTranslationError(
                "subtitle translation response is malformed", category="malformed"
            )
        content = content.strip()
        if len(content) > MAX_RESULT_CHARS:
            raise SubtitleTranslationError(
                "subtitle translation response is malformed", category="malformed"
            )
        return content

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
