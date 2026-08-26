"""Batch runner: executes scheduler actions with real I/O.

The scheduler decides; this module dials through the workbench hub,
persists status changes, personalizes each customer, and merges the three
event sources (call bus, bridge messages, timers) that can end a call.
Repository access is synchronous here: SQLite operations are millisecond
scale and serializing them through the event loop keeps the state machine
single-threaded and race-free.
"""

from __future__ import annotations

import asyncio
import logging

from app.batch import scheduler as sched
from app.batch.events import EVENT_ACTIVITY, EVENT_CALL_FINISHED, CallEventBus
from app.batch.hub import WorkbenchHub
from app.batch.import_parser import extract_gender
from app.batch.personalizer import (
    CACHE_KEY,
    Personalizer,
    build_opening_text,
    extract_name,
    extract_title,
)
from app.storage import Repository

logger = logging.getLogger(__name__)

DIAL_TIMEOUT_REASON = "呼叫超时，未接通"


class BatchRunnerError(RuntimeError):
    pass


class BatchRunner:
    def __init__(
        self,
        *,
        repository: Repository,
        hub: WorkbenchHub,
        bus: CallEventBus,
        personalizer: Personalizer | None,
        inter_call_seconds: float = sched.INTER_CALL_SECONDS,
        activity_timeout_seconds: float = sched.ACTIVITY_TIMEOUT_SECONDS,
    ) -> None:
        self._repository = repository
        self._hub = hub
        self._bus = bus
        self._bus_queue = bus.subscribe()
        self._personalizer = personalizer
        self._inter_call_seconds = inter_call_seconds
        self._activity_timeout_seconds = activity_timeout_seconds
        self._bridge_events: asyncio.Queue[dict] = asyncio.Queue()
        self._scheduler: sched.BatchScheduler | None = None
        self._batch_id: str | None = None
        self._batch_template: dict[str, object] | None = None
        self._last_session_id: str | None = None
        self._task: asyncio.Task | None = None
        hub.on_bridge_message = self._on_bridge_message
        hub.on_bridge_change = self._on_bridge_change

    # -- public controls ----------------------------------------------------------

    @property
    def hub(self) -> WorkbenchHub:
        return self._hub

    @property
    def bus(self) -> CallEventBus:
        return self._bus

    @property
    def running(self) -> bool:
        return (
            self._scheduler is not None
            and self._scheduler.state
            not in (sched.STATE_STOPPED, sched.STATE_COMPLETED)
        )

    @property
    def active_batch_id(self) -> str | None:
        return self._batch_id if self.running else None

    def status(self) -> dict[str, object]:
        return {
            "running": self.running,
            "batch_id": self.active_batch_id,
            "state": self._scheduler.state if self._scheduler else "idle",
            "bridge_online": self._hub.bridge_online,
        }

    async def start_batch(self, batch_id: str) -> dict[str, object]:
        if self.running:
            raise BatchRunnerError("已有批次正在运行")
        batch = self._repository.get_batch(batch_id)
        if batch is None:
            raise BatchRunnerError("批次不存在")
        if batch["status"] not in ("ready", "stopped"):
            raise BatchRunnerError(f"批次状态 {batch['status']} 不允许启动")
        if not self._hub.bridge_online:
            raise BatchRunnerError("桥接页不在线，请先打开工作台页面")
        # Drop stale bridge events queued while no batch was running.
        while not self._bridge_events.empty():
            self._bridge_events.get_nowait()
        self._bus.bind()
        self._repository.set_batch_status(batch_id, "running")
        self._batch_id = batch_id
        # Template snapshot chosen at confirm time; a template deleted after
        # confirm degrades to the legacy .env behaviour instead of failing.
        template_id = batch.get("template_id")
        self._batch_template = (
            self._repository.get_template(int(template_id))
            if isinstance(template_id, int)
            else None
        )
        self._last_session_id = None
        self._scheduler = sched.BatchScheduler(
            batch_id, inter_call_seconds=self._inter_call_seconds
        )
        first = self._repository.next_pending_customer(batch_id)
        # The drive loop spans whole calls; run it in the background so the
        # REST request returns immediately.
        self._task = asyncio.create_task(self._drive(self._scheduler.start(first)))
        return self.status()

    async def stop_batch(self) -> dict[str, object]:
        if self._scheduler is None:
            raise BatchRunnerError("没有正在运行的批次")
        actions = self._scheduler.stop()
        await self._execute_many(actions)
        # Fallback: when the drive task is dead (crashed or cancelled) the
        # graceful stop flag can never land, so force the terminal state
        # instead of leaving the batch "running" forever.
        if self.running and (self._task is None or self._task.done()):
            self._scheduler.force_stop()
            if self._batch_id is not None:
                self._repository.set_batch_status(self._batch_id, "stopped")
            await self._hub.broadcast(
                {"type": "batch.state", "batch_id": self._batch_id, "status": "stopped"}
            )
        return self.status()

    # -- hub callbacks --------------------------------------------------------------

    async def _on_bridge_message(self, message: dict) -> None:
        self._bridge_events.put_nowait(message)

    async def _on_bridge_change(self, online: bool, in_call: bool = False) -> None:
        if not self.running:
            # Idle: keep the dashboards informed directly; the scheduler is
            # not consuming the bridge queue.
            await self._hub.broadcast({"type": "bridge.status", "online": online})
        self._bridge_events.put_nowait(
            {"type": "bridge.change", "online": online, "in_call": in_call}
        )

    # -- action execution -------------------------------------------------------------

    async def _drive(self, actions: list[sched.SchedulerAction]) -> None:
        try:
            await self._drive_inner(actions)
        except asyncio.CancelledError:
            # CancelledError is a BaseException: the generic handler below
            # would miss it and the batch would stay "running" with a dead
            # drive loop.
            logger.warning("batch drive loop cancelled")
            await self._mark_stopped()
        except Exception:
            logger.exception("batch drive loop crashed")
            await self._mark_stopped()

    async def _mark_stopped(self) -> None:
        if self._batch_id is None:
            return
        # The drive loop is gone: no action will ever carry the terminal
        # transition, so settle the scheduler state here too.
        if self._scheduler is not None and self.running:
            self._scheduler.force_stop()
        try:
            self._repository.set_batch_status(self._batch_id, "stopped")
            await self._hub.broadcast(
                {"type": "batch.state", "batch_id": self._batch_id, "status": "stopped"}
            )
        except Exception:
            logger.exception("failed to mark batch stopped")

    async def _drive_inner(self, actions: list[sched.SchedulerAction]) -> None:
        # Terminal transitions (stopped/completed) are carried by the actions
        # themselves (UpdateBatch + progress broadcast), so every queued
        # action must be performed; the loop simply drains.
        pending = list(actions)
        while pending:
            action = pending.pop(0)
            follow_up = await self._perform(action)
            if follow_up:
                pending = list(follow_up) + pending

    async def _execute_many(self, actions: list[sched.SchedulerAction]) -> None:
        for action in actions:
            await self._perform(action)

    async def _perform(self, action: sched.SchedulerAction):
        if isinstance(action, sched.Prepare):
            return await self._prepare(action)
        if isinstance(action, sched.Dial):
            return await self._dial(action)
        if isinstance(action, sched.CheckPending):
            next_customer = self._repository.next_pending_customer(self._batch_id)
            if next_customer is None:
                return self._scheduler.after_wait(None)
            return [
                sched.Wait(seconds=self._inter_call_seconds),
                *self._scheduler.after_wait(next_customer),
            ]
        if isinstance(action, sched.Wait):
            return await self._wait(action)
        if isinstance(action, sched.Hangup):
            self._hub.mark_bridge_call_state(False)
            await self._hub.send_bridge({"type": "bridge.hangup"})
            return []
        if isinstance(action, sched.UpdateCustomer):
            await self._update_customer(action)
            return []
        if isinstance(action, sched.UpdateBatch):
            if self._batch_id is not None:
                self._repository.set_batch_status(self._batch_id, action.status)
            await self._hub.broadcast(
                {"type": "batch.state", "batch_id": self._batch_id, "status": action.status}
            )
            return []
        if isinstance(action, sched.Broadcast):
            await self._hub.broadcast({"type": action.event, **action.payload})
            return []
        return []

    async def _prepare(self, action: sched.Prepare):
        customer = action.customer
        raw_data = dict(customer.get("raw_data") or {})
        template = self._batch_template
        if template is not None:
            # A template carries a company-authored background: use it
            # verbatim (what the B-side wrote is what the bot says) and skip
            # the LLM per-customer personalizer entirely.
            parts = [
                str(template.get("company_name") or "").strip(),
                str(template.get("business_background") or "").strip(),
            ]
            background = " ".join(part for part in parts if part)
            opening_template = str(template.get("opening_template") or "").strip()
            if opening_template:
                opening = (
                    opening_template.replace("{name}", extract_name(raw_data))
                    .replace("{title}", extract_title(raw_data))
                )
            else:
                opening = build_opening_text(raw_data)
            return self._scheduler.personalized(opening, background)
        opening = ""
        background = ""
        if self._personalizer is not None:
            try:
                personalization = await self._personalizer.personalize(raw_data)
            except Exception:
                logger.exception("personalization crashed; using empty personalization")
                personalization = None
            if personalization is not None:
                opening = personalization.opening_text
                background = personalization.business_background
                if personalization.generated:
                    raw_data[CACHE_KEY] = background
                    self._repository.update_customer_raw_data(customer["id"], raw_data)
        return self._scheduler.personalized(opening, background)

    async def _dial(self, action: sched.Dial):
        customer = action.customer
        template = self._batch_template
        sent = await self._hub.send_bridge(
            {
                "type": "bridge.dial",
                "customer_id": customer["id"],
                "phone": customer.get("phone", ""),
                "opening_text": action.opening_text,
                "business_background": action.business_background,
                "gender": extract_gender(customer.get("raw_data") or {}),
                "bot_name": str((template or {}).get("bot_name") or ""),
                "speaking_style": str((template or {}).get("speaking_style") or ""),
                "template_id": (template or {}).get("id"),
            }
        )
        if not sent:
            return self._scheduler.call_failed("桥接页离线，无法拨打")
        return await self._await_call_phase(customer)

    async def _wait(self, action: sched.Wait):
        seconds = int(action.seconds)
        for remaining in range(seconds, 0, -1):
            await self._hub.broadcast({"type": "countdown", "seconds": remaining})
            await asyncio.sleep(1)
        return []

    async def _update_customer(self, action: sched.UpdateCustomer) -> None:
        session_id = (
            self._last_session_id if action.status == "已完成" else None
        )
        self._repository.update_customer_status(
            action.customer_id,
            action.status,
            result=action.result,
            reason=action.reason,
            duration_seconds=action.duration_seconds,
            session_id=session_id,
        )
        if action.status in ("已完成", "失败"):
            self._repository.increment_batch_done(self._batch_id)
        await self._hub.broadcast(
            {
                "type": "customer.updated",
                "customer_id": action.customer_id,
                "status": action.status,
                "result": action.result,
                "reason": action.reason,
                "duration_seconds": action.duration_seconds,
            }
        )

    # -- call phase: merge bus / bridge / timer sources -------------------------------

    async def _await_call_phase(self, customer: dict):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._activity_timeout_seconds
        customer_id = int(customer["id"])
        bus_get = asyncio.create_task(self._bus_queue.get())
        bridge_get = asyncio.create_task(self._bridge_events.get())
        try:
            while self._scheduler.state in (
                sched.STATE_DIALING,
                sched.STATE_IN_CALL,
                sched.STATE_PAUSED,
            ):
                remaining = max(deadline - loop.time(), 0)
                done, _ = await asyncio.wait(
                    {bus_get, bridge_get},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    return self._on_call_timeout()
                if bus_get in done:
                    event = bus_get.result()
                    bus_get = asyncio.create_task(self._bus_queue.get())
                    if event.customer_id == customer_id:
                        if (
                            event.kind == EVENT_ACTIVITY
                            and self._scheduler.state
                            in (sched.STATE_IN_CALL, sched.STATE_PAUSED)
                        ):
                            deadline = loop.time() + self._activity_timeout_seconds
                        elif event.kind == EVENT_CALL_FINISHED:
                            self._last_session_id = event.session_id
                            self._hub.mark_bridge_call_state(False)
                            return self._scheduler.call_finished(dict(event.payload))
                if bridge_get in done:
                    message = bridge_get.result()
                    bridge_get = asyncio.create_task(self._bridge_events.get())
                    result = await self._handle_bridge_event(
                        message, customer_id, loop
                    )
                    if result is not None:
                        return result
        finally:
            bus_get.cancel()
            bridge_get.cancel()
        return []

    def _on_call_timeout(self):
        if self._scheduler.state == sched.STATE_DIALING:
            return self._scheduler.call_failed(DIAL_TIMEOUT_REASON)
        return self._scheduler.activity_timeout()

    async def _handle_bridge_event(self, message: dict, customer_id: int, loop):
        message_type = message.get("type")
        if message_type == "bridge.call_connected":
            self._hub.mark_bridge_call_state(True)
            await self._execute_many(self._scheduler.call_connected())
            return None
        if message_type == "bridge.call_failed":
            self._hub.mark_bridge_call_state(False)
            reason = str(message.get("reason") or "呼叫失败")
            return self._scheduler.call_failed(reason)
        if message_type == "bridge.call_ended":
            self._hub.mark_bridge_call_state(False)
            return None
        if message_type == "bridge.change":
            if message.get("online"):
                actions = self._scheduler.bridge_online(
                    in_call=bool(message.get("in_call"))
                )
            else:
                actions = self._scheduler.bridge_offline()
            await self._execute_many(actions)
            return None
        return None
