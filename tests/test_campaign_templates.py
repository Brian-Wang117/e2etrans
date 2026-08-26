"""Tests for the campaign-template feature: storage, REST API, batch
binding, dial payload wiring, protocol validation, and gateway merge."""

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.batch.runner import BatchRunner
from app.config import OutboundSettings, Settings
from app.main import create_app
from app.outbound import call_director
from app.outbound.script_library import Script
from app.realtime.browser_protocol import (
    BrowserProtocolError,
    parse_client_event,
)
from app.realtime.gateway import OUTBOUND_SCENARIO, RealtimeGateway
from app.storage import Repository
from tests.test_gateway import FakeDoubao, FakeWebSocket, envelope, find, wait_until
from tests.test_gateway_outbound import OUTBOUND_SCENARIOS

VALID_SCRIPT = {
    "category": "询价",
    "triggers": ["多少钱", "怎么收费"],
    "reply": "费用会根据您的具体情况评估，稍后由专属顾问为您详细说明。",
    "end_call": False,
    "verdict": "",
    "priority": 7,
    "description": "",
}

CSV_BYTES = "姓名,电话\n王女士,13800000001\n".encode("utf-8")


def good_template_payload(**overrides):
    payload = {
        "name": "甲保险外呼",
        "company_name": "甲保险",
        "business_background": "甲保险主推重疾险，覆盖多种重疾保障。",
        "opening_template": "您好{title}，我是甲保险客服小甲，请问是{name}吗？",
        "bot_name": "小甲",
        "speaking_style": "亲切自然",
        "is_default": False,
        "scripts": [dict(VALID_SCRIPT)],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def template_app(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        outbound=OutboundSettings(enabled=True),
    )
    repository = Repository(settings.database_path)
    app = create_app(settings, repository=repository)
    return {"app": app, "repository": repository, "settings": settings}


# -- storage -----------------------------------------------------------------


def test_template_crud_roundtrip(repository):
    created = repository.create_template(
        name="模板A",
        company_name="公司A",
        business_background="背景A",
        opening_template="您好 {title}",
        bot_name="小A",
        speaking_style="活泼",
        scripts=[VALID_SCRIPT],
    )
    template_id = created["id"]
    assert created["name"] == "模板A"
    assert created["is_default"] is False
    assert created["scripts"] == [
        {
            "category": "询价",
            "triggers": ["多少钱", "怎么收费"],
            "reply": VALID_SCRIPT["reply"],
            "end_call": False,
            "verdict": "",
            "priority": 7,
            "description": "",
        }
    ]

    listed = repository.list_templates()
    assert [t["id"] for t in listed] == [template_id]

    updated = repository.update_template(
        template_id,
        name="模板A改",
        company_name="公司A",
        scripts=[dict(VALID_SCRIPT, priority=9)],
    )
    assert updated["name"] == "模板A改"
    assert updated["business_background"] == ""  # whole-object replacement
    assert updated["scripts"][0]["priority"] == 9

    typed = repository.list_template_scripts(template_id)
    assert len(typed) == 1
    assert isinstance(typed[0], Script)
    assert typed[0].library_name == f"template-{template_id}"

    assert repository.delete_template(template_id) is True
    assert repository.get_template(template_id) is None
    assert repository.delete_template(template_id) is False


def test_duplicate_template_name_rejected(repository):
    repository.create_template(name="重名", company_name="X")
    with pytest.raises(ValueError):
        repository.create_template(name="重名", company_name="Y")


def test_default_template_is_exclusive(repository):
    first = repository.create_template(name="第一", company_name="X", is_default=True)
    second = repository.create_template(name="第二", company_name="Y")
    assert repository.get_default_template()["id"] == first["id"]
    assert repository.set_default_template(second["id"]) is True
    assert repository.get_default_template()["id"] == second["id"]
    defaults = [t for t in repository.list_templates() if t["is_default"]]
    assert [t["id"] for t in defaults] == [second["id"]]
    assert repository.set_default_template(9999) is False


def test_batch_table_migration_adds_template_columns(tmp_path):
    """Databases created before templates existed must upgrade in place."""
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE batches (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            columns TEXT NOT NULL,
            phone_column TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft'
        );
        INSERT INTO batches (id, created_at, total, columns)
            VALUES ('B-old', '2026-01-01T00:00:00+00:00', 1, '[]');
        """
    )
    connection.close()
    repository = Repository(database_path)
    batch = repository.get_batch("B-old")
    assert batch["template_id"] is None
    assert batch["template_name"] == ""
    # Migration is idempotent: re-opening must not raise.
    assert repository.get_batch("B-old")["id"] == "B-old"


# -- REST API ------------------------------------------------------------------


def test_startup_seeds_default_template(template_app):
    templates = template_app["repository"].list_templates()
    assert len(templates) == 1
    assert templates[0]["name"] == "内置默认"
    assert templates[0]["is_default"] is True


def test_create_and_list_template_api(template_app):
    with TestClient(template_app["app"]) as client:
        response = client.post("/api/templates", json=good_template_payload())
        assert response.status_code == 200, response.text
        created = response.json()["template"]
        assert created["id"] >= 2  # seeded default takes id 1
        listed = client.get("/api/templates").json()["templates"]
        assert [t["name"] for t in listed] == ["内置默认", "甲保险外呼"]


def test_create_template_rejects_invalid_scripts(template_app):
    payload = good_template_payload(
        scripts=[
            dict(VALID_SCRIPT, reply="太短"),  # below 20 chars
            dict(VALID_SCRIPT, end_call=True, reply="结束语里没有告别词，只有其他内容而已"),
            dict(VALID_SCRIPT, triggers=["99元", "价格"], reply=VALID_SCRIPT["reply"]),
        ],
        bot_name="这个名字实在太长了超过二十个字符的限制了吧",
    )
    with TestClient(template_app["app"]) as client:
        response = client.post("/api/templates", json=payload)
        assert response.status_code == 400
        errors = response.json()["detail"]["errors"]
        assert any("AI 名字" in error for error in errors)
        # validate_script reports in English; the API prefixes each problem
        # with the offending item number.
        assert any("第 1 条" in error and "reply length" in error for error in errors)
        assert any("第 2 条" in error and "farewell" in error for error in errors)
        assert any("第 3 条" in error and "price" in error for error in errors)


def test_create_template_requires_name_and_company(template_app):
    payload = good_template_payload(name="  ", company_name="")
    with TestClient(template_app["app"]) as client:
        response = client.post("/api/templates", json=payload)
        assert response.status_code == 400
        errors = response.json()["detail"]["errors"]
        assert "模板名不能为空" in errors
        assert "公司名称不能为空" in errors


def test_duplicate_template_name_conflict(template_app):
    with TestClient(template_app["app"]) as client:
        assert client.post("/api/templates", json=good_template_payload()).status_code == 200
        response = client.post("/api/templates", json=good_template_payload())
        assert response.status_code == 409


def test_update_and_delete_template_api(template_app):
    with TestClient(template_app["app"]) as client:
        created = client.post("/api/templates", json=good_template_payload()).json()["template"]
        response = client.put(
            f"/api/templates/{created['id']}",
            json=good_template_payload(name="改过名", scripts=[]),
        )
        assert response.status_code == 200
        assert response.json()["template"]["name"] == "改过名"
        assert response.json()["template"]["scripts"] == []
        assert client.delete(f"/api/templates/{created['id']}").status_code == 200
        assert client.delete(f"/api/templates/{created['id']}").status_code == 404
        assert client.put("/api/templates/9999", json=good_template_payload()).status_code == 404


def test_set_default_template_api(template_app):
    with TestClient(template_app["app"]) as client:
        created = client.post("/api/templates", json=good_template_payload()).json()["template"]
        response = client.post(f"/api/templates/{created['id']}/default")
        assert response.status_code == 200
        templates = client.get("/api/templates").json()["templates"]
        defaults = [t for t in templates if t["is_default"]]
        assert [t["id"] for t in defaults] == [created["id"]]
        assert client.post("/api/templates/9999/default").status_code == 404


def test_templates_page_served(template_app):
    with TestClient(template_app["app"]) as client:
        response = client.get("/templates")
        assert response.status_code == 200
        assert "外呼模板管理" in response.text
        assert client.get("/static/templates.js").status_code == 200


# -- batch binding + dial wiring ------------------------------------------------


def confirm_with(client, batch_id, **payload):
    response = client.post(f"/api/batches/{batch_id}/confirm", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["batch"]


def test_confirm_binds_explicit_template_and_snapshots_name(template_app):
    with TestClient(template_app["app"]) as client:
        created = client.post("/api/templates", json=good_template_payload()).json()["template"]
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch = confirm_with(client, response.json()["batch_id"], template_id=created["id"])
        assert batch["template_id"] == created["id"]
        assert batch["template_name"] == "甲保险外呼"


def test_confirm_without_template_uses_default(template_app):
    with TestClient(template_app["app"]) as client:
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch = confirm_with(client, response.json()["batch_id"])
        seeded = template_app["repository"].get_default_template()
        assert batch["template_id"] == seeded["id"]
        assert batch["template_name"] == "内置默认"


def test_confirm_rejects_unknown_template(template_app):
    with TestClient(template_app["app"]) as client:
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch_id = response.json()["batch_id"]
        response = client.post(
            f"/api/batches/{batch_id}/confirm", json={"template_id": 9999}
        )
        assert response.status_code == 409


@pytest.fixture
def wiring(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        outbound=OutboundSettings(enabled=True),
    )
    repository = Repository(settings.database_path)
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
    app = create_app(settings, repository=repository, batch_runner=runner)
    return {"app": app, "repository": repository, "runner": runner, "bus": bus}


def receive_until(websocket, message_type: str, limit: int = 100) -> dict:
    for _ in range(limit):
        message = websocket.receive_json()
        if message.get("type") == message_type:
            return message
    raise AssertionError(f"never received {message_type!r}")


def test_dial_payload_carries_template_persona(wiring):
    """Template fields travel runner -> bridge.dial, the opening placeholders
    get filled per customer, and the personalizer is skipped entirely."""
    with TestClient(wiring["app"]) as client:
        created = client.post("/api/templates", json=good_template_payload()).json()["template"]
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch_id = confirm_with(
            client, response.json()["batch_id"], template_id=created["id"]
        )["id"]
        with client.websocket_connect("/ws/workbench") as bridge:
            bridge.receive_json()
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)
            client.post(f"/api/batches/{batch_id}/start")
            dial = receive_until(bridge, "bridge.dial")
            assert dial["bot_name"] == "小甲"
            assert dial["speaking_style"] == "亲切自然"
            assert dial["template_id"] == created["id"]
            assert dial["business_background"] == "甲保险 甲保险主推重疾险，覆盖多种重疾保障。"
            # {title} resolved from 王女士 -> 女士, {name} -> 王女士
            assert dial["opening_text"] == "您好女士，我是甲保险客服小甲，请问是王女士吗？"
            bridge.send_json({"type": "bridge.call_failed", "reason": "无人接听"})
            receive_until(bridge, "batch.state")


def test_dial_without_template_keeps_legacy_payload(wiring):
    """A batch confirmed before any template existed (template_id NULL) must
    dial exactly like the old .env chain."""
    repository = wiring["repository"]
    # Remove the seeded default to simulate a database with no templates.
    for template in repository.list_templates():
        repository.delete_template(template["id"])
    with TestClient(wiring["app"]) as client:
        response = client.post(
            "/api/batches/import", files={"file": ("c.csv", CSV_BYTES, "text/csv")}
        )
        batch_id = confirm_with(client, response.json()["batch_id"])["id"]
        assert repository.get_batch(batch_id)["template_id"] is None
        with client.websocket_connect("/ws/workbench") as bridge:
            bridge.receive_json()
            bridge.send_json({"type": "workbench.hello", "role": "bridge"})
            time.sleep(0.2)
            client.post(f"/api/batches/{batch_id}/start")
            dial = receive_until(bridge, "bridge.dial")
            assert dial["bot_name"] == ""
            assert dial["speaking_style"] == ""
            assert dial["template_id"] is None
            # Legacy chain: no personalizer configured, so the opening stays
            # empty and the gateway falls back to the default opening.
            assert dial["opening_text"] == ""
            bridge.send_json({"type": "bridge.call_failed", "reason": "无人接听"})
            receive_until(bridge, "batch.state")


# -- protocol validation ---------------------------------------------------------

PROTOCOL_LIMITS = dict(max_message_bytes=131_072, max_audio_bytes=32_000)
OUTBOUND_SCENARIO_SET = frozenset({OUTBOUND_SCENARIO})


def parse_start(payload):
    document = json.dumps(
        {
            "v": 1,
            "type": "session.start",
            "event_id": "evt-1",
            "session_id": None,
            "turn_id": None,
            "seq": 1,
            "ts_ms": 1_700_000_000_000,
            "payload": payload,
        }
    )
    return parse_client_event(
        document, allowed_scenarios=OUTBOUND_SCENARIO_SET, **PROTOCOL_LIMITS
    )


def test_protocol_accepts_template_fields():
    event = parse_start(
        {
            "scenario_id": OUTBOUND_SCENARIO,
            "bot_name": "小甲",
            "speaking_style": "亲切自然",
            "template_id": 3,
        }
    )
    assert event.payload["template_id"] == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario_id": OUTBOUND_SCENARIO, "bot_name": "超" * 21},
        {"scenario_id": OUTBOUND_SCENARIO, "speaking_style": "长" * 201},
        {"scenario_id": OUTBOUND_SCENARIO, "template_id": 0},
        {"scenario_id": OUTBOUND_SCENARIO, "template_id": "3"},
        {"scenario_id": OUTBOUND_SCENARIO, "template_id": True},
    ],
)
def test_protocol_rejects_bad_template_fields(payload):
    with pytest.raises(BrowserProtocolError):
        parse_start(payload)


# -- gateway: persona override + template script merge -----------------------------


@pytest.fixture(autouse=True)
def fast_speech_estimates(monkeypatch):
    """Shrink TTS playback estimates so script-hit hang-ups happen quickly."""
    monkeypatch.setattr(call_director, "SPEECH_SECONDS_PER_CHAR", 0.001)
    monkeypatch.setattr(call_director, "SPEECH_BUFFER_SECONDS", 0.05)
    monkeypatch.setattr(call_director, "GOODBYE_MARGIN_SECONDS", 0.05)


def make_gateway(settings, repository, doubao, seen):
    def factory(session_id, input_mode, persona=None, speaker=None):
        seen["persona"] = persona
        return doubao

    return RealtimeGateway(
        settings=settings,
        repository=repository,
        doubao_factory=factory,
        translator=None,
        scenarios=OUTBOUND_SCENARIOS,
        outbound_settings=OutboundSettings(enabled=True),
        adjudicator=None,
    )


def start_with_template(template_id, bot_name=None, speaking_style=None):
    payload = {"scenario_id": OUTBOUND_SCENARIO, "template_id": template_id}
    if bot_name is not None:
        payload["bot_name"] = bot_name
    if speaking_style is not None:
        payload["speaking_style"] = speaking_style
    return envelope("session.start", 1, payload)


async def test_gateway_persona_override_from_payload(repository, settings):
    repository.seed_builtin_scripts()
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_gateway(settings, repository, doubao, seen)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_with_template(1, bot_name="模板小甲", speaking_style="活泼"))
        await wait_until(lambda: doubao.connected)
        assert seen["persona"].bot_name == "模板小甲"
        assert seen["persona"].speaking_style == "活泼"
        websocket.feed(envelope("session.end", 2, {}))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()


async def test_gateway_merges_template_scripts_with_builtins(repository, settings):
    """Template scripts join the matcher alongside the built-ins; a unique
    trigger proves the template library is actually wired in."""
    repository.seed_builtin_scripts()
    template = repository.create_template(
        name="合并测试",
        company_name="X",
        scripts=[
            {
                "category": "已办理查询",
                "triggers": ["已经办过了"],
                "reply": "好的，那就不重复打扰您了，祝您生活愉快，再见。",
                "end_call": True,
                "verdict": "",
                "priority": 8,
                "description": "",
            }
        ],
    )
    websocket = FakeWebSocket()
    doubao = FakeDoubao("pending")
    seen = {}
    gateway = make_gateway(settings, repository, doubao, seen)
    task = asyncio.create_task(gateway.run(websocket))
    try:
        websocket.feed(start_with_template(template["id"]))
        await wait_until(lambda: doubao.connected)
        await wait_until(lambda: find(websocket.sent, "session.ready"))

        # Template script hit: fixed reply + scheduled hang-up.
        doubao.emit(
            451, {"results": [{"text": "这个活动我已经办过了", "is_interim": False}]}
        )
        await wait_until(lambda: doubao.interrupts >= 1)
        assert doubao.greetings[-1] == "好的，那就不重复打扰您了，祝您生活愉快，再见。"
        await wait_until(lambda: find(websocket.sent, "script.hit"))
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
