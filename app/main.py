"""FastAPI application: REST review endpoints, static UI, realtime WebSocket."""

from __future__ import annotations

import csv
import io
import logging
import mimetypes
from contextlib import asynccontextmanager

import httpx
from fastapi import Body, FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth import install_auth
from app.batch.events import CallEventBus
from app.batch.hub import WorkbenchHub
from app.batch.import_parser import (
    ImportParseError,
    detect_phone_column,
    new_batch_id,
    normalize_phone,
    parse_table,
)
from app.batch.personalizer import CACHE_KEY, Personalizer
from app.batch.runner import BatchRunner, BatchRunnerError
from app.config import BUNDLE_ROOT, Settings, settings_from_env
from app.outbound.adjudicator import OutboundAdjudicator
from app.outbound.persona import (
    DEFAULT_OPENING_TEXT,
    FALLBACK_BUSINESS_BACKGROUND,
    MAX_BOT_NAME_CHARS,
)
from app.outbound.script_library import Script, validate_script
from app.realtime.browser_protocol import server_event
from app.realtime.doubao import DoubaoRealtimeClient
from app.realtime.gateway import OUTBOUND_SCENARIO, RealtimeGateway
from app.realtime.qwen import QwenSubtitleTranslator
from app.storage import Repository
from app.voice_clone import CLONE_SAMPLE_SECONDS, CLONE_TEXT, VoiceCloneError, VoiceCloneRelay

logger = logging.getLogger(__name__)

# Windows machines may lack registry-based MIME mappings; module scripts are
# rejected by browsers unless served with the correct Content-Type.
for extension, media_type in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".html", "text/html"),
    (".json", "application/json"),
    (".wasm", "application/wasm"),
    (".wav", "audio/wav"),
):
    mimetypes.add_type(media_type, extension)

SCENARIOS: dict[str, str] = {
    "product_intro": (
        "Hello! This is the English customer care team calling about your "
        "recent order. Do you have a minute to talk?"
    ),
    "delivery_confirm": (
        "Hello, this is English customer care. I am calling to confirm the "
        "delivery address for your package. Could you help me verify it?"
    ),
    "satisfaction_survey": (
        "Hello! This is English customer care with a very short satisfaction "
        "survey. May I ask you three quick questions?"
    ),
    # Chinese outbound-marketing call; the real greeting is resolved per
    # session (opening_text payload / OUTBOUND_OPENING_TEXT / default).
    OUTBOUND_SCENARIO: DEFAULT_OPENING_TEXT,
}


class _AdmissionCounter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    def try_acquire(self) -> bool:
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active = max(0, self._active - 1)


def create_app(
    settings: Settings,
    *,
    repository: Repository | None = None,
    realtime_gateway_factory=None,
    batch_runner: BatchRunner | None = None,
) -> FastAPI:
    repository = repository or Repository(settings.database_path)

    translator = None
    if settings.realtime.enabled and settings.realtime.subtitle_enabled:
        translator = QwenSubtitleTranslator(
            api_key=settings.realtime.dashscope_api_key or "",
            model=settings.realtime.subtitle_model,
            base_url=settings.realtime.dashscope_base_url,
            timeout_seconds=settings.realtime.subtitle_timeout_seconds,
        )

    adjudicator = None
    if (
        settings.realtime.enabled
        and settings.outbound.enabled
        and settings.realtime.dashscope_api_key
    ):
        adjudicator = OutboundAdjudicator(
            api_key=settings.realtime.dashscope_api_key,
            model=settings.realtime.subtitle_model,
            base_url=settings.realtime.dashscope_base_url,
            timeout_seconds=settings.realtime.subtitle_timeout_seconds,
        )

    # Voice-clone relay: credentials stay server-side, the browser never
    # carries them. Only usable when Doubao credentials are configured.
    clone_relay = None
    if settings.realtime.app_id and settings.realtime.access_key:
        clone_relay = VoiceCloneRelay(
            app_id=settings.realtime.app_id,
            access_key=settings.realtime.access_key,
        )
    if settings.outbound.enabled:
        repository.seed_builtin_scripts()
        # First-run seed: one editable default template mirroring the legacy
        # .env behaviour, so upgrading deployments keep working unchanged.
        if repository.count_templates() == 0:
            repository.create_template(
                name="内置默认",
                company_name="",
                business_background=settings.outbound.business_background,
                opening_template=DEFAULT_OPENING_TEXT,
                is_default=True,
            )
        # Restart recovery: a batch left as running belongs to a dead process,
        # and customers stuck 进行中 must be redialed on resume.
        repository.reset_running_batches()
        repository.reset_active_customers()

    personalizer: Personalizer | None = None
    if batch_runner is None:
        call_events = CallEventBus()
        workbench_hub = WorkbenchHub()
        if settings.outbound.enabled:
            if settings.realtime.enabled and settings.realtime.dashscope_api_key:
                personalizer = Personalizer(
                    api_key=settings.realtime.dashscope_api_key,
                    model=settings.realtime.subtitle_model,
                    base_url=settings.realtime.dashscope_base_url,
                    fallback_background=(
                        settings.outbound.business_background
                        or FALLBACK_BUSINESS_BACKGROUND
                    ),
                    timeout_seconds=settings.realtime.subtitle_timeout_seconds,
                )
            batch_runner = BatchRunner(
                repository=repository,
                hub=workbench_hub,
                bus=call_events,
                personalizer=personalizer,
            )
    else:
        call_events = batch_runner.bus
        workbench_hub = batch_runner.hub

    if realtime_gateway_factory is None and settings.realtime.enabled:

        def realtime_gateway_factory() -> RealtimeGateway:  # type: ignore[misc]
            return RealtimeGateway(
                settings=settings.realtime,
                repository=repository,
                doubao_factory=lambda session_id, input_mode, persona, speaker=None: DoubaoRealtimeClient(
                    settings.realtime,
                    session_id=session_id,
                    input_mode=input_mode,
                    persona=persona,
                    speaker=speaker,
                ),
                translator=translator,
                scenarios=SCENARIOS,
                outbound_settings=settings.outbound,
                adjudicator=adjudicator,
                call_events=call_events,
            )

    admission = _AdmissionCounter(settings.realtime.max_concurrent_sessions)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if translator is not None:
            await translator.aclose()
        if adjudicator is not None:
            await adjudicator.aclose()
        if personalizer is not None:
            await personalizer.aclose()
        if clone_relay is not None:
            await clone_relay.aclose()

    app = FastAPI(title="Doubao E2E Voice Demo", lifespan=lifespan)

    # -- REST review API -----------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "provider_mode": settings.provider_mode,
            "realtime_provider": settings.realtime.provider,
            "subtitles": (
                "enabled"
                if settings.realtime.enabled and settings.realtime.subtitle_enabled
                else "disabled"
            ),
        }

    @app.get("/api/scenarios")
    async def scenarios() -> dict[str, list[dict[str, str]]]:
        labels = {
            "product_intro": "产品回访（Product intro）",
            "delivery_confirm": "快递地址确认（Delivery confirm）",
            "satisfaction_survey": "满意度调研（Satisfaction survey）",
            OUTBOUND_SCENARIO: "外呼营销（Outbound call）",
        }
        return {
            "scenarios": [
                {"id": scenario_id, "label": labels.get(scenario_id, scenario_id)}
                for scenario_id in SCENARIOS
            ]
        }

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, object]:
        return {"sessions": await _to_thread(repository.list_sessions)}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        session = await _to_thread(repository.get_session, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        deleted = await _to_thread(repository.delete_session, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": True}

    @app.post("/api/sessions/{session_id}/rating")
    async def rate_session(session_id: str, rating: int) -> dict[str, bool]:
        try:
            await _to_thread(repository.set_rating, session_id, rating)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"saved": True}

    @app.get("/api/sessions/{session_id}/export")
    async def export_session(session_id: str) -> JSONResponse:
        session = await _to_thread(repository.get_session, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse(
            session,
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )

    # -- voice clone relay ----------------------------------------------------------

    @app.get("/api/voice-clone/meta")
    async def clone_meta() -> dict[str, object]:
        return {
            "enabled": clone_relay is not None,
            "clone_text": CLONE_TEXT,
            "sample_seconds": CLONE_SAMPLE_SECONDS,
        }

    @app.post("/api/voice-clone/upload")
    async def clone_upload(payload: dict = Body(...)) -> dict[str, object]:
        if clone_relay is None:
            raise HTTPException(status_code=503, detail="音色复刻未配置凭证")
        speaker_id = str(payload.get("speaker_id") or "").strip()
        audio_b64 = str(payload.get("audio_wav_b64") or "")
        try:
            result = await clone_relay.upload(
                speaker_id=speaker_id, audio_b64=audio_b64
            )
        except VoiceCloneError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail=f"上游请求失败: {error}") from error
        return {"ok": True, "speaker_id": result.get("speaker_id") or speaker_id}

    @app.post("/api/voice-clone/status")
    async def clone_status(payload: dict = Body(...)) -> dict[str, object]:
        if clone_relay is None:
            raise HTTPException(status_code=503, detail="音色复刻未配置凭证")
        speaker_id = str(payload.get("speaker_id") or "").strip()
        try:
            result = await clone_relay.status(speaker_id=speaker_id)
        except VoiceCloneError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail=f"上游请求失败: {error}") from error
        # status 2 (Success) or 4 (Active) means the voice is usable.
        status = result.get("status")
        return {
            "speaker_id": result.get("speaker_id") or speaker_id,
            "status": status,
            "ready": status in (2, 4),
            "create_time": result.get("create_time"),
        }

    # -- batch outbound REST ------------------------------------------------------

    def _require_runner() -> BatchRunner:
        if batch_runner is None:
            raise HTTPException(status_code=503, detail="批量外呼未启用")
        return batch_runner

    @app.post("/api/batches/import")
    async def import_batch(file: UploadFile = File(...)) -> dict[str, object]:
        _require_runner()
        content = await file.read()
        try:
            table = parse_table(file.filename or "", content)
        except ImportParseError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        phone_column = detect_phone_column(table)
        rows: list[dict[str, object]] = []
        invalid_rows = 0
        for parsed in table.rows:
            raw_phone = parsed.data.get(phone_column, "") if phone_column else ""
            phone = normalize_phone(str(raw_phone or ""))
            if not phone:
                invalid_rows += 1
                continue
            rows.append(
                {"row_number": parsed.row_number, "raw_data": parsed.data, "phone": phone}
            )
        batch_id = new_batch_id()

        def _persist() -> None:
            repository.create_batch(
                batch_id,
                columns=table.columns,
                total=len(rows),
                phone_column=phone_column or "",
            )
            repository.add_customers(batch_id, rows)

        await _to_thread(_persist)
        return {
            "batch_id": batch_id,
            "status": "draft",
            "total": len(rows),
            "invalid_rows": invalid_rows,
            "columns": table.columns,
            "phone_column": phone_column,
            "preview": [
                {"row_number": row["row_number"], "raw_data": row["raw_data"], "phone": row["phone"]}
                for row in rows[:3]
            ],
        }

    @app.post("/api/batches/{batch_id}/confirm")
    async def confirm_batch(
        batch_id: str, payload: dict | None = Body(default=None)
    ) -> dict[str, object]:
        _require_runner()
        body = payload or {}

        def _confirm() -> dict[str, object]:
            batch = repository.get_batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            phone_column = str(body.get("phone_column") or batch["phone_column"])
            if not phone_column or phone_column not in batch["columns"]:
                raise ValueError("电话列不存在于该批次的列名中")
            # Template binding: explicit id, else the default template, else
            # no template at all (legacy .env behaviour).
            template: dict[str, object] | None = None
            raw_template_id = body.get("template_id")
            if raw_template_id is not None:
                try:
                    template = repository.get_template(int(raw_template_id))
                except (TypeError, ValueError):
                    template = None
                if template is None:
                    raise ValueError("所选外呼模板不存在")
            else:
                template = repository.get_default_template()
            if not repository.confirm_batch(
                batch_id,
                phone_column,
                template_id=(template or {}).get("id"),
                template_name=str((template or {}).get("name") or ""),
            ):
                raise ValueError("只有草稿状态的批次可以确认")
            if phone_column != batch["phone_column"]:
                for customer in repository.list_customers(batch_id):
                    raw_value = (customer["raw_data"] or {}).get(phone_column, "")
                    repository.update_customer_phone(
                        int(customer["id"]), normalize_phone(str(raw_value or ""))
                    )
            return repository.get_batch(batch_id) or {}

        try:
            batch = await _to_thread(_confirm)
        except KeyError:
            raise HTTPException(status_code=404, detail="batch not found")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"batch": batch}

    @app.get("/api/batches/latest")
    async def latest_batch() -> dict[str, object]:
        batch = await _to_thread(repository.get_latest_batch)
        runner = _require_runner()
        return {"batch": batch, "runner": runner.status()}

    @app.get("/api/batches/{batch_id}/customers")
    async def batch_customers(batch_id: str, page: int = 1, size: int = 50) -> dict[str, object]:
        _require_runner()
        batch = await _to_thread(repository.get_batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        page = max(1, page)
        size = min(max(1, size), 500)
        customers = await _to_thread(
            repository.list_customers, batch_id, limit=size, offset=(page - 1) * size
        )
        return {"batch": batch, "customers": customers, "page": page, "size": size}

    @app.get("/api/batches/{batch_id}/export")
    async def export_batch(batch_id: str) -> Response:
        """Download the customer list with call outcomes as a CSV file.

        Encoded utf-8-sig so Excel renders Chinese correctly (the inverse of
        the import-side damage mode).
        """
        _require_runner()
        batch = await _to_thread(repository.get_batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        customers = await _to_thread(repository.list_customers, batch_id)

        def _render() -> bytes:
            columns = list(batch.get("columns") or [])
            # 拨打号码 = normalized number actually dialed (may carry the 00
            # prefix logic downstream); kept distinct from the raw phone column.
            header = ["行号", *columns, "拨打号码", "状态", "结果", "原因", "时长(秒)", "完成时间"]
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            for customer in customers:
                raw_data = customer.get("raw_data") or {}
                writer.writerow(
                    [
                        customer.get("row_number"),
                        *[str(raw_data.get(column, "") or "") for column in columns],
                        customer.get("phone") or "",
                        customer.get("status") or "",
                        customer.get("result") or "",
                        customer.get("reason") or "",
                        customer.get("duration_seconds") if customer.get("duration_seconds") is not None else "",
                        customer.get("finished_at") or "",
                    ]
                )
            return buffer.getvalue().encode("utf-8-sig")

        content = await _to_thread(_render)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{batch_id}.csv"'},
        )

    @app.patch("/api/batches/{batch_id}/customers/{customer_id}")
    async def edit_customer(
        batch_id: str,
        customer_id: int,
        payload: dict = Body(...),
    ) -> dict[str, object]:
        _require_runner()
        edits = payload.get("raw_data")
        if not isinstance(edits, dict):
            raise HTTPException(status_code=400, detail="缺少 raw_data 字段")

        def _edit() -> dict[str, object]:
            batch = repository.get_batch(batch_id)
            customer = repository.get_customer(customer_id)
            if batch is None or customer is None or customer["batch_id"] != batch_id:
                raise KeyError(customer_id)
            if customer["status"] in ("进行中", "已完成"):
                raise ValueError("该客户状态不允许编辑")
            raw_data = dict(customer["raw_data"] or {})
            changed = False
            for column, value in edits.items():
                if str(column).startswith("_"):
                    continue
                if raw_data.get(column) != value:
                    raw_data[column] = value
                    changed = True
            if changed:
                # Stale LLM cache: regenerate background on the next dial.
                raw_data.pop(CACHE_KEY, None)
            repository.update_customer_raw_data(customer_id, raw_data)
            phone_column = batch["phone_column"]
            if phone_column:
                repository.update_customer_phone(
                    customer_id, normalize_phone(str(raw_data.get(phone_column) or ""))
                )
            return repository.get_customer(customer_id) or {}

        try:
            customer = await _to_thread(_edit)
        except KeyError:
            raise HTTPException(status_code=404, detail="customer not found")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"customer": customer}

    @app.get("/api/batches/{batch_id}/customers/{customer_id}/transcript")
    async def customer_transcript(batch_id: str, customer_id: int) -> dict[str, object]:
        _require_runner()

        def _transcript() -> dict[str, object]:
            customer = repository.get_customer(customer_id)
            if customer is None or customer["batch_id"] != batch_id:
                raise KeyError(customer_id)
            session_id = customer.get("session_id")
            if not session_id:
                return {"customer_id": customer_id, "session_id": None, "turns": []}
            session = repository.get_session(str(session_id))
            turns = (session or {}).get("turns", [])
            return {"customer_id": customer_id, "session_id": session_id, "turns": turns}

        try:
            return await _to_thread(_transcript)
        except KeyError:
            raise HTTPException(status_code=404, detail="customer not found")

    @app.post("/api/batches/{batch_id}/start")
    async def start_batch(batch_id: str) -> dict[str, object]:
        runner = _require_runner()
        if runner.running:
            if runner.active_batch_id == batch_id:
                return runner.status()  # idempotent
            raise HTTPException(status_code=409, detail="已有批次正在运行")
        try:
            return await runner.start_batch(batch_id)
        except BatchRunnerError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/batches/{batch_id}/stop")
    async def stop_batch(batch_id: str) -> dict[str, object]:
        runner = _require_runner()
        if not runner.running or runner.active_batch_id != batch_id:
            raise HTTPException(status_code=409, detail="该批次未在运行")
        try:
            return await runner.stop_batch()
        except BatchRunnerError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    # -- campaign templates ---------------------------------------------------------

    def _parse_template_payload(
        body: dict[str, object],
    ) -> tuple[dict[str, object], list[dict[str, object]], list[str]]:
        """Normalize + validate the template form; returns (fields, scripts,
        errors). Errors are per-item strings the UI can display directly."""
        errors: list[str] = []
        name = str(body.get("name") or "").strip()
        company_name = str(body.get("company_name") or "").strip()
        bot_name = str(body.get("bot_name") or "").strip()
        if not name:
            errors.append("模板名不能为空")
        if not company_name:
            errors.append("公司名称不能为空")
        if len(bot_name) > MAX_BOT_NAME_CHARS:
            errors.append(f"AI 名字不能超过 {MAX_BOT_NAME_CHARS} 字")
        raw_scripts = body.get("scripts") or []
        if not isinstance(raw_scripts, list):
            errors.append("话术列表格式错误")
            raw_scripts = []
        scripts: list[dict[str, object]] = []
        for index, item in enumerate(raw_scripts, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 条话术格式错误")
                continue
            triggers = [
                str(trigger).strip()
                for trigger in (item.get("triggers") or [])
                if str(trigger or "").strip()
            ]
            try:
                priority = int(item.get("priority", 5))
            except (TypeError, ValueError):
                priority = -1
            script = Script(
                category=str(item.get("category") or ""),
                triggers=tuple(triggers),
                reply=str(item.get("reply") or ""),
                end_call=bool(item.get("end_call")),
                verdict=str(item.get("verdict") or ""),
                priority=priority,
                description=str(item.get("description") or ""),
            )
            for problem in validate_script(script):
                errors.append(f"第 {index} 条话术：{problem}")
            scripts.append(
                {
                    "category": script.category.strip(),
                    "triggers": triggers,
                    "reply": script.reply,
                    "end_call": script.end_call,
                    "verdict": script.verdict,
                    "priority": priority,
                    "description": script.description,
                }
            )
        fields = {
            "name": name,
            "company_name": company_name,
            "business_background": str(body.get("business_background") or "").strip(),
            "opening_template": str(body.get("opening_template") or "").strip(),
            "bot_name": bot_name,
            "speaking_style": str(body.get("speaking_style") or "").strip(),
        }
        return fields, scripts, errors

    @app.get("/api/templates")
    async def list_templates_api() -> dict[str, object]:
        return {"templates": await _to_thread(repository.list_templates)}

    @app.post("/api/templates")
    async def create_template(payload: dict = Body(...)) -> dict[str, object]:
        fields, scripts, errors = _parse_template_payload(payload)
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        try:
            template = await _to_thread(
                repository.create_template,
                is_default=bool(payload.get("is_default")),
                scripts=scripts,
                **fields,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"template": template}

    @app.put("/api/templates/{template_id}")
    async def update_template(
        template_id: int, payload: dict = Body(...)
    ) -> dict[str, object]:
        fields, scripts, errors = _parse_template_payload(payload)
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        try:
            template = await _to_thread(
                repository.update_template,
                template_id,
                scripts=scripts,
                **fields,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        return {"template": template}

    @app.delete("/api/templates/{template_id}")
    async def delete_template(template_id: int) -> dict[str, bool]:
        deleted = await _to_thread(repository.delete_template, template_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="template not found")
        return {"deleted": True}

    @app.post("/api/templates/{template_id}/default")
    async def set_default_template(template_id: int) -> dict[str, bool]:
        updated = await _to_thread(repository.set_default_template, template_id)
        if not updated:
            raise HTTPException(status_code=404, detail="template not found")
        return {"default": True}

    # -- workbench WebSocket --------------------------------------------------------

    @app.websocket("/ws/workbench")
    async def workbench_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        if batch_runner is None:
            await websocket.close(code=1013, reason="batch outbound is disabled")
            return
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in settings.realtime.allowed_origins:
            await websocket.close(code=1008, reason="origin is not allowed")
            return
        await workbench_hub.register(websocket)
        try:
            await websocket.send_json(
                {"type": "hello", "runner": batch_runner.status()}
            )
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                message_type = str(message.get("type") or "")
                if message_type == "workbench.hello":
                    if message.get("role") == "bridge":
                        await workbench_hub.set_bridge(
                            websocket, in_call=bool(message.get("in_call"))
                        )
                elif message_type.startswith("bridge."):
                    await workbench_hub.deliver_bridge_message(message)
        except Exception:
            logger.info("workbench connection closed")
        finally:
            await workbench_hub.unregister(websocket)

    # -- realtime WebSocket -----------------------------------------------------

    @app.websocket("/ws/realtime")
    async def realtime_voice(websocket: WebSocket) -> None:
        await websocket.accept()
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in settings.realtime.allowed_origins:
            await websocket.close(code=1008, reason="origin is not allowed")
            return
        if not settings.realtime.enabled or realtime_gateway_factory is None:
            await websocket.close(code=1013, reason="realtime voice is disabled")
            return
        if not admission.try_acquire():
            await websocket.close(code=1013, reason="realtime capacity is full")
            return
        gateway = realtime_gateway_factory()
        try:
            await gateway.run(websocket)
        finally:
            admission.release()

    # -- static UI --------------------------------------------------------------

    static_dir = BUNDLE_ROOT / "app" / "static"
    static_app = StaticFiles(directory=static_dir)
    _static_file_response = static_app.file_response

    def no_cache_file_response(*args, **kwargs):
        response = _static_file_response(*args, **kwargs)
        # Dev server: never let browsers cache stale JS/CSS across restarts.
        response.headers["Cache-Control"] = "no-store"
        return response

    static_app.file_response = no_cache_file_response
    app.mount("/static", static_app, name="static")

    def _page_response(filename: str) -> Response:
        html = (static_dir / filename).read_text(encoding="utf-8")
        if settings.base_path:
            # Served under a URL prefix (e.g. nginx /v2): tell the JS where
            # API/WebSocket routes live after the prefix is stripped upstream.
            html = html.replace(
                "</head>",
                f'<script>window.APP_BASE="{settings.base_path}";</script></head>',
                1,
            )
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/")
    async def index() -> Response:
        return _page_response("index.html")

    @app.get("/workbench")
    async def workbench_page() -> Response:
        return _page_response("workbench.html")

    @app.get("/templates")
    async def templates_page() -> Response:
        return _page_response("templates.html")

    if settings.auth.enabled:
        install_auth(
            app,
            settings.auth,
            settings.base_path,
            static_dir,
            fallback_frontend_url=f"http://{settings.host}:{settings.port}",
        )

    return app


async def _to_thread(func, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


def build_app() -> FastAPI:
    return create_app(settings_from_env())
