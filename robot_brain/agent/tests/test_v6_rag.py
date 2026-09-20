from __future__ import annotations

from agent.app.bootstrap import bootstrap_runtime
from agent.app.rag import (
    capability_ontology_for_prompt,
    init_rag_db,
    record_execution_experience,
    retrieve_context,
    status,
    sync_rag_knowledge,
    validate_plan_closed_set,
    validate_bt_xml_closed_set,
)
from agent.app.schemas import MissionRequirements, TaskPlanIR


def _ready_requirements() -> dict:
    return MissionRequirements.model_validate(
        {
            "request_kind": "MISSION",
            "success_conditions": ["blue box is held"],
            "required_capabilities": [
                "locate_object",
                "navigate_to_object",
                "verify_object_visible",
            ],
            "retrieval_sketch": {
                "semantic_concepts": ["visual search", "blue box", "approach"],
                "target_descriptions": ["blue box"],
                "skill_queries": ["visually ground target navigate to target and verify"],
                "pattern_queries": ["bounded visual search action verification fail stop"],
                "scene_queries": ["blue box"],
                "experience_queries": ["blue box approach"],
            },
        }
    ).model_dump(mode="json")


def test_rag_bootstrap_and_skill_coverage():
    bootstrap_runtime()
    init_rag_db()
    counts = sync_rag_knowledge()
    assert counts["skills"] == 7
    assert counts["patterns"] >= 6
    assert counts["scene"] >= 4
    ctx = retrieve_context("find the blue box and grab it", _ready_requirements())
    assert ctx["missing_capabilities"] == []
    ids = set(ctx["allowed_skill_ids"])
    assert {"VisualizeObject", "NavigateToDetectedObject", "IsObjectFound"} <= ids
    # Deterministic dependencies needed by compiler/hardening are included without
    # exposing the full skill registry.
    assert "IsObjectFound" in ids
    assert "Patrol" in ids
    assert len(ids) < status()["documents"]["skill"]
    # Generic open-world object tasks must not pull unrelated meeting-room scene priors.
    assert ctx["scene"] == []
    assert ctx["locations"] == {}


def test_capability_ontology_hides_detailed_ports():
    text = capability_ontology_for_prompt()
    assert "capability_vocabulary" in text
    assert "recommended_verification" not in text
    assert "inputs:" not in text


def test_scene_rag_returns_meeting_prior_and_exact_location_registry():
    sync_rag_knowledge()
    req = _ready_requirements()
    req["retrieval_sketch"]["scene_queries"] = ["whiteboard marker stationery holder marker station"]
    ctx = retrieve_context("bring me a whiteboard marker", req)
    ids = {d["id"] for d in ctx["scene"]}
    assert "scene:whiteboard_marker_prior" in ids
    assert "marker_station" in ctx["locations"]
    assert "presentation_area" not in ctx["locations"]
    assert ctx["locations"]["marker_station"]["frame_id"] == "map"
    assert ctx["locations"]["marker_station"]["metadata"].get("calibrated") is False


def test_experience_is_written_and_retrievable():
    sync_rag_knowledge()
    mission = {
        "mission_id": "m-v6-test",
        "user_request": "find the blue box and grab it",
        "artifacts": {
            "task_plan_ir": {
                "phases": [
                    {"nominal_action": {"skill": "VisualizeObject", "arguments": {"object_name": "blue box"}}},
                    {"nominal_action": {"skill": "NavigateToDetectedObject", "arguments": {"speed": "slow"}}},
                ]
            },
            "rag_context": {"allowed_skill_ids": ["VisualizeObject", "NavigateToDetectedObject"], "patterns": [], "scene": []},
        },
    }
    saved = record_execution_experience(
        mission,
        {
            "run_id": "run-v6-test",
            "state": "failure",
            "elapsed_s": 12.3,
            "last_leaf_failure": {"node": "find_blue_box", "type": "VisualizeObject"},
            "notes": [{"node": "find_blue_box", "message": "blue box not found"}],
        },
    )
    assert saved and saved["outcome"] == "failure"
    req = _ready_requirements()
    req["retrieval_sketch"]["experience_queries"] = ["blue box not found visual search grasp"]
    ctx = retrieve_context("find blue box and grab it", req)
    assert any(x["id"] == saved["experience_id"] for x in ctx["experiences"])


def test_closed_set_validation_rejects_nonretrieved_skill():
    ir = TaskPlanIR.model_validate(
        {
            "mission_status": "EXECUTABLE",
            "goal": {"description": "test", "success_conditions": ["done"]},
            "termination_policy": {"success_conditions": ["done"]},
            "phases": [
                {
                    "id": "p",
                    "objective": "patrol",
                    "nominal_action": {"skill": "Patrol", "arguments": {}},
                    "failure_modes": [],
                }
            ],
        }
    )
    result = validate_plan_closed_set(ir, {"allowed_skill_ids": ["VisualizeObject"]})
    assert result["valid"] is False
    assert "Patrol" in result["errors"][0]


def test_bt_xml_closed_set_rejects_valid_but_nonretrieved_skill():
    xml = '<root BTCPP_format="4" main_tree_to_execute="main"><BehaviorTree ID="main"><Sequence name="s"><Patrol name="p"/></Sequence></BehaviorTree></root>'
    result = validate_bt_xml_closed_set(xml, {"allowed_skill_ids": ["VisualizeObject"]})
    assert result["valid"] is False
    assert "Patrol" in result["errors"][0]
