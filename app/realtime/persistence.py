"""Sequential final-turn persistence with non-blocking Qwen subtitles.

One async worker tail preserves conversational order: translation requests run
without blocking the upstream receive loop, and turns are inserted strictly in
the order they were finalized. Failures keep the original text with a safe
``subtitle_error`` marker instead of fabricating translations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from app.realtime.qwen import SubtitleTranslationError, SubtitleTranslator
from app.storage import Repository

logger = logging.getLogger(__name__)

Notify = Callable[[str, dict[str, object], str | None], Awaitable[None]]


class PersistenceError(RuntimeError):
    pass


def _safe_turn(
    *,
    turn_id: int,
    seq: int,
    speaker: str,
    source_language: str,
    target_language: str,
    source_text: str,
    translated_text: str,
    model: str,
    latency_ms: int | None,
    interrupted: bool,
    error_code: str | None,
) -> dict[str, object]:
    return {
        "id": turn_id,
        "seq": seq,
        "speaker": speaker,
        "source_language": source_language,
        "target_language": target_language,
        "source_text": source_text,
        "translated_text": translated_text,
        "model": model,
        "latency_ms": latency_ms,
        "interrupted": interrupted,
        "error_code": error_code,
    }


class RealtimePersistence:
    def __init__(
        self,
        *,
        session_id: str,
        repository: Repository,
        translator: SubtitleTranslator | None,
        translation_model: str,
        notify: Notify,
    ) -> None:
        self.session_id = session_id
        self._repository = repository
        self._translator = translator
        self._translation_model = translation_model
        self._notify = notify
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._seq = 0
        self._worker = asyncio.create_task(self._work())

    @property
    def subtitles_enabled(self) -> bool:
        return self._translator is not None

    # -- public API -------------------------------------------------------------

    async def finalize_user(
        self,
        *,
        text: str,
        input_kind: str,
        source: str = "",
    ) -> None:
        if input_kind not in {"audio", "text"}:
            raise PersistenceError("input_kind must be 'audio' or 'text'")
        await self._queue.put(
            ("user", text, input_kind, False, source)
        )

    async def complete_assistant(
        self,
        *,
        text: str,
        interrupted: bool,
        source: str = "",
    ) -> None:
        await self._queue.put(("agent", text, "audio", interrupted, source))

    async def drain(self, *, timeout_seconds: float) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._queue.join()
        except asyncio.TimeoutError as error:
            raise PersistenceError("persistence drain timed out") from error

    async def abort(self) -> None:
        self._worker.cancel()
        try:
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass

    # -- worker tail --------------------------------------------------------------

    async def _work(self) -> None:
        try:
            while True:
                job = await self._queue.get()
                try:
                    await self._process(job)
                except Exception:
                    logger.exception("realtime persistence job failed")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _process(self, job: tuple) -> None:
        kind, text, input_kind, interrupted, source = job
        self._seq += 1
        seq = self._seq
        translated = ""
        latency_ms: int | None = None
        error_code: str | None = None
        model = "doubao_asr" if kind == "user" else "doubao_e2e"
        if kind == "user":
            source_language, target_language = "zh", "en"
            speaker = "tester"
        else:
            source_language, target_language = "en", "zh"
            speaker = "agent"
        if self._translator is not None and text.strip():
            started = time.perf_counter()
            try:
                result = await self._translator.translate(
                    text,
                    source_language=source_language,
                    target_language=target_language,
                )
                translated = result.text
                model = f"{model};translation_model={result.model}"
            except SubtitleTranslationError as error:
                error_code = "subtitle_error"
                logger.warning("subtitle translation failed: category=%s", error.category)
            latency_ms = int((time.perf_counter() - started) * 1000)
        turn_id = await asyncio.to_thread(
            self._repository.add_turn,
            self.session_id,
            speaker=speaker,
            source_language=source_language,
            target_language=target_language,
            source_text=text,
            translated_text=translated,
            model=model,
            latency_ms=latency_ms,
            interrupted=interrupted,
            source=source,
        )
        if error_code:
            await asyncio.to_thread(self._repository.mark_turn_error, turn_id, error_code)
        turn = _safe_turn(
            turn_id=turn_id,
            seq=seq,
            speaker=speaker,
            source_language=source_language,
            target_language=target_language,
            source_text=text,
            translated_text=translated,
            model=model,
            latency_ms=latency_ms,
            interrupted=interrupted,
            error_code=error_code,
        )
        turn_id_str = f"turn-{seq}"
        prefix = "user" if kind == "user" else "assistant"
        if self._translator is None:
            await self._notify(
                f"{prefix}.translation.unavailable", {"reason": "disabled"}, turn_id_str
            )
        elif error_code:
            await self._notify(
                f"{prefix}.translation.unavailable", {"reason": "failed"}, turn_id_str
            )
        else:
            await self._notify(
                f"{prefix}.translation.done",
                {"text": translated, "model": self._translation_model},
                turn_id_str,
            )
        await self._notify("turn.completed", {"turn": turn}, turn_id_str)
