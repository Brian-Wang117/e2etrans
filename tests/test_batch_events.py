"""Tests for the call event bus and the gateway publish hooks."""

import asyncio

import pytest

from app.batch.events import (
    EVENT_ACTIVITY,
    EVENT_CALL_FINISHED,
    CallEvent,
    CallEventBus,
)
from app.config import OutboundSettings
from app.outbound import call_director
from app.realtime.gateway import OUTBOUND_SCENARIO, RealtimeGateway
from tests.test_gateway import FakeDoubao, FakeWebSocket, envelope, find, wait_until


@pytest.fixture(autouse=True)
def fast_speech_estimates(monkeypatch):
    """Shrink TTS playback estimates so hang-ups happen quickly in tests."""
    monkeypatch.setattr(call_director, "SPEECH_SECONDS_PER_CHAR", 0.001)
    monkeypatch.setattr(call_director, "SPEECH_BUFFER_SECONDS", 0.05)
    monkeypatch.setattr(call_director, "GOODBYE_MARGIN_SECONDS", 0.05)


# -- bus unit tests -------------------------------------------------------------


def test_bus_delivers_to_all_subscribers():
    bus = CallEventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    event = CallEvent(kind=EVENT_ACTIVITY, session_id="s1")
    bus.publish(event)
    assert q1.get_nowait() is event
    assert q2.get_nowait() is event
    assert bus.subscriber_count == 2


def test_bus_unsubscribe_stops_delivery():
    bus = CallEventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)
    bus.publish(CallEvent(kind=EVENT_ACTIVITY, session_id="s1"))
    assert queue.empty()
    assert bus.subscriber_count == 0


def test_bus_drops_on_overflow_instead_of_blocking():
    bus = CallEventBus(queue_size=1)
    queue = bus.subscribe()
    bus.publish(CallEvent(kind=EVENT_ACTIVITY, session_id="s1"))
    bus.publish(CallEvent(kind=EVENT_ACTIVITY, session_id="s2"))  # dropped
    assert queue.qsize() == 1
    assert queue.get_nowait().session_id == "s1"


# -- gateway publish hooks --------------------------------------------------------


def make_gateway_with_bus(settings, repository, doubao, bus):
    def factory(session_id, input_mode, persona=None, speaker=None):
        return doubao

    return RealtimeGateway(
        settings=settings,
        repository=repository,
        doubao_factory=factory,
        translator=None,
        scenarios={OUTBOUND_SCENARIO: "默认开场白"},
        outbound_settings=OutboundSettings(enabled=True),
        adjudicator=None,
        call_events=bus,
    )


def start_payload(customer_id):
    return envelope(
        "session.start",
        1,
        {
            "scenario_id": OUTBOUND_SCENARIO,
            "opening_text": "您好，这里是活动回访。",
            "customer_id": customer_id,
        },
    )


async def test_gateway_publishes_activity_and_call_finished(
    repository, settings
):
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    bus = CallEventBus()
    queue = bus.subscribe()
    gateway = make_gateway_with_bus(settings, repository, doubao, bus)
    task = asyncio.create_task(gateway.run(websocket))
    events: list[CallEvent] = []
    try:
        websocket.feed(start_payload(customer_id=42))
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Script hit -> user-final activity + one-shot report.
        doubao.emit(451, {"results": [{"text": "我要投诉", "is_interim": False}]})
        await wait_until(lambda: find(websocket.sent, "result.reported"))
        doubao.emit(459, {})
        await asyncio.wait_for(task, timeout=10)

        while not queue.empty():
            events.append(queue.get_nowait())
        kinds = [event.kind for event in events]
        assert EVENT_ACTIVITY in kinds
        assert kinds.count(EVENT_CALL_FINISHED) == 1
        finished = next(e for e in events if e.kind == EVENT_CALL_FINISHED)
        assert finished.customer_id == 42
        assert finished.payload["result"] == "不感兴趣"
        activity = next(e for e in events if e.kind == EVENT_ACTIVITY)
        assert activity.customer_id == 42
        # The stored call result carries the customer linkage too.
        sessions = await asyncio.to_thread(repository.list_sessions)
        call_result = await asyncio.to_thread(
            repository.get_call_result, sessions[0]["id"]
        )
        assert call_result["customer_id"] == 42
    finally:
        if not task.done():
            task.cancel()


async def test_no_events_without_bus(repository, settings):
    """Legacy wiring (no bus) must behave exactly as before."""
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    gateway = make_gateway_with_bus(settings, repository, doubao, None)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_payload(customer_id=7))
        await wait_until(lambda: find(websocket.sent, "session.ready"))
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
