from __future__ import annotations

from typing import Any

from .store import (
    add_execution_event,
    add_message,
    get_execution_run,
    get_mission,
    upsert_execution_run,
)

TERMINAL_ENGINE_STATES = {"success", "failure", "canceled", "error"}
VALID_ENGINE_STATES = {"running", *TERMINAL_ENGINE_STATES}


def terminal_feedback(payload: dict[str, Any]) -> str:
    state = str(payload.get("state", "unknown")).lower()
    run_id = payload.get("run_id") or "unknown"
    failed = payload.get("last_leaf_failure") or {}
    failed_node = failed.get("node") or "unknown"
    failed_type = failed.get("type") or "unknown"
    notes = payload.get("notes") or []
    note_lines = []
    for note in notes:
        if isinstance(note, dict):
            node = note.get("node") or "node"
            message = note.get("message") or ""
            note_lines.append(f"- {node}: {message}".rstrip())
        elif note:
            note_lines.append(f"- {note}")

    if state == "success":
        return f"[BT_ENGINE_FEEDBACK]\nrun_id={run_id}\nstate=success\nThe behavior tree completed successfully."
    if state == "failure":
        reasons = "\n".join(note_lines) if note_lines else "- No textual note was reported."
        return (
            f"[BT_ENGINE_FEEDBACK]\nrun_id={run_id}\nstate=failure\n"
            f"Failed node: {failed_node} ({failed_type})\nReasons:\n{reasons}\n"
            "The mission-level Sequence stopped at this failed phase; downstream phases were not executed. "
            "Use this runtime feedback when replanning."
        )
    if state == "error":
        err = payload.get("error") or "Unknown BT-engine runtime exception."
        return (
            f"[BT_ENGINE_FEEDBACK]\nrun_id={run_id}\nstate=error\nEngine runtime error: {err}\n"
            "The robot was halted by the BT engine. Use this runtime feedback when replanning."
        )
    if state == "canceled":
        return (
            f"[BT_ENGINE_FEEDBACK]\nrun_id={run_id}\nstate=canceled\n"
            "The behavior tree was canceled/preempted before mission completion."
        )
    return f"[BT_ENGINE_FEEDBACK]\nrun_id={run_id}\nstate={state}"


def human_execution_report(payload: dict[str, Any]) -> str:
    state = str(payload.get("state", "unknown")).lower()
    if state == "success":
        return "BT Engine 回報：任務執行成功。"
    if state == "failure":
        failed = payload.get("last_leaf_failure") or {}
        node = failed.get("node") or failed.get("type") or "未知節點"
        notes = payload.get("notes") or []
        reasons = [str(x.get("message")) for x in notes if isinstance(x, dict) and x.get("message")]
        suffix = f" 原因：{'；'.join(reasons)}" if reasons else ""
        return f"BT Engine 回報：任務執行失敗，失敗節點為 {node}。後續 Sequence 階段不會繼續執行。{suffix}"
    if state == "error":
        return f"BT Engine 回報執行例外，機器人已停止：{payload.get('error') or 'unknown error'}"
    if state == "canceled":
        return "BT Engine 回報：目前任務已取消或被新的 BehaviorTree 取代。"
    running = payload.get("running_leaves") or []
    return f"BT Engine 執行中：{', '.join(map(str, running)) if running else '等待節點狀態'}"


def execution_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    failed = payload.get("last_leaf_failure") or {}
    return (
        payload.get("run_id"),
        payload.get("state"),
        tuple(payload.get("running_leaves") or []),
        failed.get("node"),
        failed.get("to"),
        payload.get("error"),
    )


def normalize_engine_status(mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    state = str(payload.get("state", "")).lower()
    if state not in VALID_ENGINE_STATES:
        raise ValueError(f"Invalid BT-engine state {state!r}; expected one of {sorted(VALID_ENGINE_STATES)}")
    out = dict(payload)
    out["state"] = state
    out.setdefault("run_id", f"unknown-{mission_id[:8]}")
    out.setdefault("running_leaves", [])
    out.setdefault("notes", [])
    out.setdefault("last_leaf_failure", None)
    out.setdefault("trace", [])
    out.setdefault("trace_truncated", False)
    return out


def persist_engine_status(mission_id: str, payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    item = get_mission(mission_id)
    if not item:
        raise KeyError("Mission not found")
    payload = normalize_engine_status(mission_id, payload)
    state = payload["state"]
    previous = get_execution_run(mission_id)
    feedback = terminal_feedback(payload) if state in TERMINAL_ENGINE_STATES else None

    upsert_execution_run(
        mission_id,
        str(payload.get("run_id")) if payload.get("run_id") is not None else None,
        source,
        state,
        payload,
        feedback,
    )

    changed = previous is None or execution_signature(previous.get("latest_payload") or {}) != execution_signature(payload)
    event = None
    if changed:
        event = {
            "mission_id": mission_id,
            "source": source,
            "run_id": payload.get("run_id"),
            "status": state,
            "mission_status": state,
            "node_name": ((payload.get("last_leaf_failure") or {}).get("node") or ((payload.get("running_leaves") or [None])[0])),
            "node_type": (payload.get("last_leaf_failure") or {}).get("type"),
            "message": human_execution_report(payload),
            "metadata": payload,
        }
        add_execution_event(mission_id, event)

    previous_terminal = bool(
        previous
        and previous.get("run_id") == payload.get("run_id")
        and previous.get("state") == state
        and previous.get("feedback_text")
    )
    terminal_new = state in TERMINAL_ENGINE_STATES and not previous_terminal
    if terminal_new:
        add_message(item["session_id"], "tool", feedback or "")

    return {
        "mission": item,
        "execution": get_execution_run(mission_id) or {},
        "payload": payload,
        "event": event,
        "changed": changed,
        "terminal_new": terminal_new,
        "feedback": feedback,
        "report": human_execution_report(payload),
    }
