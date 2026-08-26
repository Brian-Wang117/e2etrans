"""Single-call state machine: interception, end guards and result reporting.

The director is pure orchestration: it observes finalized ASR text and
assistant replies, then returns ordered :class:`DirectorAction` commands for
the gateway to execute (interrupt, TTS injection, mute, hangup scheduling,
result reporting). Keeping I/O in the gateway makes the state machine fully
testable and leaves the interception point swappable (requirement: evolve
from ASR-final matching to a pre-ASR gate later).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import ClassVar

from app.outbound.adjudicator import AdjudicationResult
from app.outbound.script_library import ScriptMatcher

logger = logging.getLogger(__name__)

FAREWELL_WORDS = ("再见", "再会", "拜拜")
SILENCE_FAREWELL = "您好像有些忙，那就不打扰您了，祝您生活愉快，再见。"
TURN_LIMIT_FAREWELL = "今天先聊到这里，感谢您的耐心，祝您生活愉快，再见。"
SPEECH_SECONDS_PER_CHAR = 0.25
SPEECH_BUFFER_SECONDS = 1.5
GOODBYE_MARGIN_SECONDS = 0.5
SILENCE_RESULT_REASON = "用户沉默超过60秒"
ADJUDICATION_FAILURE_REASON = "终裁失败"


# -- actions the gateway must perform, in order --------------------------------


@dataclass(frozen=True)
class DirectorAction:
    kind: ClassVar[str] = "base"


@dataclass(frozen=True)
class Interrupt(DirectorAction):
    kind: ClassVar[str] = "interrupt"


@dataclass(frozen=True)
class Say(DirectorAction):
    """Inject fixed text straight into TTS (bypasses the large model)."""

    text: str
    source: str  # "script" | "farewell" | "opening"
    kind: ClassVar[str] = "say"


@dataclass(frozen=True)
class MuteInput(DirectorAction):
    kind: ClassVar[str] = "mute_input"


@dataclass(frozen=True)
class ScheduleHangup(DirectorAction):
    delay_seconds: float
    kind: ClassVar[str] = "schedule_hangup"


@dataclass(frozen=True)
class StartAdjudication(DirectorAction):
    kind: ClassVar[str] = "start_adjudication"


@dataclass(frozen=True)
class Report(DirectorAction):
    status: str  # 已完成 | 失败
    result: str  # 感兴趣 | 不感兴趣 | 中立
    reason: str
    end_reason: str
    kind: ClassVar[str] = "report"


@dataclass(frozen=True)
class Notify(DirectorAction):
    event: str  # "script.hit" | "result.reported"
    payload: dict[str, object] = field(default_factory=dict)
    kind: ClassVar[str] = "notify"


def estimate_speech_seconds(text: str) -> float:
    return max(0.5, len(text) * SPEECH_SECONDS_PER_CHAR) + SPEECH_BUFFER_SECONDS


# -- the state machine -----------------------------------------------------------


class CallDirector:
    def __init__(
        self,
        *,
        matcher: ScriptMatcher,
        max_turns: int = 50,
        silence_seconds: float = 60.0,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if silence_seconds <= 0:
            raise ValueError("silence_seconds must be positive")
        self._matcher = matcher
        self.max_turns = max_turns
        self.silence_seconds = silence_seconds
        self.state = "talking"  # talking -> closing -> ended
        self.adjudication_pending = False
        self._reported = False
        self._assistant_turns = 0
        self._dialogue: list[tuple[str, str]] = []

    # -- observation ---------------------------------------------------------------

    def start(self, opening_text: str) -> None:
        """Record the opening greeting as the first agent turn."""
        if opening_text.strip():
            self._dialogue.append(("客服", opening_text.strip()))

    def build_dialogue(self) -> list[tuple[str, str]]:
        return list(self._dialogue)

    def observe_user_final(self, text: str) -> list[DirectorAction]:
        """One finalized customer utterance. Returns interception actions."""
        text = text.strip()
        if not text or self.state != "talking":
            return []
        self._dialogue.append(("客户", text))
        hit = self._matcher.match(text)
        if hit is None:
            return []
        script = hit.script
        self._dialogue.append(("客服", script.reply))
        actions: list[DirectorAction] = [
            Interrupt(),
            Say(text=script.reply, source="script"),
            Notify(
                "script.hit",
                {
                    "category": script.category,
                    "trigger": hit.trigger,
                    "reply": script.reply,
                    "end_call": script.end_call,
                },
            ),
        ]
        if script.verdict and not self._reported:
            actions.extend(
                self._report(
                    status="已完成",
                    result=script.verdict,
                    reason=f"固定话术匹配：{hit.trigger}",
                    end_reason="固定话术结束" if script.end_call else "继续通话",
                )
            )
        if script.end_call:
            self.state = "closing"
            actions.append(MuteInput())
            actions.append(ScheduleHangup(delay_seconds=estimate_speech_seconds(script.reply)))
        return actions

    def observe_assistant_done(self, text: str) -> list[DirectorAction]:
        """One completed assistant reply (natural or injected farewell)."""
        text = text.strip()
        if self.state != "talking":
            return []
        if text:
            self._dialogue.append(("客服", text))
            self._assistant_turns += 1
        if self._assistant_turns >= self.max_turns:
            return self._force_farewell(reason="轮次上限")
        if text and any(word in text for word in FAREWELL_WORDS):
            self.state = "closing"
            self.adjudication_pending = True
            return [MuteInput(), StartAdjudication()]
        return []

    def on_goodbye_played(self) -> list[DirectorAction]:
        """Farewell TTS finished playing (assistant.audio.done while closing)."""
        if self.state != "closing":
            return []
        return [ScheduleHangup(delay_seconds=GOODBYE_MARGIN_SECONDS)]

    def on_silence_timeout(self) -> list[DirectorAction]:
        """No new turn within the silence window."""
        if self.state != "talking":
            return []
        self.state = "closing"
        self._dialogue.append(("客服", SILENCE_FAREWELL))
        actions: list[DirectorAction] = [
            Interrupt(),
            Say(text=SILENCE_FAREWELL, source="farewell"),
            MuteInput(),
        ]
        # Requirement 六: silence means 不感兴趣 directly, no adjudication.
        actions.extend(
            self._report(
                status="已完成",
                result="不感兴趣",
                reason=SILENCE_RESULT_REASON,
                end_reason="客户沉默",
            )
        )
        actions.append(
            ScheduleHangup(delay_seconds=estimate_speech_seconds(SILENCE_FAREWELL))
        )
        return actions

    # -- adjudication outcome --------------------------------------------------

    def apply_adjudication(self, result: AdjudicationResult | None) -> list[DirectorAction]:
        """Feed the (possibly failed) adjudication outcome; reporting is once."""
        self.adjudication_pending = False
        if self._reported:
            return []
        if result is None:
            return self._report(
                status="已完成",
                result="不感兴趣",
                reason=ADJUDICATION_FAILURE_REASON,
                end_reason="正常结束",
            )
        return self._report(
            status="已完成",
            result=result.verdict,
            reason=result.reason or "终裁判定",
            end_reason="正常结束",
        )

    def finish(self, *, abnormal: bool = False) -> list[DirectorAction]:
        """Call ended (client hang-up, session.end, or upstream failure)."""
        self.state = "ended"
        if self._reported or self.adjudication_pending:
            return []
        if abnormal:
            return self._report(
                status="失败",
                result="中立",
                reason="通话异常结束",
                end_reason="异常结束",
            )
        return self._report(
            status="已完成",
            result="中立",
            reason="客户提前挂断",
            end_reason="客户挂断",
        )

    # -- internals ----------------------------------------------------------------

    def _force_farewell(self, *, reason: str) -> list[DirectorAction]:
        self.state = "closing"
        self.adjudication_pending = True
        self._dialogue.append(("客服", TURN_LIMIT_FAREWELL))
        return [
            Interrupt(),
            Say(text=TURN_LIMIT_FAREWELL, source="farewell"),
            MuteInput(),
            StartAdjudication(),
            ScheduleHangup(delay_seconds=estimate_speech_seconds(TURN_LIMIT_FAREWELL)),
        ]

    def _report(
        self, *, status: str, result: str, reason: str, end_reason: str
    ) -> list[DirectorAction]:
        if self._reported:
            return []
        self._reported = True
        return [
            Report(status=status, result=result, reason=reason, end_reason=end_reason),
            Notify(
                "result.reported",
                {"status": status, "result": result, "reason": reason},
            ),
        ]
