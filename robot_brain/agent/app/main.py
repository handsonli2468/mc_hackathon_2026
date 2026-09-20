from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .bootstrap import bootstrap_runtime
from .bt_engine_client import (
    BtEngineUnavailable,
    cancel as bt_engine_cancel,
    execute as bt_engine_execute,
    health as bt_engine_health,
    nodes as bt_engine_nodes,
    runs as bt_engine_runs,
    status as bt_engine_status,
    validate as bt_engine_validate,
)
from .bt_engine_contract import validate_runtime_contract
from .bt_node_sync import (
    BtNodeSyncFailure,
    get_bt_node_sync_status,
    persist_bt_node_sync_report,
    record_sync_failure,
    synchronize_bt_nodes_from_xml,
)
from .bt_xml import validate_bt_xml
from .graph import brain_graph
from .conversation_grounding import build_pending_grounding_context
from . import execution_bridge as exec_bridge
from .model_client import model_server_models
from .registry import load_builtin_registry, load_skill_registry, skill_registry_as_bt_nodes
from .rag import (
    get_user_feedback,
    init_rag_db,
    record_execution_experience,
    record_user_feedback_experience,
    retrieve_context,
    status as rag_status,
    sync_rag_knowledge,
)
from .schemas import MissionFeedbackRequest, SessionResetRequest
from .settings import settings
from .store import (
    add_execution_event,
    add_message,
    get_execution_run,
    get_history,
    get_mission,
    get_pending_mission,
    set_pending_mission,
    clear_pending_mission,
    init_db,
    list_execution_events,
    save_mission,
    upsert_execution_run,
)
from .telemetry import persist_trace, reset_trace, snapshot_trace, start_trace


app = FastAPI(title="Robot Brain Comparator", version=settings.version)
origins = [x.strip() for x in settings.cors_origins.split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_vision: bool = False
    auto_execute: bool = True


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    session_id: str | None = None
    pipeline_mode: Literal["hybrid", "direct", "compare"] = "hybrid"
    world_state: dict[str, Any] | None = None
    options: Options = Field(default_factory=Options)
    _planning_message: str | None = PrivateAttr(default=None)
    _seed_requirements: dict[str, Any] | None = PrivateAttr(default=None)


class BtGenerationSummary(BaseModel):
    succeeded: bool
    validated: bool
    message: str


class AutoExecutionSummary(BaseModel):
    enabled: bool
    attempted: bool
    started: bool
    status: Literal["STARTED", "SKIPPED", "FAILED"]
    mission_id: str | None = None
    run_id: str | None = None
    engine_state: str | None = None
    preempted_previous: bool = False
    poll_interval_s: float | None = None
    reason: str | None = None
    error: Any | None = None


class ChatCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    pipeline: Literal["hybrid", "direct"]
    status: str | None = None
    message: str = ""
    questions: list[dict[str, Any]] = Field(default_factory=list)
    mission_id: str | None = None
    bt_xml: str | None = None
    bt_generation: BtGenerationSummary
    auto_execution: AutoExecutionSummary


class ChatResponse(BaseModel):
    session_id: str
    pipeline_mode: Literal["hybrid", "direct", "compare"]
    candidate: ChatCandidateResponse | None = None
    candidates: dict[str, ChatCandidateResponse] | None = None
    conversation_grounding: dict[str, Any] = Field(default_factory=dict)


class ValidateRequest(BaseModel):
    xml: str = Field(min_length=1)


class RagRetrieveRequest(BaseModel):
    message: str = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)
    retrieval_sketch: dict[str, Any] = Field(default_factory=dict)
    world_state: dict[str, Any] | None = None


class ExecutionEvent(BaseModel):
    node_uid: str | None = None
    node_name: str | None = None
    node_type: str | None = None
    status: str
    mission_status: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplanRequest(BaseModel):
    pipeline_mode: Literal["hybrid", "direct"] = "hybrid"


class SocketHub:
    def __init__(self):
        self.clients: dict[str, set[WebSocket]] = defaultdict(set)
        self.lock = asyncio.Lock()

    async def connect(self, sid: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients[sid].add(ws)

    async def disconnect(self, sid: str, ws: WebSocket) -> None:
        async with self.lock:
            self.clients[sid].discard(ws)
            if not self.clients[sid]:
                self.clients.pop(sid, None)

    async def broadcast(self, sid: str, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients.get(sid, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(sid, ws)


hub = SocketHub()
_engine_tasks: set[asyncio.Task[Any]] = set()
_bt_node_sync_lock = asyncio.Lock()


def _remember_task(task: asyncio.Task[Any]) -> None:
    _engine_tasks.add(task)
    task.add_done_callback(_engine_tasks.discard)


def _engine_body_dict(body: Any) -> dict[str, Any]:
    return body if isinstance(body, dict) else {"raw": body}


async def _engine_health_snapshot() -> dict[str, Any]:
    if not settings.bt_engine_enabled:
        return {
            "enabled": False,
            "ok": False,
            "reachable": False,
            "auth_ok": None,
            "token_configured": bool(settings.bt_engine_token),
            "url": settings.bt_engine_url,
            "error": "disabled",
        }
    try:
        health_resp = await bt_engine_health()
        health_body = _engine_body_dict(health_resp.body)
        reachable = bool(health_resp.ok and health_body.get("ok", True))
        if not reachable:
            return {
                "enabled": True,
                "ok": False,
                "reachable": False,
                "auth_ok": None,
                "token_configured": bool(settings.bt_engine_token),
                "url": settings.bt_engine_url,
                "http_status": health_resp.status_code,
                "response": health_body,
            }

        # /health is intentionally public. Probe one protected, lightweight endpoint
        # so the UI can distinguish "engine reachable" from "token missing/wrong".
        auth_resp = await bt_engine_runs()
        auth_body = auth_resp.body if isinstance(auth_resp.body, (dict, list)) else _engine_body_dict(auth_resp.body)
        auth_ok = bool(auth_resp.ok)
        error_kind = "auth" if auth_resp.status_code == 401 else (None if auth_ok else "engine_api")
        return {
            "enabled": True,
            "ok": bool(reachable and auth_ok),
            "reachable": reachable,
            "auth_ok": auth_ok,
            "token_configured": bool(settings.bt_engine_token),
            "url": settings.bt_engine_url,
            "http_status": health_resp.status_code,
            "auth_http_status": auth_resp.status_code,
            "response": health_body,
            "auth_response": auth_body,
            "error_kind": error_kind,
            "error": (
                "BT engine token is missing or wrong."
                if auth_resp.status_code == 401
                else (None if auth_ok else f"Protected BT-engine API probe returned HTTP {auth_resp.status_code}.")
            ),
        }
    except BtEngineUnavailable as exc:
        return {
            "enabled": True,
            "ok": False,
            "reachable": False,
            "auth_ok": None,
            "token_configured": bool(settings.bt_engine_token),
            "url": settings.bt_engine_url,
            "error_kind": "network",
            "error": str(exc),
        }


async def _apply_engine_status(mission_id: str, payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    try:
        result = exec_bridge.persist_engine_status(mission_id, payload, source=source)
    except KeyError as exc:
        raise HTTPException(404, "Mission not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    item = result["mission"]
    if result.get("event") is not None:
        await hub.broadcast(item["session_id"], {"type": "execution_event", "event": result["event"]})
    if result.get("terminal_new"):
        experience = None
        try:
            experience = record_execution_experience(item, result["payload"])
        except Exception:
            experience = None
        await hub.broadcast(
            item["session_id"],
            {
                "type": "bt_engine_terminal",
                "mission_id": mission_id,
                "state": result["payload"].get("state"),
                "report": result.get("report"),
                "feedback": result.get("feedback"),
                "payload": result["payload"],
                "experience": experience,
            },
        )
    return result["execution"]


async def _poll_engine_run(mission_id: str, run_id: str) -> None:
    started = time.monotonic()
    try:
        while time.monotonic() - started <= settings.bt_engine_max_poll_seconds:
            try:
                resp = await bt_engine_status(run_id)
            except BtEngineUnavailable as exc:
                item = get_mission(mission_id)
                if item:
                    await hub.broadcast(
                        item["session_id"],
                        {"type": "bt_engine_monitor_error", "mission_id": mission_id, "error": str(exc)},
                    )
                return
            if not resp.ok or not isinstance(resp.body, dict):
                item = get_mission(mission_id)
                if item:
                    await hub.broadcast(
                        item["session_id"],
                        {
                            "type": "bt_engine_monitor_error",
                            "mission_id": mission_id,
                            "error": f"GET /status/{run_id} returned HTTP {resp.status_code}: {resp.raw_text}",
                        },
                    )
                return
            current = await _apply_engine_status(mission_id, resp.body, source="bt_engine")
            if current.get("state") in exec_bridge.TERMINAL_ENGINE_STATES:
                return
            await asyncio.sleep(max(0.05, settings.bt_engine_poll_interval))

        item = get_mission(mission_id)
        if item:
            await hub.broadcast(
                item["session_id"],
                {
                    "type": "bt_engine_monitor_error",
                    "mission_id": mission_id,
                    "error": f"Agent stopped polling after {settings.bt_engine_max_poll_seconds:g}s; the engine may still be running.",
                },
            )
    except Exception as exc:
        item = get_mission(mission_id)
        if item:
            await hub.broadcast(
                item["session_id"],
                {"type": "bt_engine_monitor_error", "mission_id": mission_id, "error": str(exc)},
            )


async def _sync_live_engine_nodes(*, trigger: str) -> dict[str, Any]:
    """Synchronize live custom nodes, rebuild merged skills, then refresh Skill RAG.

    The lock makes startup/manual sync atomic from the API's point of view: two
    concurrent requests cannot interleave registry writes and RAG refreshes.
    """
    async with _bt_node_sync_lock:
        try:
            resp = await bt_engine_nodes(include_builtin=False)
        except BtEngineUnavailable as exc:
            record_sync_failure(exc, trigger=trigger)
            raise
        if not resp.ok:
            error = BtNodeSyncFailure(
                f"GET /nodes?builtin=0 returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                detail=_engine_body_dict(resp.body),
            )
            record_sync_failure(error, trigger=trigger)
            raise error

        try:
            report = synchronize_bt_nodes_from_xml(resp.raw_text, trigger=trigger)
        except Exception as exc:
            record_sync_failure(exc, trigger=trigger)
            raise

        if settings.rag_enabled:
            try:
                report["rag_sync"] = {"ok": True, "counts": sync_rag_knowledge()}
            except Exception as exc:
                report["rag_sync"] = {"ok": False, "error": str(exc)}
                report["ok"] = False
                report["error"] = f"BT nodes synchronized, but RAG refresh failed: {exc}"
        else:
            report["rag_sync"] = {"ok": True, "skipped": True, "reason": "RAG disabled"}
        persist_bt_node_sync_report(report)
        return report


@app.on_event("startup")
async def startup() -> None:
    bootstrap_runtime()
    init_db()
    init_rag_db()

    # v6.1: every Agent process start/restart performs one synchronous live-node
    # synchronization before the service begins planning.  The attempt is recorded
    # even when the robot is offline; last-known-good contracts remain available.
    synced_rag = False
    if settings.bt_engine_enabled and not settings.bt_engine_skip_startup_node_sync:
        try:
            report = await _sync_live_engine_nodes(trigger="agent_startup")
            synced_rag = bool((report.get("rag_sync") or {}).get("ok"))
        except Exception:
            # Offline development remains possible, but /health and /api/info expose
            # the failed startup sync instead of silently pretending it succeeded.
            pass
    elif settings.bt_engine_enabled:
        record_sync_failure(
            RuntimeError("Startup BT-node synchronization skipped by BT_ENGINE_SKIP_STARTUP_NODE_SYNC=1."),
            trigger="agent_startup_skipped",
        )

    # If live sync was unavailable, rebuild RAG from the last-known-good merged
    # registry so planning is internally consistent with the cached BT contract.
    if settings.rag_enabled and not synced_rag:
        sync_rag_knowledge()


@app.get("/health")
async def health():
    try:
        planner = await model_server_models(settings.planner_base_url)
        planner_ok = True
    except Exception:
        planner = []
        planner_ok = False
    try:
        compiler = await model_server_models(settings.compiler_base_url)
        compiler_ok = True
    except Exception:
        compiler = []
        compiler_ok = False
    try:
        contract = validate_runtime_contract()
        contract_ok = bool(contract.get("valid")) and (
            bool(contract.get("semantic_complete")) or not settings.bt_engine_semantic_complete_for_health
        )
    except Exception as exc:
        contract = {"valid": False, "errors": [str(exc)], "warnings": []}
        contract_ok = False
    engine = await _engine_health_snapshot()
    node_sync = get_bt_node_sync_status()
    engine_gate = bool(engine.get("ok")) if settings.bt_engine_required_for_health else True
    rag = rag_status()
    rag_ok = (not settings.rag_required) or int((rag.get("documents") or {}).get("skill", 0)) > 0
    overall_ok = planner_ok and (compiler_ok or not settings.enable_btgenbot) and contract_ok and engine_gate and rag_ok
    return {
        "ok": overall_ok,
        "version": settings.version,
        "planner": {"ok": planner_ok, "model": settings.planner_model, "available": planner},
        "compiler": {"enabled": settings.enable_btgenbot, "ok": compiler_ok, "model": settings.compiler_model, "available": compiler},
        "bt_engine": {**engine, "auto_execute": settings.bt_engine_auto_execute},
        "bt_engine_contract": contract,
        "bt_node_sync": node_sync,
        "rag": {**rag, "ok": rag_ok},
        "project_root": str(settings.project_root),
        "runtime_root": str(settings.runtime_root),
    }


@app.get("/api/info")
def info():
    reg = load_skill_registry()
    return {
        "version": settings.version,
        "default_pipeline": "hybrid",
        "pipelines": ["hybrid", "direct", "compare"],
        "planner_model": settings.planner_model,
        "compiler_model": settings.compiler_model,
        "skill_library_version": reg.get("version"),
        "skill_count": len(reg.get("skills", [])),
        "bt_engine_contract": validate_runtime_contract(),
        "bt_node_sync": get_bt_node_sync_status(),
        "bt_engine": {
            "enabled": settings.bt_engine_enabled,
            "auto_execute": settings.bt_engine_auto_execute,
            "url": settings.bt_engine_url,
            "token_configured": bool(settings.bt_engine_token),
        },
        "rag": rag_status(),
        "project_root": str(settings.project_root),
        "runtime_root": str(settings.runtime_root),
    }


@app.get("/api/skills")
def skills():
    return load_skill_registry()


@app.get("/api/rag/status")
def api_rag_status():
    return rag_status()


@app.post("/api/rag/sync")
def api_rag_sync():
    if not settings.rag_enabled:
        raise HTTPException(409, "RAG is disabled")
    return {"ok": True, "synced": sync_rag_knowledge(), "status": rag_status()}


@app.post("/api/rag/retrieve")
def api_rag_retrieve(req: RagRetrieveRequest):
    if not settings.rag_enabled:
        raise HTTPException(409, "RAG is disabled")
    return retrieve_context(
        req.message,
        {
            "required_capabilities": req.required_capabilities,
            "retrieval_sketch": req.retrieval_sketch,
        },
        world_state=req.world_state,
    )


@app.post("/api/validate-bt")
def validate_bt(req: ValidateRequest):
    return validate_bt_xml(req.xml, skill_registry_as_bt_nodes(), load_builtin_registry())


@app.get("/api/bt-engine/health")
async def api_bt_engine_health():
    return await _engine_health_snapshot()


@app.get("/api/bt-engine/nodes")
async def api_bt_engine_nodes(builtin: bool = False):
    try:
        resp = await bt_engine_nodes(include_builtin=builtin)
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(resp.raw_text, status_code=resp.status_code, media_type="application/xml")


@app.post("/api/bt-engine/sync-nodes")
async def api_bt_engine_sync_nodes():
    try:
        return await _sync_live_engine_nodes(trigger="manual_api")
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except BtNodeSyncFailure as exc:
        raise HTTPException(exc.status_code, {"message": str(exc), "detail": exc.detail}) from exc


@app.get("/api/bt-engine/sync-status")
def api_bt_engine_sync_status():
    return get_bt_node_sync_status()


@app.get("/api/bt-engine/runs")
async def api_bt_engine_runs():
    try:
        resp = await bt_engine_runs()
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not resp.ok:
        raise HTTPException(resp.status_code, _engine_body_dict(resp.body))
    return resp.body


async def run_pipeline(mode: str, req: ChatRequest, history: list[dict[str, str]]) -> dict[str, Any]:
    started = time.perf_counter()
    trace_token = start_trace({
        "pipeline": mode,
        "session_id": req.session_id,
        "planner_thinking_mode": "enabled_rag_fast_first_v6",
        "rag_enabled": settings.rag_enabled,
        "rag_required": settings.rag_required,
        "compact_planner_enabled": settings.compact_planner_enabled,
        "compact_planner_thinking_budget": settings.compact_planner_thinking_budget,
        "planner_task_thinking_budget_simple": settings.planner_task_thinking_budget,
        "planner_task_retry_thinking_budget": settings.planner_task_retry_thinking_budget,
        "plan_critic_mode": settings.plan_critic_mode,
        "plan_auto_harden_search_recovery": settings.plan_auto_harden_search_recovery,
        "btgenbot_enabled": settings.enable_btgenbot,
    })
    try:
        state = await brain_graph.ainvoke({
            "message": req._planning_message or req.message,
            "history": history,
            "world_state": req.world_state or {},
            "pipeline_mode": mode,
            "seed_requirements": req._seed_requirements,
        })
        elapsed_sec = time.perf_counter() - started
        timing = snapshot_trace(elapsed_sec)
    except Exception:
        elapsed_sec = time.perf_counter() - started
        timing = snapshot_trace(elapsed_sec)
        try:
            if settings.timing_trace_persist:
                persist_trace(timing, {"pipeline": mode, "status": "UNHANDLED_EXCEPTION"})
        finally:
            reset_trace(trace_token)
        raise
    reset_trace(trace_token)
    mission_id = None
    artifacts = {
        "requirements": state.get("requirements"),
        "requirement_normalization": state.get("requirement_normalization", {}),
        "rag_context": state.get("rag_context", {}),
        "rag_validation": state.get("rag_validation", {}),
        "rag_utilization": state.get("rag_utilization", {}),
        "capability_check": state.get("capability_check"),
        "bt_engine_contract": state.get("bt_engine_contract", {}),
        "compact_semantic_plan": state.get("compact_semantic_plan"),
        "compact_plan_normalization": state.get("compact_plan_normalization", {}),
        "compact_fallback_reason": state.get("compact_fallback_reason"),
        "compact_planner_used": bool(state.get("compact_planner_used", False)),
        "planner_escalated": bool(state.get("planner_escalated", False)),
        "planner_task_plan_ir": state.get("planner_task_plan_ir"),
        "task_plan_ir": state.get("task_plan_ir"),
        "location_grounding_policy": state.get("location_grounding_policy", {}),
        "grounding_policy": state.get("grounding_policy", {}),
        "skill_contract_normalization": state.get("skill_contract_normalization", {}),
        "plan_hardening": state.get("plan_hardening", {}),
        "compiler_task_plan_ir": state.get("compiler_task_plan_ir"),
        "ir_validation": state.get("ir_validation", {}),
        "quality_gate": state.get("quality_gate", {}),
        "quality_gate_before_critic": state.get("quality_gate_before_critic", {}),
        "quality_gate_after_critic": state.get("quality_gate_after_critic", {}),
        "ir_normalization": state.get("ir_normalization", {}),
        "compiler_capabilities": state.get("compiler_capabilities", {}),
        "bt_validation": state.get("bt_validation", {}),
        "bt_normalization": state.get("bt_normalization", {}),
        "metrics": state.get("metrics", {}),
        "critic_warning": state.get("critic_warning"),
        "critic_attempted": bool(state.get("critic_attempted", False)),
        "compiler_phases": state.get("compiler_phases", []),
        "compiler_warnings": state.get("compiler_warnings", []),
        "compiler_fallback_count": state.get("compiler_fallback_count", 0),
        "ir_repair_attempts": state.get("ir_repair_attempts", 0),
        "bt_repair_attempts": state.get("bt_repair_attempts", 0),
        "elapsed_sec": elapsed_sec,
        "timing": timing,
        "planner_model": settings.planner_model,
        "compiler_model": settings.compiler_model if mode == "hybrid" else None,
        "skill_library_version": load_skill_registry().get("version"),
        "world_state": req.world_state or {},
    }
    if state.get("status") == "SUCCESS" and state.get("bt_xml") and state.get("bt_validation", {}).get("valid"):
        mission_id = str(uuid.uuid4())
        save_mission(mission_id, req.session_id or "", mode, req._planning_message or req.message, state["status"], state["bt_xml"], artifacts)
    try:
        if settings.timing_trace_persist:
            persist_trace(timing, {
                "pipeline": mode,
                "status": state.get("status"),
                "mission_id": mission_id,
                "fallback_count": int(state.get("compiler_fallback_count", 0) or 0),
                "ir_repairs": int(state.get("ir_repair_attempts", 0) or 0),
                "bt_repairs": int(state.get("bt_repair_attempts", 0) or 0),
            })
    except Exception:
        pass
    bt_generated = bool(
        state.get("status") == "SUCCESS"
        and state.get("bt_xml")
        and state.get("bt_validation", {}).get("valid")
        and mission_id
    )
    return {
        "pipeline": mode,
        "status": state.get("status"),
        "message": state.get("assistant_message", ""),
        "questions": state.get("questions", []),
        "missing_capabilities": state.get("missing_capabilities", []),
        "requirements": state.get("requirements"),
        "requirement_normalization": state.get("requirement_normalization", {}),
        "rag_context": state.get("rag_context", {}),
        "rag_validation": state.get("rag_validation", {}),
        "rag_utilization": state.get("rag_utilization", {}),
        "bt_engine_contract": state.get("bt_engine_contract", {}),
        "compact_semantic_plan": state.get("compact_semantic_plan"),
        "compact_plan_normalization": state.get("compact_plan_normalization", {}),
        "compact_fallback_reason": state.get("compact_fallback_reason"),
        "compact_planner_used": bool(state.get("compact_planner_used", False)),
        "planner_escalated": bool(state.get("planner_escalated", False)),
        "planner_task_plan_ir": state.get("planner_task_plan_ir"),
        "task_plan_ir": state.get("task_plan_ir"),
        "location_grounding_policy": state.get("location_grounding_policy", {}),
        "grounding_policy": state.get("grounding_policy", {}),
        "skill_contract_normalization": state.get("skill_contract_normalization", {}),
        "plan_hardening": state.get("plan_hardening", {}),
        "compiler_task_plan_ir": state.get("compiler_task_plan_ir") if mode == "hybrid" else None,
        "ir_validation": state.get("ir_validation", {}),
        "quality_gate": state.get("quality_gate", {}),
        "quality_gate_before_critic": state.get("quality_gate_before_critic", {}),
        "quality_gate_after_critic": state.get("quality_gate_after_critic", {}),
        "ir_normalization": state.get("ir_normalization", {}),
        "compiler_capabilities": state.get("compiler_capabilities", {}) if mode == "hybrid" else {},
        "critic_warning": state.get("critic_warning"),
        "critic_attempted": bool(state.get("critic_attempted", False)),
        "bt_xml": state.get("bt_xml"),
        "bt_validation": state.get("bt_validation", {}),
        "bt_generation": {
            "succeeded": bt_generated,
            "validated": bool(state.get("bt_validation", {}).get("valid")),
            "message": (
                "BehaviorTree XML generated and passed deterministic validation."
                if bt_generated
                else "BehaviorTree XML was not generated successfully; nothing was sent to the BT Engine."
            ),
        },
        "auto_execution": {
            "enabled": bool(settings.bt_engine_auto_execute),
            "attempted": False,
            "started": False,
            "status": "SKIPPED",
            "mission_id": mission_id,
            "reason": "awaiting automatic dispatch" if bt_generated else "no validated BehaviorTree XML",
        },
        "bt_normalization": state.get("bt_normalization", {}),
        "compiler_task": state.get("compiler_task") if mode == "hybrid" else None,
        "compiler_phases": state.get("compiler_phases", []) if mode == "hybrid" else [],
        "compiler_warnings": state.get("compiler_warnings", []) if mode == "hybrid" else [],
        "compiler_fallback_count": state.get("compiler_fallback_count", 0) if mode == "hybrid" else 0,
        "raw_bt_response": state.get("raw_bt_response") if state.get("status") != "SUCCESS" else None,
        "metrics": state.get("metrics", {}),
        "ir_repair_attempts": state.get("ir_repair_attempts", 0),
        "bt_repair_attempts": state.get("bt_repair_attempts", 0),
        "elapsed_sec": round(elapsed_sec, 3),
        "timing": timing,
        "mission_id": mission_id,
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    sid = req.session_id or str(uuid.uuid4())
    req.session_id = sid
    history = get_history(sid, 16)
    pending = get_pending_mission(sid)

    # Slot-fill a pending mission deterministically before invoking the planner.
    # This keeps a reply such as "I am beside the table" attached to the original
    # delivery mission and exposes the visual recipient query to both planners.
    if pending:
        world, grounding_ctx = build_pending_grounding_context(pending, req.message, req.world_state)
        req.world_state = world
        original = str(pending.get("original_request") or "").strip()
        req._planning_message = (
            f"Original pending mission: {original}\nUser clarification: {req.message}" if original else req.message
        )
        seed = dict(pending.get("requirements") or {})
        if seed:
            seed["status_hint"] = "READY"
            seed["required_user_information"] = []
            seed["message"] = "Pending mission clarification received; continue planning the original mission."
            req._seed_requirements = seed
    else:
        grounding_ctx = {}

    add_message(sid, "user", req.message)
    if req.pipeline_mode == "compare":
        direct = await run_pipeline("direct", req, history)
        hybrid = await run_pipeline("hybrid", req, history)
        direct = await _attach_auto_execution(
            direct, allow=False, skip_reason="compare mode never executes multiple candidate trees"
        )
        hybrid = await _attach_auto_execution(
            hybrid, allow=False, skip_reason="compare mode never executes multiple candidate trees"
        )
        response = {"session_id": sid, "pipeline_mode": "compare", "candidates": {"direct": direct, "hybrid": hybrid}, "conversation_grounding": grounding_ctx}
        primary = hybrid if hybrid.get("message") else direct
        summary = primary.get("message") or f"Comparison complete. direct={direct.get('status')}; hybrid={hybrid.get('status')}"
        if primary.get("questions"):
            summary += "\n" + "\n".join(q.get("question", "") for q in primary["questions"] if q.get("question"))
        add_message(sid, "assistant", summary)
        if primary.get("status") == "NEED_MORE_INFO":
            set_pending_mission(sid, {"original_request": pending.get("original_request") if pending else req.message, "requirements": primary.get("requirements") or {}, "questions": primary.get("questions") or []})
        else:
            clear_pending_mission(sid)
        return response

    result = await run_pipeline(req.pipeline_mode, req, history)
    result = await _attach_auto_execution(result, allow=req.options.auto_execute)
    result["conversation_grounding"] = grounding_ctx
    add_message(sid, "assistant", result.get("message", ""))
    if result.get("status") == "NEED_MORE_INFO":
        set_pending_mission(
            sid,
            {
                "original_request": pending.get("original_request") if pending else req.message,
                "requirements": result.get("requirements") or {},
                "questions": result.get("questions") or [],
            },
        )
    else:
        clear_pending_mission(sid)
    return {"session_id": sid, "pipeline_mode": req.pipeline_mode, "candidate": result, "conversation_grounding": grounding_ctx}


@app.post("/api/sessions/reset")
def reset_session(req: SessionResetRequest | None = None):
    """Start a clean conversation while preserving the previous session as audit history."""
    return {
        "session_id": str(uuid.uuid4()),
        "previous_session_id": req.previous_session_id if req else None,
        "history_carried_over": False,
    }


@app.get("/api/missions/{mission_id}")
def mission(mission_id: str):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    item["events"] = list_execution_events(mission_id)
    item["execution"] = get_execution_run(mission_id)
    return item


@app.get("/api/missions/{mission_id}/bt.xml")
def mission_xml(mission_id: str):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    return Response(item["bt_xml"] or "", media_type="application/xml")


@app.get("/api/missions/{mission_id}/feedback")
def mission_feedback(mission_id: str):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    feedback = get_user_feedback(mission_id)
    if feedback is None:
        raise HTTPException(404, "Mission feedback not found")
    return feedback


@app.post("/api/missions/{mission_id}/feedback", status_code=201)
def submit_mission_feedback(mission_id: str, req: MissionFeedbackRequest):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    execution = get_execution_run(mission_id)
    if not execution or execution.get("state") not in exec_bridge.TERMINAL_ENGINE_STATES:
        raise HTTPException(409, "Feedback is accepted only after the BT Engine reaches a terminal state.")
    try:
        result = record_user_feedback_experience(
            item,
            execution,
            req.model_dump(mode="json"),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"Could not persist mission feedback: {exc}") from exc
    return {"ok": True, **result}


@app.post("/api/missions/{mission_id}/events")
async def mission_event(mission_id: str, event: ExecutionEvent):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    payload = event.model_dump()
    payload["mission_id"] = mission_id
    add_execution_event(mission_id, payload)
    await hub.broadcast(item["session_id"], {"type": "execution_event", "event": payload})
    return {"ok": True}


@app.post("/api/missions/{mission_id}/engine/validate")
async def mission_engine_validate(mission_id: str):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    try:
        resp = await bt_engine_validate(item.get("bt_xml") or "")
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(resp.raw_text, status_code=resp.status_code, media_type="application/json")


async def _execute_saved_mission(mission_id: str) -> dict[str, Any]:
    """Submit one persisted, validated mission and start background status polling."""
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    xml = item.get("bt_xml") or ""
    if not xml:
        raise HTTPException(409, "Mission has no BehaviorTree XML to execute.")
    try:
        resp = await bt_engine_execute(xml)
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    body = _engine_body_dict(resp.body)
    if resp.status_code != 202 or not body.get("run_id"):
        # Authentication and transport/protocol errors are not planning failures.
        # In particular, a 401 must never cause the agent to rewrite the tree.
        if resp.status_code == 401:
            raise HTTPException(
                401,
                {
                    "kind": "auth",
                    "error": body.get("error") or "missing or wrong token",
                    "message": "BT Engine authentication failed. Configure BT_ENGINE_TOKEN; do not replan the BehaviorTree.",
                },
            )
        if resp.status_code == 400:
            raise HTTPException(
                502,
                {
                    "kind": "agent_protocol",
                    "error": body.get("error") or resp.raw_text,
                    "message": "BT Engine rejected the request format. This is an agent integration error, not a robot-plan failure.",
                },
            )
        if resp.status_code >= 500:
            latest_status: dict[str, Any] | None = None
            try:
                latest = await bt_engine_status()
                if latest.ok and isinstance(latest.body, dict):
                    latest_status = latest.body
                    # The engine documentation says a 500 may leave a run in any
                    # state. Return the latest snapshot to the caller, but do not
                    # attach it to this mission automatically because /status may
                    # describe the previous run.
            except Exception:
                latest_status = None
            raise HTTPException(
                502,
                {
                    "kind": "engine_error",
                    "error": body.get("error") or resp.raw_text,
                    "message": "BT Engine returned an internal error. Check the latest engine status before replanning.",
                    "latest_status": latest_status,
                },
            )

        # 422 is the only response here that means the XML itself was rejected
        # before execution. Preserve it as repair/replan feedback for the LLM.
        if resp.status_code == 422:
            event = {
                "mission_id": mission_id,
                "source": "bt_engine",
                "status": "rejected",
                "mission_status": "rejected",
                "message": body.get("error") or resp.raw_text,
                "metadata": body,
            }
            add_execution_event(mission_id, event)
            await hub.broadcast(item["session_id"], {"type": "execution_event", "event": event})
            feedback = (
                "[BT_ENGINE_FEEDBACK]\nstate=validation_rejected\n"
                f"BT engine rejected the generated XML before execution: {body.get('error') or resp.raw_text}\n"
                "Repair the XML against the current /nodes contract."
            )
            add_message(item["session_id"], "tool", feedback)
            await hub.broadcast(
                item["session_id"],
                {
                    "type": "bt_engine_terminal",
                    "mission_id": mission_id,
                    "state": "rejected",
                    "report": event["message"],
                    "feedback": feedback,
                    "payload": body,
                },
            )
            raise HTTPException(422, body)

        raise HTTPException(resp.status_code, body)

    run_id = str(body["run_id"])
    initial = {
        "run_id": run_id,
        "state": "running",
        "running_leaves": [],
        "elapsed_s": 0.0,
        "notes": [],
        "last_leaf_failure": None,
        "trace": [],
        "trace_truncated": False,
        "preempted_previous": bool(body.get("preempted_previous", False)),
    }
    await _apply_engine_status(mission_id, initial, source="bt_engine")
    _remember_task(asyncio.create_task(_poll_engine_run(mission_id, run_id)))
    return {**body, "mission_id": mission_id, "poll_interval_s": settings.bt_engine_poll_interval}


async def _attach_auto_execution(
    candidate: dict[str, Any],
    *,
    allow: bool = True,
    skip_reason: str = "automatic execution disabled for this request",
) -> dict[str, Any]:
    """Attach stable dispatch metadata without hiding a successfully generated BT."""
    mission_id = candidate.get("mission_id")
    generated = bool((candidate.get("bt_generation") or {}).get("succeeded"))
    if not generated or not mission_id:
        candidate["auto_execution"] = {
            "enabled": bool(settings.bt_engine_auto_execute),
            "attempted": False,
            "started": False,
            "status": "SKIPPED",
            "mission_id": mission_id,
            "reason": "no validated BehaviorTree XML",
        }
        return candidate
    if not settings.bt_engine_auto_execute:
        candidate["auto_execution"] = {
            "enabled": False,
            "attempted": False,
            "started": False,
            "status": "SKIPPED",
            "mission_id": mission_id,
            "reason": "BT_ENGINE_AUTO_EXECUTE=0",
        }
        return candidate
    if not allow:
        candidate["auto_execution"] = {
            "enabled": True,
            "attempted": False,
            "started": False,
            "status": "SKIPPED",
            "mission_id": mission_id,
            "reason": skip_reason,
        }
        return candidate

    try:
        started = await _execute_saved_mission(str(mission_id))
    except HTTPException as exc:
        candidate["auto_execution"] = {
            "enabled": True,
            "attempted": True,
            "started": False,
            "status": "FAILED",
            "mission_id": mission_id,
            "reason": "BT Engine did not accept automatic execution",
            "error": {"http_status": exc.status_code, "detail": exc.detail},
        }
        return candidate
    except Exception as exc:
        candidate["auto_execution"] = {
            "enabled": True,
            "attempted": True,
            "started": False,
            "status": "FAILED",
            "mission_id": mission_id,
            "reason": "Unexpected automatic-execution integration error",
            "error": {"detail": str(exc)},
        }
        return candidate

    candidate["auto_execution"] = {
        "enabled": True,
        "attempted": True,
        "started": True,
        "status": "STARTED",
        "mission_id": mission_id,
        "run_id": str(started.get("run_id")),
        "engine_state": "running",
        "preempted_previous": bool(started.get("preempted_previous", False)),
        "poll_interval_s": started.get("poll_interval_s"),
        "reason": None,
    }
    return candidate


@app.post("/api/missions/{mission_id}/engine/execute", status_code=202)
async def mission_engine_execute(mission_id: str):
    """Diagnostic/manual compatibility endpoint; normal chat missions auto-execute."""
    return await _execute_saved_mission(mission_id)


@app.get("/api/missions/{mission_id}/engine/status")
async def mission_engine_status(mission_id: str, refresh: bool = True):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    current = get_execution_run(mission_id)
    if refresh and current and current.get("source") == "bt_engine" and current.get("run_id"):
        try:
            resp = await bt_engine_status(str(current["run_id"]))
            if resp.ok and isinstance(resp.body, dict):
                current = await _apply_engine_status(mission_id, resp.body, source="bt_engine")
        except BtEngineUnavailable:
            pass
    return {"mission_id": mission_id, "execution": current, "events": list_execution_events(mission_id)}


@app.post("/api/missions/{mission_id}/engine/cancel")
async def mission_engine_cancel(mission_id: str):
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    try:
        resp = await bt_engine_cancel()
    except BtEngineUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not resp.ok:
        raise HTTPException(resp.status_code, _engine_body_dict(resp.body))
    return resp.body


@app.post("/api/missions/{mission_id}/engine/replan")
async def mission_engine_replan(mission_id: str, req: ReplanRequest | None = None):
    req = req or ReplanRequest()
    item = get_mission(mission_id)
    if not item:
        raise HTTPException(404, "Mission not found")
    execution = get_execution_run(mission_id)
    if not execution or execution.get("state") not in {"failure", "error", "canceled"}:
        raise HTTPException(409, "A terminal BT-engine failure/error/cancel result is required before replanning.")

    session_id = item["session_id"]
    history = get_history(session_id, 16)
    feedback = execution.get("feedback_text") or exec_bridge.terminal_feedback(execution.get("latest_payload") or {})
    add_message(session_id, "user", f"[REPLAN_REQUEST] Replan mission {mission_id} using the latest BT-engine runtime feedback.")
    chat_req = ChatRequest(
        message=item["user_request"],
        session_id=session_id,
        pipeline_mode=req.pipeline_mode,
        world_state={
            "previous_mission_id": mission_id,
            "bt_engine_feedback": execution.get("latest_payload") or {},
            "bt_engine_feedback_text": feedback,
        },
    )
    result = await run_pipeline(req.pipeline_mode, chat_req, history)
    result = await _attach_auto_execution(result)
    add_message(session_id, "assistant", result.get("message", ""))
    return {"session_id": session_id, "pipeline_mode": req.pipeline_mode, "candidate": result, "replanned_from": mission_id}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await hub.connect(session_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(session_id, ws)


if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
