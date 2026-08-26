"""Tests for the outbound storage extensions (scripts, call_results, turns.source)."""

import sqlite3

import pytest

from app.outbound.script_library import BUILTIN_SCRIPTS
from app.storage import Repository


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "data.sqlite3")


def test_builtin_scripts_are_seeded_once(repo):
    assert repo.seed_builtin_scripts() == len(BUILTIN_SCRIPTS)
    assert repo.seed_builtin_scripts() == 0
    assert len(repo.list_scripts("builtin")) == len(BUILTIN_SCRIPTS)


def test_script_roundtrip_preserves_fields(repo):
    repo.seed_builtin_scripts()
    scripts = repo.list_scripts("builtin")
    by_category = {script.category: script for script in scripts}
    script = by_category["投诉免打扰"]
    assert script.triggers == ("别再打", "骚扰", "拉黑", "投诉")
    assert script.end_call is True
    assert script.verdict == "不感兴趣"
    assert script.priority == 10
    assert script.library_name == "builtin"
    assert script.id is not None


def test_scripts_are_ordered_by_priority_then_insertion(repo):
    repo.seed_builtin_scripts()
    scripts = repo.list_scripts("builtin")
    priorities = [script.priority for script in scripts]
    assert priorities == sorted(priorities, reverse=True)


def test_add_turn_stores_source(repo):
    session = repo.create_session("outbound_default", "doubao")
    repo.add_turn(
        session["id"],
        speaker="agent",
        source_language="zh",
        target_language="zh",
        source_text="您好，打扰一分钟。",
        source="script_reply",
    )
    stored = repo.get_session(session["id"])
    assert stored["turns"][0]["source_text"] == "您好，打扰一分钟。"


def test_call_result_is_reported_exactly_once(repo):
    session = repo.create_session("outbound_default", "doubao")
    first = repo.record_call_result(
        session["id"],
        status="已完成",
        result="感兴趣",
        reason="客户询问参与方式",
        end_reason="正常结束",
        duration_seconds=42.5,
    )
    second = repo.record_call_result(
        session["id"],
        status="失败",
        result="中立",
        reason="",
        end_reason="重复上报",
    )
    assert first is True
    assert second is False
    result = repo.get_call_result(session["id"])
    assert result["status"] == "已完成"
    assert result["result"] == "感兴趣"
    assert result["end_reason"] == "正常结束"
    assert result["duration_seconds"] == 42.5


def test_call_result_status_enum_is_enforced(repo):
    session = repo.create_session("outbound_default", "doubao")
    with pytest.raises(ValueError):
        repo.record_call_result(
            session["id"],
            status="进行中",
            result="中立",
            reason="",
            end_reason="",
        )


def test_legacy_turns_table_gets_source_column_migrated(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
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
        """
    )
    connection.commit()
    connection.close()
    repo = Repository(database)
    session = repo.create_session("product_intro", "doubao")
    repo.add_turn(
        session["id"],
        speaker="tester",
        source_language="zh",
        target_language="en",
        source_text="hello",
        source="asr",
    )
    assert len(repo.get_session(session["id"])["turns"]) == 1
