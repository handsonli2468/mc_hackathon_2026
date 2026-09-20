from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent.app import bt_engine_client
from agent.app.bt_xml import validate_bt_xml
from agent.app.compact_plan import enrich_compact_plan, normalize_compact_plan
from agent.app.conversation_grounding import normalize_requirement_grounding_assumptions
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import (
    CompactSemanticPlan,
    MissionRequirements,
    RequestKind,
    RequirementHint,
    RequiredUserInformation,
)
from agent.tests.test_v5_3_formal_bt_engine import BUILTINS, FORMAL

ROOT = Path(__file__).resolve().parents[2]


def _compact(steps):
    return CompactSemanticPlan.model_validate({
        "schema_version": "1.0",
        "goal_description": "Find and approach the blue box.",
        "success_conditions": ["blue box visible and reachable"],
        "targets": [{"id": "box", "role": "object", "object_name": "blue box", "grounding": "VISUAL"}],
        "steps": steps,
    })


def _requirements():
    return MissionRequirements(
        request_kind=RequestKind.MISSION,
        status_hint=RequirementHint.READY,
        success_conditions=["blue box visible and reachable"],
        required_capabilities=["locate_object", "navigate_to_object"],
    )


def test_compact_redundant_condition_is_dropped_without_full_planner_escalation():
    plan = _compact([
        {"id": "look", "action": "VisualizeObject", "arguments": {"object_name": "blue box"}},
        {"id": "verify", "action": "IsObjectFound", "arguments": {"object_name": "blue box"}},
        {"id": "go", "action": "NavigateToDetectedObject", "arguments": {"speed": "normal"}},
    ])
    normalized = normalize_compact_plan(plan, FORMAL)
    assert normalized["changed"] is True
    assert [s.action for s in normalized["plan"].steps] == ["VisualizeObject", "NavigateToDetectedObject"]
    assert any("IsObjectFound" in x for x in normalized["changes"])
    ir = enrich_compact_plan(normalized["plan"], _requirements(), FORMAL)
    assert [p.nominal_action.skill for p in ir.phases] == ["VisualizeObject", "NavigateToDetectedObject"]
    assert ir.phases[0].verification[0].condition == "IsObjectFound"
    assert ir.phases[-1].verification == []


def test_compact_unrelated_condition_is_not_silently_dropped():
    plan = _compact([
        {"id": "look", "action": "VisualizeObject", "arguments": {"object_name": "blue box"}},
        {"id": "wrong", "action": "IsObjectFound", "arguments": {"object_name": "different object"}},
    ])
    with pytest.raises(ValueError):
        normalize_compact_plan(plan, FORMAL)


def test_navigate_speed_enum_is_validated_locally():
    good = '''<root BTCPP_format="4" main_tree_to_execute="MainTree"><BehaviorTree ID="MainTree"><Sequence name="mission"><Sequence name="search"><VisualizeObject name="look" object_name="blue box"/><Timeout name="search_timeout" msec="60000"><ReactiveFallback name="search_monitor"><IsObjectFound name="found" object_name="blue box"/><Patrol name="patrol"/></ReactiveFallback></Timeout></Sequence><NavigateToDetectedObject name="go" speed="slow"/></Sequence></BehaviorTree></root>'''
    bad = good.replace('speed="slow"', 'speed="medium"')
    assert validate_bt_xml(good, skill_registry_as_bt_nodes(FORMAL), BUILTINS)["valid"] is True
    invalid = validate_bt_xml(bad, skill_registry_as_bt_nodes(FORMAL), BUILTINS)
    assert invalid["valid"] is False
    assert any("allowed values" in e or "slow" in e for e in invalid["errors"])


def test_delivery_clarification_prioritizes_recipient_not_searchable_object_location():
    req = MissionRequirements(
        request_kind=RequestKind.MISSION,
        status_hint=RequirementHint.NEED_MORE_INFO,
        message="Please specify the location of the baseball or confirm visual search.",
        success_conditions=["baseball delivered"],
        required_capabilities=["locate_object", "navigate_to_object"],
        required_user_information=[
            RequiredUserInformation(field="object_location", question="Where is the baseball, or should I search visually?")
        ],
    )
    result = normalize_requirement_grounding_assumptions(req, {}, "bring the baseball to me")
    out = result["requirements"]
    assert out.status_hint == RequirementHint.NEED_MORE_INFO
    assert [x.field for x in out.required_user_information] == ["recipient_grounding"]
    assert "where are you" in out.required_user_information[0].question.lower()
    assert any("open-vocabulary" in x for x in result["clarification_changes"])


def test_bt_engine_token_header_is_sent_to_protected_calls(monkeypatch):
    token = "secret-test-token"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            return self.headers.get("X-BT-Token") == token

        def do_GET(self):
            if self.path == "/health":
                return self._json(200, {"ok": True})
            if not self._authorized():
                return self._json(401, {"ok": False, "error": "missing or wrong token"})
            if self.path == "/runs":
                return self._json(200, [])
            if self.path == "/nodes?builtin=0":
                body = b'<root BTCPP_format="4"><TreeNodesModel/></root>'
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/status/run-1":
                return self._json(200, {"run_id": "run-1", "state": "success", "running_leaves": [], "notes": [], "trace": [], "elapsed_s": 1.0})
            return self._json(404, {"ok": False, "error": "no such endpoint"})

        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"ok": False, "error": "missing or wrong token"})
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            if self.path == "/validate":
                return self._json(200, {"ok": True})
            if self.path == "/execute":
                return self._json(202, {"ok": True, "run_id": "run-1", "preempted_previous": False})
            if self.path == "/cancel":
                return self._json(200, {"ok": True, "was_running": True})
            return self._json(404, {"ok": False, "error": "no such endpoint"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(bt_engine_client, "settings", SimpleNamespace(
            bt_engine_enabled=True,
            bt_engine_url=f"http://127.0.0.1:{server.server_port}",
            bt_engine_request_timeout=2.0,
            bt_engine_health_timeout=2.0,
            bt_engine_token=token,
        ))
        assert asyncio.run(bt_engine_client.health()).status_code == 200
        assert asyncio.run(bt_engine_client.runs()).status_code == 200
        assert asyncio.run(bt_engine_client.nodes(include_builtin=False)).status_code == 200
        assert asyncio.run(bt_engine_client.validate("<root/>")).status_code == 200
        assert asyncio.run(bt_engine_client.execute("<root/>")).status_code == 202
        assert asyncio.run(bt_engine_client.status("run-1")).body["state"] == "success"
        assert asyncio.run(bt_engine_client.cancel()).status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_execution_ui_is_real_engine_dashboard_without_simulator_controls():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
    joined = (html + js).lower()
    assert "simulated engine status" not in joined
    assert "enginesim" not in joined
    assert 'id="enginephaseprogress"' in html.lower()
    assert 'id="enginetracetimeline"' in html.lower()
    assert 'id="enginerunningleaves"' in html.lower()
    assert ".phase-card" in css
    assert ".trace-row" in css
    assert "#messageInput{min-height:48px;max-height:108px;overflow-y:auto" in css
