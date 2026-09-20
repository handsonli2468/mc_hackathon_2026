from __future__ import annotations

import json
import threading
import uuid
from contextvars import ContextVar, Token
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .settings import settings

_TRACE: ContextVar[dict[str, Any] | None] = ContextVar("robot_brain_trace", default=None)
_FILE_LOCK = threading.Lock()


def start_trace(metadata: dict[str, Any] | None = None) -> Token:
    trace = {
        "trace_id": str(uuid.uuid4()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "metadata": dict(metadata or {}),
        "stages": [],
        "model_calls": [],
    }
    return _TRACE.set(trace)


def reset_trace(token: Token) -> None:
    _TRACE.reset(token)


def current_trace() -> dict[str, Any] | None:
    return _TRACE.get()


def record_stage(name: str, elapsed_sec: float, details: dict[str, Any] | None = None) -> None:
    trace = _TRACE.get()
    if trace is None:
        return
    item: dict[str, Any] = {
        "name": name,
        "elapsed_sec": round(float(elapsed_sec), 6),
    }
    if details:
        item["details"] = details
    trace["stages"].append(item)


def record_model_call(
    *,
    role: str,
    operation: str,
    model: str,
    elapsed_sec: float,
    prompt_chars: int,
    response_chars: int = 0,
    usage: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    reasoning_chars: int | None = None,
    status: str = "ok",
    http_status: int | None = None,
    error: str | None = None,
    thinking_token_budget: int | None = None,
) -> None:
    trace = _TRACE.get()
    if trace is None:
        return
    item: dict[str, Any] = {
        "role": role,
        "operation": operation,
        "model": model,
        "status": status,
        "elapsed_sec": round(float(elapsed_sec), 6),
        "prompt_chars": int(prompt_chars),
        "response_chars": int(response_chars),
    }
    if usage:
        safe_usage: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                safe_usage[key] = int(value)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            # Keep counters only. Never store model reasoning text in telemetry.
            safe_usage["completion_tokens_details"] = {
                k: int(v) for k, v in details.items() if isinstance(v, (int, float))
            }
        if safe_usage:
            item["usage"] = safe_usage
    if finish_reason is not None:
        item["finish_reason"] = finish_reason
    if reasoning_chars is not None:
        item["reasoning_chars"] = int(reasoning_chars)
    if http_status is not None:
        item["http_status"] = int(http_status)
    if error:
        item["error"] = str(error)[:2000]
    if thinking_token_budget is not None:
        item["thinking_token_budget"] = int(thinking_token_budget)
    trace["model_calls"].append(item)


def _aggregate_model_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for call in calls:
        role = str(call.get("role") or "unknown")
        bucket = result.setdefault(
            role,
            {
                "calls": 0,
                "elapsed_sec": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "reasoning_chars": 0,
                "reasoning_tokens": 0,
                "requested_thinking_budget_tokens": 0,
                "budgeted_calls": 0,
                "errors": 0,
            },
        )
        bucket["calls"] += 1
        bucket["elapsed_sec"] += float(call.get("elapsed_sec", 0.0) or 0.0)
        usage = call.get("usage") or {}
        bucket["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        bucket["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        bucket["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        bucket["reasoning_chars"] += int(call.get("reasoning_chars", 0) or 0)
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            bucket["reasoning_tokens"] += int(details.get("reasoning_tokens", 0) or 0)
        if call.get("thinking_token_budget") is not None:
            bucket["budgeted_calls"] += 1
            bucket["requested_thinking_budget_tokens"] += int(call.get("thinking_token_budget") or 0)
        if call.get("status") != "ok":
            bucket["errors"] += 1
    for bucket in result.values():
        bucket["elapsed_sec"] = round(bucket["elapsed_sec"], 6)
    return result


def _aggregate_stages(stages: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage in stages:
        name = str(stage.get("name") or "unknown")
        bucket = result.setdefault(name, {"calls": 0, "elapsed_sec": 0.0})
        bucket["calls"] += 1
        bucket["elapsed_sec"] += float(stage.get("elapsed_sec", 0.0) or 0.0)
    for bucket in result.values():
        bucket["elapsed_sec"] = round(bucket["elapsed_sec"], 6)
    return result


def snapshot_trace(total_elapsed_sec: float | None = None) -> dict[str, Any]:
    trace = deepcopy(_TRACE.get() or {})
    stages = trace.get("stages", []) or []
    calls = trace.get("model_calls", []) or []
    if total_elapsed_sec is not None:
        trace["total_elapsed_sec"] = round(float(total_elapsed_sec), 6)
    trace["stage_summary"] = _aggregate_stages(stages)
    trace["model_summary"] = _aggregate_model_calls(calls)
    trace["model_elapsed_sec"] = round(sum(float(x.get("elapsed_sec", 0.0) or 0.0) for x in calls), 6)
    if total_elapsed_sec is not None:
        trace["non_model_elapsed_sec_estimate"] = round(
            max(0.0, float(total_elapsed_sec) - float(trace["model_elapsed_sec"])), 6
        )
    return trace


def persist_trace(trace: dict[str, Any], extra: dict[str, Any] | None = None) -> Path:
    """Append one timing trace to the persistent Manta workspace.

    This is deliberately observational only: it does not alter model parameters,
    retry counts, thinking mode, or compiler behavior.
    """
    root = settings.runtime_root / "evaluations" / "timing"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "traces.jsonl"
    payload = deepcopy(trace)
    if extra:
        payload["result"] = dict(extra)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _FILE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def perf_start() -> float:
    return perf_counter()
