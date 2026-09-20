from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml
from defusedxml import ElementTree as SafeET

from agent.app import store
from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.execution_bridge import persist_engine_status, terminal_feedback
from agent.app.phase_compiler import assemble_phase_subtrees, deterministic_phase_subtree
from agent.tests.test_v5_3_formal_bt_engine import BUILTINS, FORMAL, formal_ir

ROOT = Path(__file__).resolve().parents[2]


def _formal_xml() -> tuple[str, object]:
    ir = formal_ir()
    phase_subtrees = {p.id: deterministic_phase_subtree(p) for p in ir.phases}
    raw = assemble_phase_subtrees(ir, phase_subtrees, FORMAL, BUILTINS)
    normalized = normalize_compiled_bt_xml(raw, ir, FORMAL, BUILTINS)
    assert normalized["error"] is None
    return normalized["xml"], ir


def test_mission_sequence_explicitly_proves_fail_stop_semantics():
    xml, ir = _formal_xml()
    semantic = validate_bt_semantics(xml, ir, FORMAL)
    assert semantic["valid"], semantic["errors"]
    fail_stop = semantic["mission_fail_stop"]
    assert fail_stop["valid"] is True
    assert fail_stop["root"] == "Sequence"
    assert fail_stop["direct_children"] == len(ir.phases)
    assert fail_stop["guarantee"] == "stop_downstream_on_phase_failure"


def test_non_sequence_mission_root_is_rejected_as_not_fail_stop():
    xml, ir = _formal_xml()
    root = SafeET.fromstring(xml)
    tree = root.find("BehaviorTree")
    assert tree is not None
    mission = list(tree)[0]
    mission.tag = "Fallback"
    bad_xml = SafeET.tostring(root, encoding="unicode")
    semantic = validate_bt_semantics(bad_xml, ir, FORMAL)
    assert not semantic["valid"]
    assert any("Mission root must be <Sequence>" in e for e in semantic["errors"])


def test_terminal_failure_feedback_is_short_and_replan_ready():
    feedback = terminal_feedback({
        "run_id": "run-7",
        "state": "failure",
        "last_leaf_failure": {"node": "look", "type": "VisualizeObject"},
        "notes": [{"node": "look", "message": "'blue box' not found in view"}],
    })
    assert "[BT_ENGINE_FEEDBACK]" in feedback
    assert "Failed node: look (VisualizeObject)" in feedback
    assert "blue box" in feedback
    assert "downstream phases were not executed" in feedback


def test_engine_failure_uses_same_persistence_and_llm_history_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "settings", SimpleNamespace(state_root=tmp_path))
    store.init_db()
    mission_id = "mission-1"
    session_id = "session-1"
    store.save_mission(
        mission_id,
        session_id,
        "hybrid",
        "find the blue box and grab it",
        "SUCCESS",
        "<root BTCPP_format='4' main_tree_to_execute='Main'><BehaviorTree ID='Main'><Sequence name='mission'/></BehaviorTree></root>",
        {},
    )
    payload = {
        "run_id": "run-1",
        "state": "failure",
        "elapsed_s": 3.2,
        "running_leaves": [],
        "last_leaf_failure": {"node": "locate", "type": "VisualizeObject", "from": "RUNNING", "to": "FAILURE", "t": 3.2},
        "notes": [{"node": "locate", "message": "not found"}],
        "trace": [],
        "trace_truncated": False,
    }
    result = persist_engine_status(mission_id, payload, source="bt_engine")
    execution = result["execution"]
    assert execution["state"] == "failure"
    assert execution["source"] == "bt_engine"
    assert "BT_ENGINE_FEEDBACK" in (execution["feedback_text"] or "")
    events = store.list_execution_events(mission_id)
    assert events and events[-1]["status"] == "failure"
    history = store.get_history(session_id)
    assert history[-1]["role"] == "tool"
    assert "not found" in history[-1]["content"]


def test_frontend_locks_page_scroll_but_keeps_chat_and_input_scrollable():
    css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
    assert "html,body{height:100%;overflow:hidden}" in css
    assert ".chat-log{flex:1 1 auto;min-height:0;overflow-y:auto" in css
    assert "#messageInput{min-height:48px;max-height:108px;overflow-y:auto" in css


def test_interface_document_is_shipped_with_project():
    doc = ROOT / "docs" / "BT_ENGINE_INTERFACE.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "POST /execute" in text
    assert "GET /status/<run_id>" in text
    assert "last_leaf_failure" in text


def test_bt_engine_http_client_matches_team_protocol(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agent.app import bt_engine_client

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return
        def _json(self, code, payload):
            body=json.dumps(payload).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path == '/health': return self._json(200, {'ok': True})
            if self.path == '/nodes?builtin=0':
                body=b'<root BTCPP_format="4"><TreeNodesModel><Action ID="VisualizeObject"><input_port name="object_name" type="std::string"/></Action></TreeNodesModel></root>'
                self.send_response(200);self.send_header('Content-Type','application/xml');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if self.path == '/status/run-1': return self._json(200, {'run_id':'run-1','state':'failure','running_leaves':[],'notes':[],'last_leaf_failure':None,'trace':[],'trace_truncated':False,'elapsed_s':1.0})
            return self._json(404, {'ok':False,'error':'no such endpoint'})
        def do_POST(self):
            n=int(self.headers.get('Content-Length','0'));raw=self.rfile.read(n)
            if self.path == '/validate': return self._json(200, {'ok':True})
            if self.path == '/execute':
                assert b'"xml"' in raw
                return self._json(202, {'ok':True,'run_id':'run-1','preempted_previous':False})
            if self.path == '/cancel': return self._json(200, {'ok':True,'was_running':True})
            return self._json(404, {'ok':False,'error':'no such endpoint'})

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        monkeypatch.setattr(bt_engine_client,'settings',SimpleNamespace(
            bt_engine_enabled=True,
            bt_engine_url=f'http://127.0.0.1:{server.server_port}',
            bt_engine_request_timeout=2.0,
            bt_engine_health_timeout=2.0,
            bt_engine_token='',
        ))
        health=asyncio.run(bt_engine_client.health());assert health.status_code==200 and health.body['ok'] is True
        nodes=asyncio.run(bt_engine_client.nodes(include_builtin=False));assert 'TreeNodesModel' in nodes.raw_text
        valid=asyncio.run(bt_engine_client.validate('<root/>'));assert valid.status_code==200
        execute=asyncio.run(bt_engine_client.execute('<root/>'));assert execute.status_code==202 and execute.body['run_id']=='run-1'
        status=asyncio.run(bt_engine_client.status('run-1'));assert status.body['state']=='failure'
        cancel=asyncio.run(bt_engine_client.cancel());assert cancel.body['was_running'] is True
    finally:
        server.shutdown();server.server_close()
