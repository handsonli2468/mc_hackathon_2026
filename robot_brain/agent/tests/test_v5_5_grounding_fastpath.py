from __future__ import annotations

from types import SimpleNamespace

from agent.app.compact_plan import enrich_compact_plan
from agent.app.conversation_grounding import build_pending_grounding_context, infer_recipient_visual_query
from agent.app.grounding_policy import apply_grounding_policy
from agent.app.quality_gate import assess_plan_quality
from agent.app.schemas import CompactSemanticPlan, MissionRequirements
from agent.tests.test_v5_3_formal_bt_engine import FORMAL, formal_ir


def requirements() -> MissionRequirements:
    return MissionRequirements.model_validate({
        "request_kind": "MISSION",
        "status_hint": "READY",
        "success_conditions": ["baseball delivered to the user"],
        "required_capabilities": ["locate_object", "navigate_to_object", "acquire_object", "release_object"],
        "required_user_information": [],
        "assumptions": [],
    })


def test_relational_user_reply_becomes_visual_recipient_not_known_table():
    q = infer_recipient_visual_query("bring the baseball to me", "I am beside the table")
    assert q == "person standing beside the table"
    world, ctx = build_pending_grounding_context(
        {"original_request": "bring the baseball to me", "requirements": requirements().model_dump(mode="json")},
        "I am beside the table",
        {},
    )
    assert ctx["recipient"] == "user"
    assert ctx["recipient_grounding_mode"] == "VISUAL_QUERY"
    assert ctx["recipient_visual_query"] == "person standing beside the table"
    assert "known" not in str(world).lower()


def test_compact_plan_expands_to_full_ir_from_skill_manifest():
    compact = CompactSemanticPlan.model_validate({
        "goal_description": "Find the baseball and bring it to the user.",
        "success_conditions": ["baseball delivered to user"],
        "targets": [
            {"id": "ball", "role": "object", "object_name": "baseball", "grounding": "VISUAL"},
            {"id": "recipient", "role": "recipient", "object_name": "person standing beside the table", "grounding": "VISUAL"},
        ],
        "steps": [
            {"id": "find_ball", "action": "VisualizeObject", "arguments": {"object_name": "baseball"}},
            {"id": "go_ball", "action": "NavigateToDetectedObject", "arguments": {"speed": "slow"}},
            {"id": "acquire_ball", "action": "SetGripper", "arguments": {"position": 55}},
            {"id": "find_user", "action": "VisualizeObject", "arguments": {"object_name": "person standing beside the table"}},
            {"id": "go_user", "action": "NavigateToDetectedObject", "arguments": {"speed": "slow"}},
            {"id": "release_ball", "action": "SetGripper", "arguments": {"position": 0}},
        ],
    })
    ir = enrich_compact_plan(compact, requirements(), FORMAL)
    assert len(ir.phases) == 6
    assert ir.phases[0].verification[0].condition == "IsObjectFound"
    assert ir.phases[1].verification == []
    assert ir.phases[2].nominal_action.arguments == {"position": 55}
    assert ir.phases[3].verification[0].condition == "IsObjectFound"
    assert ir.phases[4].verification == []
    assert ir.phases[5].nominal_action.arguments == {"position": 0}
    assert ir.phases[0].failure_modes
    assert ir.phases[1].resource_requirements == ["BASE", "CAMERA"]


def test_grounding_policy_rewrites_landmark_recipient_and_inserts_perception():
    ir = formal_ir().model_copy(deep=True)
    acquire = ir.phases[1].model_copy(deep=True)
    acquire.id = "acquire_bottle"
    acquire.nominal_action.skill = "SetGripper"
    acquire.nominal_action.arguments = {"position": 55}
    acquire.verification = []
    ir.phases.append(acquire)
    # Latest-detection navigation has no object-name port. The grounding policy must
    # select the recipient immediately before this post-acquisition navigation.
    return_phase = ir.phases[1].model_copy(deep=True)
    return_phase.id = "return_to_user"
    return_phase.nominal_action.arguments = {"speed": "slow"}
    return_phase.verification = []
    ir.phases.append(return_phase)
    world = {"conversation_grounding": {"recipient_visual_query": "person standing beside the table"}}
    result = apply_grounding_policy(ir, world, FORMAL)
    out = result["ir"]
    assert result["changed"]
    ids = [p.id for p in out.phases]
    assert any(x.startswith("ground_return_to_user_recipient") for x in ids)
    final_nav = [p for p in out.phases if p.id == "return_to_user"][0]
    assert final_nav.nominal_action.arguments == {"speed": "slow"}
    assert any("intended recipient" in x for x in result["changes"])


def test_quality_gate_rejects_object_navigation_without_grounding():
    ir = formal_ir().model_copy(deep=True)
    ir.phases = ir.phases[1:]  # navigate/grasp only, no visual grounding
    gate = assess_plan_quality(ir, FORMAL, world_state={})
    assert not gate["pass"]
    assert any("observable perception grounding" in x for x in gate["critical_issues"])


def test_quality_gate_accepts_explicit_world_grounding():
    ir = formal_ir().model_copy(deep=True)
    ir.phases = ir.phases[1:]
    gate = assess_plan_quality(ir, FORMAL, world_state={"grounded_targets": ["bottle"]})
    assert gate["pass"], gate["critical_issues"]


def test_formal_registry_marks_locateanything_strings_open_vocabulary():
    skills = {s["id"]: s for s in FORMAL["skills"]}
    assert skills["VisualizeObject"]["object_name_semantics"]["open_vocabulary"] is True
    assert skills["VisualizeObject"]["object_name_semantics"]["relational_query"] is True
    assert skills["NavigateToDetectedObject"]["requires_grounded_target"] is True
    assert skills["NavigateToDetectedObject"]["consumes_latest_detected_object"] is True


def test_pending_mission_store_round_trip(tmp_path, monkeypatch):
    from agent.app import store
    monkeypatch.setattr(store, "settings", SimpleNamespace(state_root=tmp_path))
    store.init_db()
    payload = {"original_request": "bring the baseball to me", "requirements": requirements().model_dump(mode="json")}
    store.set_pending_mission("s1", payload)
    assert store.get_pending_mission("s1")["original_request"] == payload["original_request"]
    store.clear_pending_mission("s1")
    assert store.get_pending_mission("s1") is None


def test_requirement_normalizer_removes_unsupported_known_location_assumption():
    from agent.app.conversation_grounding import normalize_requirement_grounding_assumptions
    req = requirements().model_copy(deep=True)
    req.assumptions = ["The table is a known location for the robot.", "The baseball is present."]
    out = normalize_requirement_grounding_assumptions(req, world_state={})
    assert out["changed"] is True
    assert "The table is a known location for the robot." in out["removed_assumptions"]
    assert out["requirements"].assumptions == ["The baseball is present."]


def test_shipped_grounded_delivery_example_uses_relational_recipient_and_is_structurally_valid():
    from pathlib import Path
    import yaml
    from agent.app.bt_xml import validate_bt_xml
    from agent.app.registry import skill_registry_as_bt_nodes
    root = Path(__file__).resolve().parents[1]
    xml = (root.parent / "examples" / "grounded_delivery_bt.xml").read_text(encoding="utf-8")
    builtins = yaml.safe_load((root / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))
    assert 'object_name="person standing beside the table"' in xml
    assert 'object_name="table"' not in xml
    result = validate_bt_xml(xml, skill_registry_as_bt_nodes(FORMAL), builtins)
    assert result["valid"], result["errors"]
