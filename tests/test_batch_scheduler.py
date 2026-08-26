"""Tests for the batch scheduler pure state machine."""

from app.batch.scheduler import (
    Broadcast,
    Dial,
    Hangup,
    Prepare,
    UpdateBatch,
    UpdateCustomer,
    Wait,
    BatchScheduler,
)

CUSTOMER_1 = {"id": 1, "row_number": 2, "phone": "13800000001"}
CUSTOMER_2 = {"id": 2, "row_number": 3, "phone": "13800000002"}


def kinds(actions):
    return [type(action).__name__ for action in actions]


def test_start_prepares_first_customer():
    scheduler = BatchScheduler("B-1")
    actions = scheduler.start(CUSTOMER_1)
    assert kinds(actions) == ["Prepare", "Broadcast"]
    assert actions[0].customer == CUSTOMER_1
    assert scheduler.state == "preparing"


def test_start_empty_batch_completes():
    scheduler = BatchScheduler("B-1")
    actions = scheduler.start(None)
    assert kinds(actions) == ["UpdateBatch", "Broadcast"]
    assert actions[0].status == "completed"
    assert scheduler.state == "completed"


def test_personalized_dials_with_personalization():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    actions = scheduler.personalized("您好，请问是王芳女士吗？", "背景")
    assert kinds(actions) == ["UpdateCustomer", "Broadcast", "Dial"]
    # The "进行中" write must precede Dial: Dial blocks for the whole call,
    # and a later write would clobber the terminal status.
    assert actions[0].status == "进行中"
    dial = actions[2]
    assert dial.opening_text == "您好，请问是王芳女士吗？"
    assert dial.customer == CUSTOMER_1
    assert scheduler.state == "dialing"


def test_call_finished_waits_then_advances():
    scheduler = BatchScheduler("B-1", inter_call_seconds=10)
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    actions = scheduler.call_finished(
        {"result": "感兴趣", "reason": "终裁", "duration_seconds": 33.5}
    )
    assert kinds(actions) == ["UpdateCustomer", "Broadcast", "CheckPending"]
    update = actions[0]
    assert update.status == "已完成" and update.result == "感兴趣"
    assert update.duration_seconds == 33.5

    actions = scheduler.after_wait(CUSTOMER_2)
    assert kinds(actions) == ["Prepare", "Broadcast"]
    assert actions[0].customer == CUSTOMER_2


def test_after_wait_none_completes_batch():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.call_finished({"result": "中立"})
    actions = scheduler.after_wait(None)
    assert kinds(actions) == ["UpdateBatch", "Broadcast"]
    assert actions[0].status == "completed"
    assert scheduler.state == "completed"


def test_call_failed_continues_batch():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    actions = scheduler.call_failed("无人接听")
    assert kinds(actions) == ["UpdateCustomer", "Broadcast", "CheckPending"]
    assert actions[0].status == "失败"
    assert actions[0].reason == "无人接听"
    # The loop keeps going.
    actions = scheduler.after_wait(CUSTOMER_2)
    assert actions[0].customer == CUSTOMER_2


def test_activity_timeout_hangs_up_and_fails():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    actions = scheduler.activity_timeout()
    assert kinds(actions) == ["Hangup", "UpdateCustomer", "Broadcast", "CheckPending"]
    assert actions[1].status == "失败"
    assert actions[1].reason == "通话异常超时"


def test_activity_timeout_ignored_outside_call():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    assert scheduler.activity_timeout() == []  # still preparing


def test_stop_waits_for_current_call():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    actions = scheduler.stop()
    assert kinds(actions) == ["Broadcast"]  # stopping notice only
    assert scheduler.state == "in_call"
    # Current call still finishes normally…
    actions = scheduler.call_finished({"result": "感兴趣"})
    # …but instead of waiting for the next customer the batch stops.
    assert kinds(actions) == ["UpdateCustomer", "Broadcast", "UpdateBatch", "Broadcast"]
    assert actions[2].status == "stopped"
    assert scheduler.state == "stopped"


def test_stop_without_active_call_stops_immediately():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.call_finished({"result": "中立"})
    assert scheduler.state == "waiting"
    actions = scheduler.stop()
    assert kinds(actions) == ["UpdateBatch", "Broadcast"]
    assert scheduler.state == "stopped"


def test_stop_while_preparing_skips_the_dial():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    assert scheduler.stop() != []  # preparing: only a stopping notice
    # Personalization completes, but no call was placed yet → stop now.
    actions = scheduler.personalized("开场", "背景")
    assert kinds(actions) == ["UpdateBatch", "Broadcast"]
    assert actions[0].status == "stopped"
    assert scheduler.state == "stopped"
    assert not any(isinstance(action, Dial) for action in actions)


def test_stopped_scheduler_ignores_events():
    scheduler = BatchScheduler("B-1")
    scheduler.start(None)
    assert scheduler.stop() == []
    assert scheduler.call_finished({"result": "感兴趣"}) == []
    assert scheduler.start(CUSTOMER_1) == []


def test_bridge_offline_pauses_and_online_resumes_live_call():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    actions = scheduler.bridge_offline()
    assert kinds(actions) == ["Broadcast"]
    assert actions[0].payload == {"online": False}
    assert scheduler.state == "paused"
    # Bridge returns reporting the call is still alive: resume in place.
    actions = scheduler.bridge_online(in_call=True)
    assert kinds(actions) == ["Broadcast", "Broadcast"]
    assert scheduler.state == "in_call"
    # And the normal end path still works afterwards.
    actions = scheduler.call_finished({"result": "感兴趣"})
    assert actions[0].status == "已完成"


def test_bridge_online_without_live_call_fails_customer():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.bridge_offline()
    actions = scheduler.bridge_online(in_call=False)
    assert kinds(actions) == ["Broadcast", "UpdateCustomer", "Broadcast", "CheckPending"]
    assert actions[1].status == "失败"
    assert actions[1].reason == "桥接页断开，通话丢失"


def test_call_finished_accepted_while_paused():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.bridge_offline()
    actions = scheduler.call_finished({"result": "中立"})
    assert actions[0].status == "已完成"


def test_bridge_online_when_waiting_schedules_wait():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.call_finished({"result": "中立"})
    scheduler.bridge_offline()
    actions = scheduler.bridge_online()
    assert kinds(actions) == ["Broadcast", "CheckPending"]
    assert scheduler.state == "waiting"


def test_single_batch_no_double_start():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    assert scheduler.start(CUSTOMER_2) == []


def test_done_counter_tracks_finished_customers():
    scheduler = BatchScheduler("B-1")
    scheduler.start(CUSTOMER_1)
    scheduler.personalized("开场", "背景")
    scheduler.call_connected()
    scheduler.call_finished({"result": "中立"})
    progress = [
        action for action in scheduler.after_wait(CUSTOMER_2) if isinstance(action, Broadcast)
    ][0]
    assert progress.payload["done"] == 1
    assert progress.payload["index"] == 2
