from __future__ import annotations

import json, sqlite3
from datetime import datetime, timezone
from typing import Any
from .settings import settings


def _db() -> sqlite3.Connection:
    settings.state_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.state_root / "brain.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS missions (
          mission_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, pipeline_mode TEXT NOT NULL, user_request TEXT NOT NULL,
          status TEXT NOT NULL, bt_xml TEXT, artifacts_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_runs (
          mission_id TEXT PRIMARY KEY, run_id TEXT, source TEXT NOT NULL, state TEXT NOT NULL,
          latest_payload_json TEXT NOT NULL, feedback_text TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_state (
          session_id TEXT PRIMARY KEY, pending_json TEXT, updated_at TEXT NOT NULL
        );
        """)


def _now() -> str: return datetime.now(timezone.utc).isoformat()


def add_message(session_id: str, role: str, content: str) -> None:
    with _db() as c: c.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)", (session_id, role, content, _now()))


def get_history(session_id: str, limit: int = 16) -> list[dict[str, str]]:
    with _db() as c:
        rows = c.execute("SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_mission(mission_id: str, session_id: str, pipeline_mode: str, user_request: str, status: str, bt_xml: str | None, artifacts: dict[str, Any]) -> None:
    with _db() as c:
        c.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?)", (mission_id, session_id, pipeline_mode, user_request, status, bt_xml, json.dumps(artifacts, ensure_ascii=False), _now()))


def get_mission(mission_id: str) -> dict[str, Any] | None:
    with _db() as c: row = c.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
    if not row: return None
    d = dict(row); d["artifacts"] = json.loads(d.pop("artifacts_json")); return d


def add_execution_event(mission_id: str, payload: dict[str, Any]) -> None:
    with _db() as c: c.execute("INSERT INTO execution_events(mission_id,payload_json,created_at) VALUES(?,?,?)", (mission_id, json.dumps(payload, ensure_ascii=False), _now()))


def list_execution_events(mission_id: str) -> list[dict[str, Any]]:
    with _db() as c: rows = c.execute("SELECT payload_json,created_at FROM execution_events WHERE mission_id=? ORDER BY id", (mission_id,)).fetchall()
    out=[]
    for r in rows:
        x=json.loads(r["payload_json"]); x["created_at"]=r["created_at"]; out.append(x)
    return out


def upsert_execution_run(
    mission_id: str,
    run_id: str | None,
    source: str,
    state: str,
    payload: dict[str, Any],
    feedback_text: str | None = None,
) -> None:
    with _db() as c:
        c.execute(
            """
            INSERT INTO execution_runs(mission_id,run_id,source,state,latest_payload_json,feedback_text,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(mission_id) DO UPDATE SET
              run_id=excluded.run_id,
              source=excluded.source,
              state=excluded.state,
              latest_payload_json=excluded.latest_payload_json,
              feedback_text=COALESCE(excluded.feedback_text, execution_runs.feedback_text),
              updated_at=excluded.updated_at
            """,
            (mission_id, run_id, source, state, json.dumps(payload, ensure_ascii=False), feedback_text, _now()),
        )


def get_execution_run(mission_id: str) -> dict[str, Any] | None:
    with _db() as c:
        row = c.execute("SELECT * FROM execution_runs WHERE mission_id=?", (mission_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["latest_payload"] = json.loads(d.pop("latest_payload_json"))
    return d


def set_pending_mission(session_id: str, payload: dict[str, Any]) -> None:
    with _db() as c:
        c.execute(
            """
            INSERT INTO session_state(session_id,pending_json,updated_at) VALUES(?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET pending_json=excluded.pending_json, updated_at=excluded.updated_at
            """,
            (session_id, json.dumps(payload, ensure_ascii=False), _now()),
        )


def get_pending_mission(session_id: str) -> dict[str, Any] | None:
    with _db() as c:
        row = c.execute("SELECT pending_json FROM session_state WHERE session_id=?", (session_id,)).fetchone()
    if not row or not row["pending_json"]:
        return None
    try:
        value = json.loads(row["pending_json"])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def clear_pending_mission(session_id: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM session_state WHERE session_id=?", (session_id,))
