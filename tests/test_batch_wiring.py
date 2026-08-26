"""Integration tests: REST + /ws/workbench wiring for batch outbound.

A FakeBridge (a TestClient websocket speaking the bridge protocol) drives
the full loop: import -> confirm -> start -> dial -> call events -> next
customer, while spectator connections receive the broadcasts.
"""

import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.batch.events import EVENT_CALL_FINISHED, CallEvent
from app.batch.runner import BatchRunner
from app.config import OutboundSettings, Settings
from app.main import create_app
from app.storage import Repository

CSV_BYTES = "姓名,电话\n王女士,13800000001\n李先生,13800000002\n".encode("utf-8")


@pytest.fixture
def wiring(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        outbound=OutboundSettings(enabled=True),
    )
    repository = Repository(settings.database_path)
    # Build the runner with fast countdowns first, then inject it so the WS
    # handler and the runner share the same hub/bus.
    from app.batch.events import CallEventBus
    from app.batch.hub import WorkbenchHub

    hub = WorkbenchHub()
    bus = CallEventBus()
    runner = BatchRunner(
        repository=repository,
        hub=hub,
        bus=bus,
        personalizer=None,
        inter_call_seconds=1,
        activity_timeout_seconds=5,
    )
    app = create_app(
        settings,
        repository=repository,
        batch_runner=runner,
    )
    return {"app": app, "repository": repository, "runner": runner, "bus": bus, "hub": hub}


def import_and_confirm(client: TestClient, csv_bytes: bytes = CSV_BYTES) -> str:
    response = client.post(
        "/api/batches/import", files={"file": ("customers.csv", csv_bytes, "text/csv")}
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]
    response = client.post(f"/api/batches/{batch_id}/confirm", json={})
    assert response.status_code == 200, response.text
    return batch_id


def receive_until(websocket, message_type: str, limit: int = 100) -> dict:
    for _ in range(limit):
        message = websocket.receive_json()
        if message.get("type") == message_type:
            return message
    raise AssertionError(f"never received {message_type!r}")


def test_import_preview_and_confirm(wiring):
    with TestClient(wiring["app"]) as client:
        response = client.post(
            "/api/batches/import",
            files={"file": ("customers.csv", CSV_BYTES, "text/csv")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["phone_column"] == "电话"
        assert body["status"] == "draft"
        assert len(body["preview"]) == 2
        assert body["preview"][0]["phone"] == "13800000001"

        batch_id = body["batch_id"]
        response = client.post(f"/api/batches/{batch_id}/confirm", json={})
        assert response.status_code == 200
        assert response.json()["batch"]["status"] == "ready"

        response = client.get("/api/batches/latest")
        assert response.json()["batch"]["id"] == batch_id

        response = client.get(f"/api/batches/{batch_id}/customers")
        customers = response.json()["customers"]
        assert [c["phone"] for c in customers] == ["13800000001", "13800000002"]
        assert all(c["status"] == "待呼叫" for c in customers)


def test_import_rejects_unsupported_file(wiring):
    with TestClient(wiring["app"]) as client:
        response = client.post(
            "/api/batches/import", files={"file": ("list.txt", b"a,b\n1,2\n")}
        )
        assert response.status_code == 400


def test_confirm_with_changed_phone_column_reextracts(wiring):
    csv_bytes = "姓名,电话,备用号码\n王女士,13800000001,13900000001\n".encode("utf-8")
    with TestClient(wiring["app"]) as client:
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", csv_bytes, "text/csv")}
        )
        batch_id = response.json()["batch_id"]
        assert response.json()["phone_column"] == "电话"
        response = client.post(
            f"/api/batches/{batch_id}/confirm", json={"phone_column": "备用号码"}
        )
        assert response.status_code == 200
        customers = client.get(f"/api/batches/{batch_id}/customers").json()["customers"]
        assert customers[0]["phone"] == "13900000001"


def test_export_batch_csv_includes_outcomes(wiring):
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        response = client.get(f"/api/batches/{batch_id}/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert f"{batch_id}.csv" in response.headers["content-disposition"]
        # utf-8-sig: the leading BOM keeps Chinese intact when opened in Excel.
        assert response.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert rows[0] == [
            "行号", "姓名", "电话", "拨打号码", "状态", "结果", "原因", "时长(秒)", "完成时间",
        ]
        assert rows[1][1] == "王女士"
        assert rows[1][3] == "13800000001"
        assert rows[1][4] == "待呼叫"
        assert len(rows) == 3


def test_export_batch_not_found(wiring):
    with TestClient(wiring["app"]) as client:
        assert client.get("/api/batches/B-missing/export").status_code == 404


def test_start_requires_ready_batch_and_bridge(wiring):
    with TestClient(wiring["app"]) as client:
        # No bridge online yet.
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch_id = response.json()["batch_id"]
        response = client.post(f"/api/batches/{batch_id}/start")
        assert response.status_code == 409  # draft: not confirmed
        client.post(f"/api/batches/{batch_id}/confirm", json={})
        response = client.post(f"/api/batches/{batch_id}/start")
        assert response.status_code == 409  # confirmed, but no bridge page
        assert "桥接" in response.json()["detail"]


def test_full_serial_call_flow(wiring):
    repository, runner, bus = wiring["repository"], wiring["runner"], wiring["bus"]
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        customers = client.get(f"/api/batches/{batch_id}/customers").json()["customers"]
        first_id, second_id = int(customers[0]["id"]), int(customers[1]["id"])

        with client.websocket_connect("/ws/workbench") as bridge:
            hello = bridge.receive_json()
            assert hello["type"] == "hello"
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)

            response = client.post(f"/api/batches/{batch_id}/start")
            assert response.status_code == 200
            assert response.json()["running"] is True
            # Idempotent start returns the same running batch.
            assert client.post(f"/api/batches/{batch_id}/start").status_code == 200

            dial = receive_until(bridge, "bridge.dial")
            assert dial["phone"] == "13800000001"
            assert dial["customer_id"] == first_id
            bridge.send_json({"type": "bridge.call_connected"})

            # Subsystem 1 gateway reports the finished call on the bus.
            bus.publish(
                CallEvent(
                    kind=EVENT_CALL_FINISHED,
                    session_id="sess-1",
                    customer_id=first_id,
                    payload={"result": "感兴趣", "reason": "", "duration_seconds": 12.5},
                )
            )

            dial = receive_until(bridge, "bridge.dial")
            assert dial["phone"] == "13800000002"
            assert dial["customer_id"] == second_id
            bridge.send_json({"type": "bridge.call_connected"})
            bus.publish(
                CallEvent(
                    kind=EVENT_CALL_FINISHED,
                    session_id="sess-2",
                    customer_id=second_id,
                    payload={"result": "不感兴趣", "reason": "", "duration_seconds": 4.0},
                )
            )

            state = receive_until(bridge, "batch.state")
            assert state["status"] == "completed"

        batch = repository.get_batch(batch_id)
        assert batch["status"] == "completed"
        assert batch["done"] == 2
        done = repository.list_customers(batch_id)
        assert all(c["status"] == "已完成" for c in done)
        assert done[0]["session_id"] == "sess-1"
        assert done[0]["result"] == "感兴趣"
        assert runner.running is False

        # Transcript endpoint resolves the stored session link.
        response = client.get(
            f"/api/batches/{batch_id}/customers/{first_id}/transcript"
        )
        assert response.status_code == 200
        assert response.json()["session_id"] == "sess-1"


def test_sip_failure_marks_customer_failed_and_continues(wiring):
    repository, bus = wiring["repository"], wiring["bus"]
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        customers = client.get(f"/api/batches/{batch_id}/customers").json()["customers"]
        first_id, second_id = int(customers[0]["id"]), int(customers[1]["id"])
        with client.websocket_connect("/ws/workbench") as bridge:
            bridge.receive_json()
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)
            client.post(f"/api/batches/{batch_id}/start")
            dial = receive_until(bridge, "bridge.dial")
            assert dial["customer_id"] == first_id
            bridge.send_json({"type": "bridge.call_failed", "reason": "无人接听"})
            dial = receive_until(bridge, "bridge.dial")
            assert dial["customer_id"] == second_id
            bridge.send_json({"type": "bridge.call_connected"})
            bus.publish(
                CallEvent(
                    kind=EVENT_CALL_FINISHED,
                    session_id="sess-9",
                    customer_id=second_id,
                    payload={"result": "中立", "reason": "", "duration_seconds": 3.0},
                )
            )
            receive_until(bridge, "batch.state")
        rows = {int(c["id"]): c for c in repository.list_customers(batch_id)}
        assert rows[first_id]["status"] == "失败"
        assert rows[first_id]["reason"] == "无人接听"
        assert rows[second_id]["status"] == "已完成"
        assert repository.get_batch(batch_id)["done"] == 2


def test_bridge_status_broadcast_to_spectators(wiring):
    with TestClient(wiring["app"]) as client:
        with client.websocket_connect("/ws/workbench") as spectator:
            spectator.receive_json()  # hello
            with client.websocket_connect("/ws/workbench") as bridge:
                bridge.receive_json()
                bridge.send_json({"type": "workbench.hello", "role": "bridge"})
                message = receive_until(spectator, "bridge.status")
                assert message["online"] is True
            message = receive_until(spectator, "bridge.status")
            assert message["online"] is False


def test_patch_customer_edits_raw_data(wiring):
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        customers = client.get(f"/api/batches/{batch_id}/customers").json()["customers"]
        customer_id = int(customers[0]["id"])
        response = client.patch(
            f"/api/batches/{batch_id}/customers/{customer_id}",
            json={"raw_data": {"姓名": "王女士", "电话": "13700000007"}},
        )
        assert response.status_code == 200
        edited = response.json()["customer"]
        assert edited["phone"] == "13700000007"
        assert edited["raw_data"]["姓名"] == "王女士"


def test_cancelled_drive_loop_marks_batch_stopped(wiring):
    """CancelledError escapes `except Exception`; a dead drive loop must
    still leave the batch in a terminal state, never 'running' forever."""
    repository, runner = wiring["repository"], wiring["runner"]
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        with client.websocket_connect("/ws/workbench") as bridge:
            bridge.receive_json()
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)
            client.post(f"/api/batches/{batch_id}/start")
            receive_until(bridge, "bridge.dial")
            runner._task.cancel()
            time.sleep(0.5)
        assert repository.get_batch(batch_id)["status"] == "stopped"
        assert runner.running is False


def test_stop_forces_terminal_when_drive_loop_dead(wiring):
    """Stop must recover even when the drive task is gone: the graceful
    flag would never land on its own."""
    repository, runner = wiring["repository"], wiring["runner"]
    with TestClient(wiring["app"]) as client:
        batch_id = import_and_confirm(client)
        with client.websocket_connect("/ws/workbench") as bridge:
            bridge.receive_json()
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)
            client.post(f"/api/batches/{batch_id}/start")
            receive_until(bridge, "bridge.dial")
            runner._task = None  # simulate a dead drive loop
            response = client.post(f"/api/batches/{batch_id}/stop")
            assert response.status_code == 200
            assert response.json()["running"] is False
            state = receive_until(bridge, "batch.state")
            assert state["status"] == "stopped"
        assert repository.get_batch(batch_id)["status"] == "stopped"
