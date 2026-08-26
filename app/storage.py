"""SQLite session/turn repository and bounded WAV audio storage."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.outbound.script_library import BUILTIN_SCRIPTS, Script

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

BUILTIN_LIBRARY_NAME = "builtin"
CALL_RESULT_COMPLETED = "已完成"
CALL_RESULT_FAILED = "失败"
CALL_RESULT_STATUSES = frozenset({CALL_RESULT_COMPLETED, CALL_RESULT_FAILED})

BATCH_DRAFT = "draft"
BATCH_READY = "ready"
BATCH_RUNNING = "running"
BATCH_STOPPED = "stopped"
BATCH_COMPLETED = "completed"
BATCH_STATUSES = frozenset(
    {BATCH_DRAFT, BATCH_READY, BATCH_RUNNING, BATCH_STOPPED, BATCH_COMPLETED}
)

CUSTOMER_PENDING = "待呼叫"
CUSTOMER_ACTIVE = "进行中"
CUSTOMER_COMPLETED = "已完成"
CUSTOMER_FAILED = "失败"
CUSTOMER_STATUSES = frozenset(
    {CUSTOMER_PENDING, CUSTOMER_ACTIVE, CUSTOMER_COMPLETED, CUSTOMER_FAILED}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._connect().close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                provider_mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                rating INTEGER
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
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
            CREATE TABLE IF NOT EXISTS scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_name TEXT NOT NULL,
                category TEXT NOT NULL,
                triggers TEXT NOT NULL,
                reply TEXT NOT NULL,
                end_call INTEGER NOT NULL DEFAULT 0,
                verdict TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE
                    REFERENCES sessions(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                end_reason TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                columns TEXT NOT NULL,
                phone_column TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft'
            );
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                row_number INTEGER NOT NULL,
                raw_data TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '待呼叫',
                result TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                session_id TEXT,
                finished_at TEXT,
                UNIQUE (batch_id, row_number)
            );
            CREATE INDEX IF NOT EXISTS idx_customers_batch_status
                ON customers (batch_id, status);
            CREATE TABLE IF NOT EXISTS campaign_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                company_name TEXT NOT NULL DEFAULT '',
                business_background TEXT NOT NULL DEFAULT '',
                opening_template TEXT NOT NULL DEFAULT '',
                bot_name TEXT NOT NULL DEFAULT '',
                speaking_style TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS template_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL
                    REFERENCES campaign_templates(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                triggers TEXT NOT NULL,
                reply TEXT NOT NULL,
                end_call INTEGER NOT NULL DEFAULT 0,
                verdict TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 5,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_template_scripts_template
                ON template_scripts (template_id);
            """
        )
        # Migration for databases created before the outbound engine existed.
        turn_columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
        if "source" not in turn_columns:
            connection.execute(
                "ALTER TABLE turns ADD COLUMN source TEXT NOT NULL DEFAULT ''"
            )
        result_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(call_results)")
        }
        if "customer_id" not in result_columns:
            connection.execute("ALTER TABLE call_results ADD COLUMN customer_id INTEGER")
        # Campaign-template support: snapshot which template a batch dialed with.
        batch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        if "template_id" not in batch_columns:
            connection.execute("ALTER TABLE batches ADD COLUMN template_id INTEGER")
        if "template_name" not in batch_columns:
            connection.execute(
                "ALTER TABLE batches ADD COLUMN template_name TEXT NOT NULL DEFAULT ''"
            )

    def create_session(self, scenario_id: str, provider_mode: str) -> dict[str, object]:
        session_id = uuid.uuid4().hex
        created_at = _utc_now()
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "INSERT INTO sessions (id, scenario_id, provider_mode, created_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, scenario_id, provider_mode, created_at),
            )
        return {"id": session_id, "scenario_id": scenario_id, "created_at": created_at}

    def end_session(self, session_id: str) -> None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (_utc_now(), session_id),
            )

    def set_rating(self, session_id: str, rating: int) -> None:
        if not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("rating must be an integer in [1, 5]")
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE sessions SET rating = ? WHERE id = ?", (rating, session_id)
            )

    def add_turn(
        self,
        session_id: str,
        *,
        speaker: str,
        source_language: str,
        target_language: str,
        source_text: str,
        translated_text: str = "",
        source_audio_path: str | None = None,
        model: str = "",
        latency_ms: int | None = None,
        interrupted: bool = False,
        source: str = "",
    ) -> int:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["count"]) + 1
            cursor = connection.execute(
                "INSERT INTO turns (session_id, seq, speaker, source_language,"
                " target_language, source_text, translated_text, source_audio_path,"
                " model, latency_ms, interrupted, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    speaker,
                    source_language,
                    target_language,
                    source_text,
                    translated_text,
                    source_audio_path,
                    model,
                    latency_ms,
                    1 if interrupted else 0,
                    source,
                ),
            )
            return int(cursor.lastrowid)

    def mark_turn_error(self, turn_id: int, error_code: str) -> None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE turns SET error_code = ? WHERE id = ?", (error_code, turn_id)
            )

    def _turn_dict(self, row: sqlite3.Row, session_id: str) -> dict[str, object]:
        return {
            "id": row["id"],
            "seq": row["seq"],
            "speaker": row["speaker"],
            "source_language": row["source_language"],
            "target_language": row["target_language"],
            "source_text": row["source_text"],
            "translated_text": row["translated_text"],
            "model": row["model"],
            "latency_ms": row["latency_ms"],
            "interrupted": bool(row["interrupted"]),
            "error_code": row["error_code"],
        }

    def get_session(self, session_id: str) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            turns = connection.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "scenario_id": row["scenario_id"],
            "provider_mode": row["provider_mode"],
            "created_at": row["created_at"],
            "ended_at": row["ended_at"],
            "rating": row["rating"],
            "turns": [self._turn_dict(turn, session_id) for turn in turns],
        }

    def list_sessions(self) -> list[dict[str, object]]:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            rows = connection.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id)"
                " AS turn_count FROM sessions s ORDER BY s.created_at DESC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "scenario_id": row["scenario_id"],
                "provider_mode": row["provider_mode"],
                "created_at": row["created_at"],
                "ended_at": row["ended_at"],
                "rating": row["rating"],
                "turn_count": row["turn_count"],
            }
            for row in rows
        ]

    def delete_session(self, session_id: str) -> bool:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount > 0

    # -- outbound script library ------------------------------------------------

    def seed_builtin_scripts(self, library_name: str = BUILTIN_LIBRARY_NAME) -> int:
        """Insert the built-in scripts once; returns how many were added."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM scripts WHERE library_name = ?",
                (library_name,),
            ).fetchone()["count"]
            if existing:
                return 0
            created_at = _utc_now()
            for script in BUILTIN_SCRIPTS:
                connection.execute(
                    "INSERT INTO scripts (library_name, category, triggers, reply,"
                    " end_call, verdict, priority, description, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        library_name,
                        script.category,
                        json.dumps(list(script.triggers), ensure_ascii=False),
                        script.reply,
                        1 if script.end_call else 0,
                        script.verdict,
                        script.priority,
                        script.description,
                        created_at,
                    ),
                )
            return len(BUILTIN_SCRIPTS)

    def list_scripts(self, library_name: str) -> list[Script]:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            rows = connection.execute(
                "SELECT * FROM scripts WHERE library_name = ?"
                " ORDER BY priority DESC, id ASC",
                (library_name,),
            ).fetchall()
        scripts: list[Script] = []
        for row in rows:
            try:
                triggers = tuple(json.loads(row["triggers"]))
            except (TypeError, ValueError):
                triggers = ()
            scripts.append(
                Script(
                    category=row["category"],
                    triggers=triggers,
                    reply=row["reply"],
                    end_call=bool(row["end_call"]),
                    verdict=row["verdict"],
                    priority=int(row["priority"]),
                    description=row["description"],
                    id=int(row["id"]),
                    library_name=row["library_name"],
                )
            )
        return scripts

    # -- outbound call results ----------------------------------------------------

    def record_call_result(
        self,
        session_id: str,
        *,
        status: str,
        result: str,
        reason: str,
        end_reason: str,
        duration_seconds: float | None = None,
        customer_id: int | None = None,
    ) -> bool:
        """Store the call outcome. Returns False when the call already has a
        result (each call is reported exactly once, requirement 六)."""
        if status not in CALL_RESULT_STATUSES:
            raise ValueError("status must be 已完成 or 失败")
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO call_results"
                " (session_id, status, result, reason, end_reason,"
                " duration_seconds, created_at, customer_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    status,
                    result,
                    reason,
                    end_reason,
                    duration_seconds,
                    _utc_now(),
                    customer_id,
                ),
            )
            return cursor.rowcount > 0

    def get_call_result(self, session_id: str) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM call_results WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "status": row["status"],
            "result": row["result"],
            "reason": row["reason"],
            "end_reason": row["end_reason"],
            "duration_seconds": row["duration_seconds"],
            "created_at": row["created_at"],
            "customer_id": row["customer_id"],
        }

    # -- batch outbound: batches & customers -------------------------------------

    def create_batch(
        self,
        batch_id: str,
        *,
        columns: list[str],
        total: int,
        phone_column: str = "",
    ) -> dict[str, object]:
        created_at = _utc_now()
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "INSERT INTO batches (id, created_at, total, columns,"
                " phone_column, status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    created_at,
                    total,
                    json.dumps(list(columns), ensure_ascii=False),
                    phone_column,
                    BATCH_DRAFT,
                ),
            )
        return {"id": batch_id, "created_at": created_at, "total": total}

    def add_customers(
        self,
        batch_id: str,
        rows: list[dict[str, object]],
    ) -> int:
        """Bulk-insert customers; each row carries row_number, raw_data, phone."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.executemany(
                "INSERT INTO customers (batch_id, row_number, raw_data, phone)"
                " VALUES (?, ?, ?, ?)",
                [
                    (
                        batch_id,
                        row["row_number"],
                        json.dumps(row["raw_data"], ensure_ascii=False),
                        row.get("phone", ""),
                    )
                    for row in rows
                ],
            )
            return len(rows)

    @staticmethod
    def _batch_dict(row: sqlite3.Row) -> dict[str, object]:
        try:
            columns = json.loads(row["columns"])
        except (TypeError, ValueError):
            columns = []
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "total": row["total"],
            "done": row["done"],
            "columns": columns,
            "phone_column": row["phone_column"],
            "status": row["status"],
            "template_id": row["template_id"],
            "template_name": row["template_name"],
        }

    def get_batch(self, batch_id: str) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        return self._batch_dict(row)

    def get_latest_batch(self) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self._batch_dict(row)

    def confirm_batch(
        self,
        batch_id: str,
        phone_column: str,
        *,
        template_id: int | None = None,
        template_name: str = "",
    ) -> bool:
        """Operator confirmed the preview (and possibly re-selected the phone
        column / outbound template); the batch becomes ready for scheduling."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE batches SET status = ?, phone_column = ?,"
                " template_id = ?, template_name = ?"
                " WHERE id = ? AND status = ?",
                (
                    BATCH_READY,
                    phone_column,
                    template_id,
                    template_name,
                    batch_id,
                    BATCH_DRAFT,
                ),
            )
            return cursor.rowcount > 0

    def set_batch_status(self, batch_id: str, status: str) -> None:
        if status not in BATCH_STATUSES:
            raise ValueError("unknown batch status")
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE batches SET status = ? WHERE id = ?", (status, batch_id)
            )

    def reset_running_batches(self) -> int:
        """Server restart recovery: a batch marked running cannot still be
        executing (the runner lives in this process), so demote it to
        stopped. Returns how many batches were affected."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE batches SET status = ? WHERE status = ?",
                (BATCH_STOPPED, BATCH_RUNNING),
            )
            return cursor.rowcount

    def reset_active_customers(self) -> int:
        """Server restart recovery: customers stuck 进行中 were mid-dial when
        the process died; demote them to 待呼叫 so a resumed batch redials
        them instead of skipping them forever."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE customers SET status = ?, reason = '' WHERE status = ?",
                (CUSTOMER_PENDING, CUSTOMER_ACTIVE),
            )
            return cursor.rowcount

    def increment_batch_done(self, batch_id: str) -> None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE batches SET done = done + 1 WHERE id = ?", (batch_id,)
            )

    @staticmethod
    def _customer_dict(row: sqlite3.Row) -> dict[str, object]:
        try:
            raw_data = json.loads(row["raw_data"])
        except (TypeError, ValueError):
            raw_data = {}
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "row_number": row["row_number"],
            "raw_data": raw_data,
            "phone": row["phone"],
            "status": row["status"],
            "result": row["result"],
            "reason": row["reason"],
            "duration_seconds": row["duration_seconds"],
            "session_id": row["session_id"],
            "finished_at": row["finished_at"],
        }

    def get_customer(self, customer_id: int) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
        if row is None:
            return None
        return self._customer_dict(row)

    def list_customers(
        self,
        batch_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        query = "SELECT * FROM customers WHERE batch_id = ? ORDER BY row_number"
        params: list[object] = [batch_id]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            rows = connection.execute(query, params).fetchall()
        return [self._customer_dict(row) for row in rows]

    def next_pending_customer(self, batch_id: str) -> dict[str, object] | None:
        """The next customer to dial: lowest row_number still 待呼叫."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM customers WHERE batch_id = ? AND status = ?"
                " ORDER BY row_number LIMIT 1",
                (batch_id, CUSTOMER_PENDING),
            ).fetchone()
        if row is None:
            return None
        return self._customer_dict(row)

    def update_customer_status(
        self,
        customer_id: int,
        status: str,
        *,
        result: str = "",
        reason: str = "",
        duration_seconds: float | None = None,
        session_id: str | None = None,
    ) -> None:
        if status not in CUSTOMER_STATUSES:
            raise ValueError("unknown customer status")
        finished_at = (
            _utc_now() if status in (CUSTOMER_COMPLETED, CUSTOMER_FAILED) else None
        )
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            connection.execute(
                "UPDATE customers SET status = ?, result = ?, reason = ?,"
                " duration_seconds = ?, session_id = COALESCE(?, session_id),"
                " finished_at = COALESCE(?, finished_at) WHERE id = ?",
                (
                    status,
                    result,
                    reason,
                    duration_seconds,
                    session_id,
                    finished_at,
                    customer_id,
                ),
            )

    def update_customer_raw_data(
        self, customer_id: int, raw_data: dict[str, object]
    ) -> bool:
        """Online edit of a customer's original fields (requirement 2.2)."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE customers SET raw_data = ? WHERE id = ?",
                (json.dumps(raw_data, ensure_ascii=False), customer_id),
            )
            return cursor.rowcount > 0

    def update_customer_phone(self, customer_id: int, phone: str) -> bool:
        """Re-extract the dial number after the phone column changes."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE customers SET phone = ? WHERE id = ?",
                (phone, customer_id),
            )
            return cursor.rowcount > 0

    # -- campaign templates -------------------------------------------------------

    @staticmethod
    def _template_dict(
        row: sqlite3.Row, scripts: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "id": row["id"],
            "name": row["name"],
            "company_name": row["company_name"],
            "business_background": row["business_background"],
            "opening_template": row["opening_template"],
            "bot_name": row["bot_name"],
            "speaking_style": row["speaking_style"],
            "is_default": bool(row["is_default"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "scripts": scripts,
        }

    @staticmethod
    def _template_script_dict(row: sqlite3.Row) -> dict[str, object]:
        try:
            triggers = list(json.loads(row["triggers"]))
        except (TypeError, ValueError):
            triggers = []
        return {
            "category": row["category"],
            "triggers": triggers,
            "reply": row["reply"],
            "end_call": bool(row["end_call"]),
            "verdict": row["verdict"],
            "priority": row["priority"],
            "description": row["description"],
        }

    def _fetch_template_scripts(
        self, connection: sqlite3.Connection, template_id: int
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            "SELECT * FROM template_scripts WHERE template_id = ?"
            " ORDER BY priority DESC, id ASC",
            (template_id,),
        ).fetchall()
        return [self._template_script_dict(row) for row in rows]

    def list_templates(self) -> list[dict[str, object]]:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            rows = connection.execute(
                "SELECT * FROM campaign_templates ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [
                self._template_dict(row, self._fetch_template_scripts(connection, row["id"]))
                for row in rows
            ]

    def get_template(self, template_id: int) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM campaign_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if row is None:
                return None
            return self._template_dict(
                row, self._fetch_template_scripts(connection, template_id)
            )

    def get_default_template(self) -> dict[str, object] | None:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT * FROM campaign_templates WHERE is_default = 1"
                " ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return self._template_dict(
                row, self._fetch_template_scripts(connection, row["id"])
            )

    def count_templates(self) -> int:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM campaign_templates"
            ).fetchone()
            return int(row["count"])

    def _insert_template_scripts(
        self,
        connection: sqlite3.Connection,
        template_id: int,
        scripts: Sequence[Mapping[str, object]],
    ) -> None:
        for script in scripts:
            triggers = [
                str(trigger).strip()
                for trigger in script.get("triggers", [])
                if str(trigger or "").strip()
            ]
            connection.execute(
                "INSERT INTO template_scripts (template_id, category, triggers,"
                " reply, end_call, verdict, priority, description)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    template_id,
                    str(script.get("category", "")),
                    json.dumps(triggers, ensure_ascii=False),
                    str(script.get("reply", "")),
                    1 if script.get("end_call") else 0,
                    str(script.get("verdict", "") or ""),
                    int(script.get("priority", 5)),
                    str(script.get("description", "") or ""),
                ),
            )

    def create_template(
        self,
        *,
        name: str,
        company_name: str = "",
        business_background: str = "",
        opening_template: str = "",
        bot_name: str = "",
        speaking_style: str = "",
        is_default: bool = False,
        scripts: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        created_at = _utc_now()
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            if is_default:
                connection.execute("UPDATE campaign_templates SET is_default = 0")
            try:
                cursor = connection.execute(
                    "INSERT INTO campaign_templates (name, company_name,"
                    " business_background, opening_template, bot_name,"
                    " speaking_style, is_default, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        company_name,
                        business_background,
                        opening_template,
                        bot_name,
                        speaking_style,
                        1 if is_default else 0,
                        created_at,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("模板名已存在") from error
            template_id = int(cursor.lastrowid)
            self._insert_template_scripts(connection, template_id, scripts)
        return self.get_template(template_id) or {}

    def update_template(
        self,
        template_id: int,
        *,
        name: str,
        company_name: str = "",
        business_background: str = "",
        opening_template: str = "",
        bot_name: str = "",
        speaking_style: str = "",
        scripts: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object] | None:
        """Whole-object replacement: fields plus the script list."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            try:
                cursor = connection.execute(
                    "UPDATE campaign_templates SET name = ?, company_name = ?,"
                    " business_background = ?, opening_template = ?,"
                    " bot_name = ?, speaking_style = ?, updated_at = ?"
                    " WHERE id = ?",
                    (
                        name,
                        company_name,
                        business_background,
                        opening_template,
                        bot_name,
                        speaking_style,
                        _utc_now(),
                        template_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("模板名已存在") from error
            if cursor.rowcount == 0:
                return None
            connection.execute(
                "DELETE FROM template_scripts WHERE template_id = ?",
                (template_id,),
            )
            self._insert_template_scripts(connection, template_id, scripts)
        return self.get_template(template_id)

    def delete_template(self, template_id: int) -> bool:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "DELETE FROM campaign_templates WHERE id = ?", (template_id,)
            )
            return cursor.rowcount > 0

    def set_default_template(self, template_id: int) -> bool:
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            cursor = connection.execute(
                "UPDATE campaign_templates SET is_default = 1"
                " WHERE id = ?",
                (template_id,),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "UPDATE campaign_templates SET is_default = 0 WHERE id != ?",
                (template_id,),
            )
            return True

    def list_template_scripts(self, template_id: int) -> list[Script]:
        """Typed script objects for the call director matcher."""
        with self._guard, self._connect() as connection:
            self._init_schema(connection)
            rows = connection.execute(
                "SELECT * FROM template_scripts WHERE template_id = ?"
                " ORDER BY priority DESC, id ASC",
                (template_id,),
            ).fetchall()
        scripts: list[Script] = []
        for row in rows:
            try:
                triggers = tuple(json.loads(row["triggers"]))
            except (TypeError, ValueError):
                triggers = ()
            scripts.append(
                Script(
                    category=row["category"],
                    triggers=triggers,
                    reply=row["reply"],
                    end_call=bool(row["end_call"]),
                    verdict=row["verdict"],
                    priority=row["priority"],
                    description=row["description"],
                    id=row["id"],
                    library_name=f"template-{template_id}",
                )
            )
        return scripts
