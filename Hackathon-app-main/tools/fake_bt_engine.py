#!/usr/bin/env python3
"""Offline stand-in for mc_main_nav's bt_engine HTTP API (stdlib only).

Mimics /execute, /status[/<id>], /cancel, /runs, /health closely enough to develop the app
without the robot. Every run walks through a few fake leaves; FAKE_FAIL=0.3 makes 30% fail.

    python3 tools/fake_bt_engine.py            # listens on :8080
    FAKE_PORT=8090 FAKE_FAIL=0.5 python3 tools/fake_bt_engine.py
"""
import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("FAKE_PORT", "8080"))
FAIL = float(os.environ.get("FAKE_FAIL", "0.3"))
STEP_S = float(os.environ.get("FAKE_STEP_S", "1.5"))

LEAVES = [
    ("start_looking", "VisualizeObject", "'cup' not found in view"),
    ("open_gripper", "SetGripper", "action server '/set_gripper' not available"),
    ("close_in", "TrackObject", "never saw the object"),
    ("close_gripper", "SetGripper", "gripper did not respond"),
]

lock = threading.Lock()
runs: list[dict] = []


def now() -> float:
    return time.time()


def event(run: dict, node: str, typ: str, frm: str, to: str) -> dict:
    ev = {"t": round(now() - run["started_at"], 3), "node": node, "type": typ, "from": frm, "to": to}
    run["trace"].append(ev)
    return ev


def worker(run: dict) -> None:
    fail_at = random.randrange(len(LEAVES)) if random.random() < FAIL else None
    with lock:
        event(run, "Sequence::1", "Sequence", "IDLE", "RUNNING")
    for i, (name, typ, reason) in enumerate(LEAVES):
        with lock:
            if run["state"] != "running":
                return
            event(run, name, typ, "IDLE", "RUNNING")
            run["running_leaves"] = [name]
        time.sleep(STEP_S)
        with lock:
            if run["state"] != "running":
                return
            if i == fail_at:
                run["last_leaf_failure"] = event(run, name, typ, "RUNNING", "FAILURE")
                event(run, "Sequence::1", "Sequence", "RUNNING", "FAILURE")
                run["notes"] = [{"node": name, "message": reason}]
                finish(run, "failure")
                return
            event(run, name, typ, "RUNNING", "SUCCESS")
    with lock:
        event(run, "Sequence::1", "Sequence", "RUNNING", "SUCCESS")
        finish(run, "success")


def finish(run: dict, state: str) -> None:
    run["state"] = state
    run["running_leaves"] = []
    run["finished_at"] = now()


def snapshot(run: dict) -> dict:
    out = {k: v for k, v in run.items() if k != "trace"}
    out["trace"] = run["trace"][-40:]
    out["trace_truncated"] = len(run["trace"]) > 40
    out["elapsed_s"] = round((run.get("finished_at") or now()) - run["started_at"], 2)
    return out


class Handler(BaseHTTPRequestHandler):
    def reply(self, code: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        with lock:
            if path == "/health":
                return self.reply(200, {"ok": True})
            if path == "/runs":
                return self.reply(200, [{"run_id": r["run_id"], "state": r["state"], "started_at": r["started_at"]} for r in runs[-20:]])
            m = re.fullmatch(r"/status(?:/([\w-]+))?", path)
            if m:
                run = next((r for r in runs if r["run_id"] == m[1]), None) if m[1] else (runs[-1] if runs else None)
                return self.reply(200, snapshot(run)) if run else self.reply(404, {"ok": False, "error": "no such run"})
        self.reply(404, {"ok": False, "error": "no such endpoint"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with lock:
            current = runs[-1] if runs and runs[-1]["state"] == "running" else None
            if path == "/cancel":
                if current:
                    finish(current, "canceled")
                return self.reply(200, {"ok": True, "was_running": current is not None})
            if path == "/execute":
                if not body.strip():
                    return self.reply(400, {"ok": False, "error": "empty body; send BT XML"})
                if current:
                    finish(current, "canceled")
                run = {"run_id": f"run-{len(runs) + 1}", "state": "running", "running_leaves": [], "notes": [],
                       "last_leaf_failure": None, "trace": [], "started_at": now()}
                runs.append(run)
                threading.Thread(target=worker, args=(run,), daemon=True).start()
                return self.reply(202, {"ok": True, "run_id": run["run_id"], "preempted_previous": current is not None})
        self.reply(404, {"ok": False, "error": "no such endpoint"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"fake bt_engine on :{PORT} (fail rate {FAIL})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
