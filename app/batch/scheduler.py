"""Batch call scheduler: a pure state machine producing action directives.

Mirrors the CallDirector design from subsystem 1: this module decides, the
runner (module 6) performs I/O — dialing via the workbench WebSocket,
persisting status changes, broadcasting progress. Everything here is
synchronously unit-testable with no I/O.

Single serial loop per batch (requirement 2.4):
    prepare(customer) -> personalized -> dial -> wait for end
    (call_finished | call_failed | activity timeout) -> wait 10s -> next
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

INTER_CALL_SECONDS = 10.0
ACTIVITY_TIMEOUT_SECONDS = 60.0


# -- action directives ----------------------------------------------------------


class SchedulerAction:
    kind: ClassVar[str] = "action"


@dataclass(frozen=True)
class Prepare(SchedulerAction):
    """Runner: personalize this customer, then call ``personalized``."""

    customer: dict
    kind: ClassVar[str] = "prepare"


@dataclass(frozen=True)
class Dial(SchedulerAction):
    customer: dict
    opening_text: str
    business_background: str
    kind: ClassVar[str] = "dial"


@dataclass(frozen=True)
class Wait(SchedulerAction):
    """Countdown before the next dial; runner broadcasts progress."""

    seconds: float = INTER_CALL_SECONDS
    kind: ClassVar[str] = "wait"


@dataclass(frozen=True)
class CheckPending(SchedulerAction):
    """Runner: fetch the next pending customer, then call ``after_wait``.
    Emitted before the countdown so a finished batch completes without a
    pointless wait (and without blocking on the repository lookup)."""

    kind: ClassVar[str] = "check_pending"


@dataclass(frozen=True)
class Hangup(SchedulerAction):
    """Activity timeout: tell the bridge to tear down the current call."""

    reason: str = "通话异常超时"
    kind: ClassVar[str] = "hangup"


@dataclass(frozen=True)
class UpdateCustomer(SchedulerAction):
    customer_id: int
    status: str
    result: str = ""
    reason: str = ""
    duration_seconds: float | None = None
    kind: ClassVar[str] = "update_customer"


@dataclass(frozen=True)
class UpdateBatch(SchedulerAction):
    status: str
    kind: ClassVar[str] = "update_batch"


@dataclass(frozen=True)
class Broadcast(SchedulerAction):
    event: str  # "batch.progress" | "countdown" | "bridge.status" ...
    payload: dict = field(default_factory=dict)
    kind: ClassVar[str] = "broadcast"


# -- scheduler states -------------------------------------------------------------

STATE_IDLE = "idle"
STATE_PREPARING = "preparing"
STATE_DIALING = "dialing"
STATE_IN_CALL = "in_call"
STATE_WAITING = "waiting"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_COMPLETED = "completed"


class BatchScheduler:
    def __init__(
        self,
        batch_id: str,
        *,
        inter_call_seconds: float = INTER_CALL_SECONDS,
    ) -> None:
        self.batch_id = batch_id
        self._inter_call_seconds = inter_call_seconds
        self.state = STATE_IDLE
        self._active: dict | None = None
        self._stop_requested = False
        self._done = 0
        self._index = 0  # how many customers have been started
        self._paused_from: str | None = None

    # -- helpers -----------------------------------------------------------------

    def _progress(self) -> Broadcast:
        return Broadcast(
            "batch.progress",
            {"batch_id": self.batch_id, "state": self.state, "index": self._index, "done": self._done},
        )

    def _finish_customer(
        self,
        status: str,
        *,
        result: str = "",
        reason: str = "",
        duration_seconds: float | None = None,
    ) -> list[SchedulerAction]:
        customer = self._active
        self._active = None
        self._done += 1
        self.state = STATE_STOPPED if self._stop_requested else STATE_WAITING
        actions: list[SchedulerAction] = [
            UpdateCustomer(
                customer_id=int(customer["id"]),
                status=status,
                result=result,
                reason=reason,
                duration_seconds=duration_seconds,
            ),
            self._progress(),
        ]
        if self._stop_requested:
            actions.append(UpdateBatch("stopped"))
            actions.append(self._progress())
            return actions
        actions.append(CheckPending())
        return actions

    # -- public transitions --------------------------------------------------------

    def start(self, first_customer: dict | None) -> list[SchedulerAction]:
        """Begin the loop. ``first_customer`` is None for an empty batch."""
        if self.state != STATE_IDLE:
            return []
        if first_customer is None:
            self.state = STATE_COMPLETED
            return [UpdateBatch("completed"), self._progress()]
        self._index = 1
        self._active = first_customer
        self.state = STATE_PREPARING
        return [Prepare(customer=first_customer), self._progress()]

    def personalized(self, opening_text: str, business_background: str) -> list[SchedulerAction]:
        """Runner finished personalization for the active customer."""
        if self.state != STATE_PREPARING or self._active is None:
            return []
        if self._stop_requested:
            # Stop landed while preparing: the call was never placed, so
            # leave the customer pending and stop without dialing.
            self.state = STATE_STOPPED
            return [UpdateBatch("stopped"), self._progress()]
        self.state = STATE_DIALING
        # UpdateCustomer must precede Dial: Dial blocks for the whole call,
        # and a later "进行中" write would clobber the terminal status.
        return [
            UpdateCustomer(customer_id=int(self._active["id"]), status="进行中"),
            self._progress(),
            Dial(
                customer=self._active,
                opening_text=opening_text,
                business_background=business_background,
            ),
        ]

    def call_connected(self) -> list[SchedulerAction]:
        if self.state != STATE_DIALING:
            return []
        self.state = STATE_IN_CALL
        return [self._progress()]

    def call_finished(self, payload: dict) -> list[SchedulerAction]:
        """The outbound engine reported this call (normal end path). Accepted
        while paused too: the call lives in the browser, not the WS link."""
        if self.state not in (STATE_DIALING, STATE_IN_CALL, STATE_PAUSED):
            return []
        if self._active is None:
            return []
        return self._finish_customer(
            "已完成",
            result=str(payload.get("result", "")),
            reason=str(payload.get("reason", "")),
            duration_seconds=payload.get("duration_seconds"),
        )

    def call_failed(self, reason: str) -> list[SchedulerAction]:
        """SIP-level failure; the batch continues (requirement 2.4-4)."""
        if self.state not in (STATE_DIALING, STATE_IN_CALL, STATE_PAUSED):
            return []
        if self._active is None:
            return []
        return self._finish_customer("失败", reason=reason)

    def activity_timeout(self) -> list[SchedulerAction]:
        """No fresh dialogue for ACTIVITY_TIMEOUT_SECONDS: hang up and fail.
        Also applies while paused when the call was known to be in progress."""
        if self.state == STATE_PAUSED:
            if self._paused_from != STATE_IN_CALL or self._active is None:
                return []
        elif self.state != STATE_IN_CALL or self._active is None:
            return []
        actions: list[SchedulerAction] = [Hangup()]
        actions.extend(self._finish_customer("失败", reason="通话异常超时"))
        return actions

    def after_wait(self, next_customer: dict | None) -> list[SchedulerAction]:
        """Countdown elapsed; runner fetched the next pending customer."""
        if self.state != STATE_WAITING:
            return []
        if self._stop_requested:
            self.state = STATE_STOPPED
            return [UpdateBatch("stopped"), self._progress()]
        if next_customer is None:
            self.state = STATE_COMPLETED
            return [UpdateBatch("completed"), self._progress()]
        self._index += 1
        self._active = next_customer
        self.state = STATE_PREPARING
        return [Prepare(customer=next_customer), self._progress()]

    def stop(self) -> list[SchedulerAction]:
        """Stop after the current call finishes (or immediately if none)."""
        if self.state in (STATE_STOPPED, STATE_COMPLETED):
            return []
        self._stop_requested = True
        if self.state in (STATE_PREPARING, STATE_DIALING, STATE_IN_CALL):
            return [Broadcast("batch.stopping", {"batch_id": self.batch_id})]
        self.state = STATE_STOPPED
        return [UpdateBatch("stopped"), self._progress()]

    def force_stop(self) -> None:
        """Unconditional terminal transition for the stop fallback: used only
        when the drive loop is known to be dead and the graceful stop flag
        can never land."""
        self._stop_requested = True
        self._active = None
        self.state = STATE_STOPPED

    def bridge_offline(self) -> list[SchedulerAction]:
        """The bridge page vanished; pause. The in-flight call may survive in
        the browser, so call events keep being consumed while paused."""
        if self.state in (STATE_STOPPED, STATE_COMPLETED, STATE_PAUSED, STATE_IDLE):
            return []
        self._paused_from = self.state
        self.state = STATE_PAUSED
        return [Broadcast("bridge.status", {"online": False})]

    def bridge_online(self, in_call: bool = False) -> list[SchedulerAction]:
        """Bridge (re)connected. When it reports a live call, resume exactly
        where we paused; otherwise the call is lost — fail the customer and
        continue with the loop."""
        actions: list[SchedulerAction] = [
            Broadcast("bridge.status", {"online": True})
        ]
        if self.state != STATE_PAUSED:
            return actions
        if in_call and self._paused_from in (STATE_DIALING, STATE_IN_CALL):
            self.state = self._paused_from or STATE_IN_CALL
            self._paused_from = None
            actions.append(self._progress())
            return actions
        self._paused_from = None
        if self._active is not None:
            actions.extend(self._finish_customer("失败", reason="桥接页断开，通话丢失"))
            return actions
        self.state = STATE_WAITING
        actions.append(CheckPending())
        return actions

    @property
    def active_customer(self) -> dict | None:
        return self._active

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested
