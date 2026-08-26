"""Storage layer tests for batch outbound: batches, customers, migrations."""

from app.storage import (
    BATCH_COMPLETED,
    BATCH_DRAFT,
    BATCH_READY,
    BATCH_RUNNING,
    CUSTOMER_ACTIVE,
    CUSTOMER_COMPLETED,
    CUSTOMER_FAILED,
    CUSTOMER_PENDING,
    Repository,
)


def make_batch(repository, batch_id="B-20260820-0001", total=3):
    repository.create_batch(
        batch_id, columns=["姓名", "性别", "手机号"], total=total, phone_column="手机号"
    )
    repository.add_customers(
        batch_id,
        [
            {"row_number": 1, "raw_data": {"姓名": "王芳", "性别": "女", "手机号": "13800000001"}, "phone": "13800000001"},
            {"row_number": 2, "raw_data": {"姓名": "李强", "性别": "男", "手机号": "13800000002"}, "phone": "13800000002"},
            {"row_number": 3, "raw_data": {"姓名": "张三", "性别": "男", "手机号": "13800000003"}, "phone": "13800000003"},
        ],
    )
    return batch_id


def test_create_and_get_batch_roundtrip(repository):
    batch_id = make_batch(repository)
    batch = repository.get_batch(batch_id)
    assert batch["id"] == batch_id
    assert batch["total"] == 3
    assert batch["done"] == 0
    assert batch["columns"] == ["姓名", "性别", "手机号"]
    assert batch["phone_column"] == "手机号"
    assert batch["status"] == BATCH_DRAFT


def test_confirm_batch_sets_ready_and_phone_column(repository):
    batch_id = make_batch(repository)
    assert repository.confirm_batch(batch_id, "手机号") is True
    batch = repository.get_batch(batch_id)
    assert batch["status"] == BATCH_READY
    # Confirming twice is rejected (no longer draft).
    assert repository.confirm_batch(batch_id, "姓名") is False
    assert repository.get_batch(batch_id)["phone_column"] == "手机号"


def test_latest_batch_is_most_recent(repository):
    assert repository.get_latest_batch() is None
    make_batch(repository, "B-20260820-0001")
    repository.create_batch("B-20260820-0002", columns=["a"], total=1)
    assert repository.get_latest_batch()["id"] == "B-20260820-0002"


def test_list_customers_preserves_row_order_and_raw_data(repository):
    batch_id = make_batch(repository)
    customers = repository.list_customers(batch_id)
    assert [c["row_number"] for c in customers] == [1, 2, 3]
    assert customers[0]["raw_data"]["姓名"] == "王芳"
    assert customers[0]["status"] == CUSTOMER_PENDING
    # Pagination
    page = repository.list_customers(batch_id, limit=2, offset=1)
    assert [c["row_number"] for c in page] == [2, 3]


def test_next_pending_customer_follows_row_order(repository):
    batch_id = make_batch(repository)
    first = repository.next_pending_customer(batch_id)
    assert first["row_number"] == 1
    repository.update_customer_status(first["id"], CUSTOMER_ACTIVE)
    second = repository.next_pending_customer(batch_id)
    assert second["row_number"] == 2


def test_update_customer_status_records_result_and_finish_time(repository):
    batch_id = make_batch(repository)
    customer = repository.next_pending_customer(batch_id)
    repository.update_customer_status(
        customer["id"],
        CUSTOMER_COMPLETED,
        result="感兴趣",
        reason="客户询问参与方式",
        duration_seconds=42.5,
        session_id="sess-1",
    )
    updated = repository.get_customer(customer["id"])
    assert updated["status"] == CUSTOMER_COMPLETED
    assert updated["result"] == "感兴趣"
    assert updated["duration_seconds"] == 42.5
    assert updated["session_id"] == "sess-1"
    assert updated["finished_at"] is not None


def test_failed_customer_also_counts_towards_done(repository):
    batch_id = make_batch(repository)
    customer = repository.next_pending_customer(batch_id)
    repository.update_customer_status(
        customer["id"], CUSTOMER_FAILED, reason="无人接听"
    )
    repository.increment_batch_done(batch_id)
    batch = repository.get_batch(batch_id)
    assert batch["done"] == 1
    assert repository.get_customer(customer["id"])["finished_at"] is not None


def test_update_raw_data_online_edit(repository):
    batch_id = make_batch(repository)
    customer = repository.next_pending_customer(batch_id)
    edited = dict(customer["raw_data"])
    edited["姓名"] = "王芳（改）"
    assert repository.update_customer_raw_data(customer["id"], edited) is True
    assert repository.get_customer(customer["id"])["raw_data"]["姓名"] == "王芳（改）"
    assert repository.update_customer_raw_data(99999, edited) is False


def test_batch_status_transitions(repository):
    batch_id = make_batch(repository)
    repository.set_batch_status(batch_id, BATCH_RUNNING)
    assert repository.get_batch(batch_id)["status"] == BATCH_RUNNING
    repository.set_batch_status(batch_id, BATCH_COMPLETED)
    assert repository.get_batch(batch_id)["status"] == BATCH_COMPLETED


def test_call_result_links_customer_id(repository):
    session = repository.create_session("outbound_default", "doubao")
    assert repository.record_call_result(
        session["id"],
        status="已完成",
        result="感兴趣",
        reason="终裁",
        end_reason="正常结束",
        customer_id=7,
    )
    call_result = repository.get_call_result(session["id"])
    assert call_result["customer_id"] == 7


def test_call_result_customer_id_defaults_none(repository):
    session = repository.create_session("outbound_default", "doubao")
    repository.record_call_result(
        session["id"],
        status="已完成",
        result="中立",
        reason="",
        end_reason="正常结束",
    )
    assert repository.get_call_result(session["id"])["customer_id"] is None


def test_migration_adds_customer_id_to_legacy_database(tmp_path):
    """Databases created before batches existed must keep working."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            provider_mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ended_at TEXT,
            rating INTEGER
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            speaker TEXT NOT NULL,
            source_language TEXT NOT NULL,
            target_language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL DEFAULT '',
            source_audio_path TEXT,
            model TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER,
            interrupted INTEGER NOT NULL DEFAULT 0,
            error_code TEXT
        );
        CREATE TABLE call_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            end_reason TEXT NOT NULL DEFAULT '',
            duration_seconds REAL,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    repository = Repository(db_path)
    session = repository.create_session("outbound_default", "doubao")
    assert repository.record_call_result(
        session["id"],
        status="已完成",
        result="不感兴趣",
        reason="沉默",
        end_reason="正常结束",
        customer_id=3,
    )
    assert repository.get_call_result(session["id"])["customer_id"] == 3
    # batches tables exist after migration path ran.
    batch_id = make_batch(repository)
    assert repository.get_batch(batch_id)["total"] == 3


def test_restart_recovery_demotes_running_batch_and_active_customer(repository):
    batch_id = make_batch(repository)
    repository.confirm_batch(batch_id, "手机号")
    repository.set_batch_status(batch_id, BATCH_RUNNING)
    customer = repository.next_pending_customer(batch_id)
    repository.update_customer_status(int(customer["id"]), CUSTOMER_ACTIVE)

    assert repository.reset_running_batches() == 1
    assert repository.get_batch(batch_id)["status"] == "stopped"

    assert repository.reset_active_customers() == 1
    reset = repository.get_customer(int(customer["id"]))
    assert reset["status"] == CUSTOMER_PENDING
    # The demoted customer is dialable again on resume.
    assert repository.next_pending_customer(batch_id)["id"] == customer["id"]
