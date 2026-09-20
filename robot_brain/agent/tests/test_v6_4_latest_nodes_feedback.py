from __future__ import annotations

import uuid
from pathlib import Path

import yaml

from agent.app.bootstrap import bootstrap_runtime
from agent.app.bt_engine_contract import build_merged_skill_registry, parse_bt_engine_tree_nodes_model
from agent.app.bt_xml import validate_bt_xml
from agent.app.ir_validator import validate_task_plan_ir
from agent.app.mission_semantics import validate_goal_effect_closure
from agent.app.rag import get_user_feedback, record_user_feedback_experience
from agent.app.schemas import MissionRequirements, TaskPlanIR
from agent.app.settings import settings
from agent.app.registry import skill_registry_as_bt_nodes


ROOT = Path(__file__).resolve().parents[1]


def _registry():
    return yaml.safe_load((ROOT / "defaults" / "skill_registry.yaml").read_text(encoding="utf-8"))


def test_latest_tree_nodes_and_reviewed_semantics_are_complete(monkeypatch):
    xml = (ROOT / "defaults" / "bt_engine_nodes.xml").read_text(encoding="utf-8")
    overlay = yaml.safe_load((ROOT / "defaults" / "bt_engine_semantic_overlay.yaml").read_text(encoding="utf-8"))
    policy = yaml.safe_load((ROOT / "defaults" / "bt_skill_policy.yaml").read_text(encoding="utf-8"))
    import agent.app.bt_engine_contract as contract
    monkeypatch.setattr(contract, "load_bt_skill_policy", lambda: policy)
    engine = parse_bt_engine_tree_nodes_model(xml)
    registry, report = build_merged_skill_registry(engine, overlay)

    assert set(engine) == {
        "IsObjectFound", "NavigateToDetectedObject", "NavigateToPoint", "Patrol",
        "RotateInPlace", "SetGripper", "VisualizeObject",
    }
    assert report["semantic_complete"] is True
    assert {x["id"] for x in registry["skills"]} == {
        "IsObjectFound", "NavigateToDetectedObject", "NavigateToPoint", "Patrol",
        "RotateInPlace", "SetGripper", "VisualizeObject",
    }
    visualize = next(x for x in registry["skills"] if x["id"] == "VisualizeObject")
    assert visualize["continuous_search_trigger"] is True
    patrol = next(x for x in registry["skills"] if x["id"] == "Patrol")
    assert patrol["search_policy_role"] == "continuous_patrol"


def test_setgripper_bounds_and_argument_dependent_goal_effects():
    registry = _registry()
    req = MissionRequirements.model_validate({
        "request_kind": "MISSION",
        "success_conditions": ["object moved"],
        "task_semantics": {"operation": "RELOCATE_OBJECT"},
    })

    def plan(acquire_position: int, release_position: int):
        return TaskPlanIR.model_validate({
            "mission_status": "EXECUTABLE",
            "goal": {"description": "move object", "success_conditions": ["done"]},
            "termination_policy": {"success_conditions": ["done"], "max_mission_duration_sec": 300},
            "phases": [
                {"id": "grasp", "objective": "grasp", "nominal_action": {"skill": "SetGripper", "arguments": {"position": acquire_position}}},
                {"id": "move", "objective": "move", "nominal_action": {"skill": "NavigateToPoint", "arguments": {"x": 1.0, "y": 2.0, "speed": "slow"}}},
                {"id": "release", "objective": "release", "nominal_action": {"skill": "SetGripper", "arguments": {"position": release_position}}},
            ],
        })

    assert validate_goal_effect_closure(plan(55, 0), req, registry)["valid"] is True
    invalid_effects = validate_goal_effect_closure(plan(0, 55), req, registry)
    assert invalid_effects["valid"] is False

    out_of_range = plan(101, 0)
    validation = validate_task_plan_ir(out_of_range, registry)
    assert validation["valid"] is False
    assert any("maximum 100" in x for x in validation["errors"])


def test_gripper_prompts_define_open_first_and_adaptive_fifty_baseline():
    for relative in [
        "prompts/compact_planner.md",
        "prompts/planner.md",
        "prompts/direct_bt.md",
    ]:
        text = (ROOT / "defaults" / relative).read_text(encoding="utf-8")
        assert "position=0 before approach/grasp" in text
        assert "start at position=50" in text

    registry = _registry()
    gripper = next(x for x in registry["skills"] if x["id"] == "SetGripper")
    assert "start at 50" in gripper["description"]
    assert "high-rated Experience RAG" in gripper["description"]


def test_obsolete_direct_visualize_then_condition_shape_is_rejected():
    registry = _registry()
    builtins = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))
    xml = '<root BTCPP_format="4" main_tree_to_execute="MainTree"><BehaviorTree ID="MainTree"><Sequence name="mission"><VisualizeObject name="look" object_name="bottle"/><IsObjectFound name="found" object_name="bottle"/></Sequence></BehaviorTree></root>'
    result = validate_bt_xml(xml, skill_registry_as_bt_nodes(registry), builtins)
    assert result["valid"] is False
    assert any("Timeout(ReactiveFallback" in error for error in result["errors"])


def test_human_feedback_is_json_and_experience_rag_record():
    bootstrap_runtime()
    mission_id = f"feedback-{uuid.uuid4()}"
    mission = {
        "mission_id": mission_id,
        "session_id": "session-test",
        "user_request": "pick up the bottle and move slowly",
        "artifacts": {"task_plan_ir": {"phases": [
            {"nominal_action": {"skill": "NavigateToDetectedObject", "arguments": {"speed": "normal"}}},
            {"nominal_action": {"skill": "SetGripper", "arguments": {"position": 60}}},
        ]}},
    }
    execution = {"run_id": "run-feedback", "state": "success"}
    saved = record_user_feedback_experience(mission, execution, {
        "rating": 5,
        "comment": "Use a lighter grip and approach slowly.",
        "parameters": {
            "set_gripper_position": 52,
            "navigate_to_detected_object_speed": "slow",
            "navigate_to_point_speed": "normal",
        },
    })

    assert Path(saved["json_path"]).exists()
    record = get_user_feedback(mission_id)
    assert record and record["rating"] == 5
    assert record["parameters"]["set_gripper_position"] == 52
    assert "SetGripper.position=52" in saved["summary"]
