"""Per-customer personalization before each outbound call (requirement 2.3).

Two artifacts, generated just-in-time:

1. Opening greeting — pure rules, never an LLM call, so the first sentence
   plays as fast as possible: 您好，请问是{姓名}{女士/先生}吗？
2. Business background — one lightweight LLM call that folds the customer's
   raw row into the global template. Any failure falls back to the plain
   template; personalization is an enhancement, never a blocker.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import httpx

NAME_COLUMN_HINTS = ("姓名", "名字", "客户姓名", "称呼", "name", "customer_name")
GENDER_COLUMN_HINTS = ("性别", "gender", "sex")
BACKGROUND_COLUMN_HINTS = ("客户背景", "背景", "background")
TASK_COLUMN_HINTS = ("外呼任务", "任务", "task")
FALLBACK_OPENING = "您好，请问是本机主人吗？"
MAX_BACKGROUND_CHARS = 300
CACHE_KEY = "_personalized_background"

_SYSTEM_PROMPT = (
    "你是外呼营销坐席的后台助手。根据商家背景模板和一位客户的资料，"
    "写一段给坐席用的业务背景说明，把该客户相关的信息（购买过的产品、"
    "时间、偏好等）自然地融进去。要求：不超过300字；只使用给定资料，"
    "不得编造价格、优惠或资料里没有的事实；直接输出正文，不要标题和解释。"
)


class PersonalizationError(RuntimeError):
    """LLM background generation failed; callers must fall back."""


@dataclass(frozen=True)
class Personalization:
    opening_text: str
    business_background: str
    generated: bool  # True when the background came from the LLM


def _find_value(raw_data: dict[str, object], hints: tuple[str, ...]) -> str:
    """Hints are tried in priority order, then columns are scanned."""
    for hint in hints:
        for column, value in raw_data.items():
            if column.startswith("_"):
                continue
            if hint in str(column).lower():
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def extract_name(raw_data: dict[str, object]) -> str:
    return _find_value(raw_data, NAME_COLUMN_HINTS)


def extract_title(raw_data: dict[str, object]) -> str:
    """Return 女士 / 先生 / '' from the gender column, falling back to
    honorifics embedded in any field (e.g. 王女士)."""
    gender = _find_value(raw_data, GENDER_COLUMN_HINTS)
    if "女" in gender:
        return "女士"
    if "男" in gender:
        return "先生"
    for column, value in raw_data.items():
        if column.startswith("_"):
            continue
        text = str(value or "")
        if "女士" in text:
            return "女士"
        if "先生" in text:
            return "先生"
    return ""


def build_opening_text(raw_data: dict[str, object]) -> str:
    name = extract_name(raw_data)
    if not name:
        return FALLBACK_OPENING
    if name.endswith("女士") or name.endswith("先生"):
        # The name cell already carries the honorific (e.g. 王女士).
        return f"您好，请问是{name}吗？"
    title = extract_title(raw_data)
    return f"您好，请问是{name}{title}吗？"


def extract_background(raw_data: dict[str, object]) -> str:
    """Background supplied by the imported list itself (e.g. 客户背景)."""
    return _find_value(raw_data, BACKGROUND_COLUMN_HINTS)


def extract_task(raw_data: dict[str, object]) -> str:
    """Call objective supplied by the imported list itself (e.g. 外呼任务)."""
    return _find_value(raw_data, TASK_COLUMN_HINTS)


def build_provided_background(raw_data: dict[str, object]) -> str:
    """Fold the list-provided background (and task, when present) into the
    agent-facing text. Returns '' when the list carries no background."""
    background = extract_background(raw_data)
    if not background:
        return ""
    task = extract_task(raw_data)
    if task:
        return f"外呼任务：{task}\n客户背景：{background}"[:MAX_BACKGROUND_CHARS]
    return background[:MAX_BACKGROUND_CHARS]


class Personalizer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        fallback_background: str,
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
        self._fallback_background = fallback_background
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def personalize(self, raw_data: dict[str, object]) -> Personalization:
        opening_text = build_opening_text(raw_data)
        cached = str(raw_data.get(CACHE_KEY) or "").strip()
        if cached:
            return Personalization(
                opening_text=opening_text,
                business_background=cached,
                generated=False,
            )
        provided = build_provided_background(raw_data)
        if provided:
            # The imported list already carries a curated background; use it
            # directly instead of spending an LLM call per customer.
            return Personalization(
                opening_text=opening_text,
                business_background=provided,
                generated=False,
            )
        try:
            background = await self._generate_background(raw_data)
        except PersonalizationError:
            return Personalization(
                opening_text=opening_text,
                business_background=self._fallback_background,
                generated=False,
            )
        return Personalization(
            opening_text=opening_text,
            business_background=background,
            generated=True,
        )

    async def _generate_background(self, raw_data: dict[str, object]) -> str:
        visible = {
            column: value
            for column, value in raw_data.items()
            if not column.startswith("_")
        }
        user_content = (
            "商家背景模板：\n"
            f"{self._fallback_background}\n\n"
            "客户资料（JSON）：\n"
            f"{json.dumps(visible, ensure_ascii=False)}"
        )
        body = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
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
            raise PersonalizationError("personalization timed out") from error
        except httpx.TransportError as error:
            raise PersonalizationError("personalization transport failed") from error
        if response.status_code >= 400:
            raise PersonalizationError(
                f"personalization request failed: {response.status_code}"
            )
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise PersonalizationError("personalization response malformed") from error
        background = str(content).strip()
        if not background:
            raise PersonalizationError("personalization response empty")
        return background[:MAX_BACKGROUND_CHARS]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
