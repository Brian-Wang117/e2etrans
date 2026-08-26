"""In-process call event bus.

Decouples the realtime gateway (publisher) from the batch scheduler
(subscriber): the gateway emits call lifecycle events without knowing who
listens. Bounded queues with drop-on-overflow keep a slow subscriber from
backing up the audio path; dropping ``activity`` events is harmless because
they only reset a timer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

EVENT_CALL_FINISHED = "call_finished"
EVENT_ACTIVITY = "activity"


@dataclass(frozen=True)
class CallEvent:
    kind: str
    session_id: str
    customer_id: int | None = None
    payload: dict[str, object] = field(default_factory=dict)


class CallEventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[CallEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self) -> None:
        """Record the loop owning the subscribers, so publishers calling from
        foreign threads (tests, worker threads) can hop onto it safely."""
        self._loop = asyncio.get_running_loop()

    def subscribe(self) -> asyncio.Queue[CallEvent]:
        queue: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[CallEvent]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: CallEvent) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and self._loop.is_running() and running is not self._loop:
            # asyncio queues are loop-bound; wake the subscribers through
            # their own loop instead of touching them cross-thread.
            self._loop.call_soon_threadsafe(self._deliver, event)
        else:
            self._deliver(event)

    def _deliver(self, event: CallEvent) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
