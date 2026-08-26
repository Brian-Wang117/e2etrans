"""WorkbenchHub resilience: a half-open (zombie) connection must never block
broadcasts or stall the scheduler's drive loop."""

import asyncio
import time

from app.batch.hub import WorkbenchHub


class HangingSocket:
    async def send_json(self, event):
        await asyncio.sleep(30)


class RecordingSocket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(event)


async def test_broadcast_drops_hanging_connection_without_blocking():
    hub = WorkbenchHub(send_timeout_seconds=0.1)
    hanging, good = HangingSocket(), RecordingSocket()
    await hub.register(hanging)
    await hub.register(good)
    started = time.monotonic()
    await hub.broadcast({"type": "ping"})
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert good.events == [{"type": "ping"}]
    assert hub.connection_count == 1  # the zombie was cleaned up


async def test_send_bridge_times_out_and_unregisters():
    hub = WorkbenchHub(send_timeout_seconds=0.1)
    hanging = HangingSocket()
    await hub.register(hanging)
    await hub.set_bridge(hanging)
    assert await hub.send_bridge({"type": "bridge.dial"}) is False
    assert hub.bridge_online is False
