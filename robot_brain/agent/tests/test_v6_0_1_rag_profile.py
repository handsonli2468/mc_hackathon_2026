from __future__ import annotations

from agent.app.bootstrap import bootstrap_runtime
from agent.app.rag import analyze_plan_rag_utilization, retrieve_context, sync_rag_knowledge
from agent.app.schemas import MissionRequirements, TaskPlanIR
from agent.tests.run_timing_profile import DEFAULT_DIVERSE_MESSAGES, choose_messages


def test_scene_rag_rejects_generic_false_positive_for_blue_box():
    bootstrap_runtime()
    sync_rag_knowledge()
    req = MissionRequirements.model_validate(
        {
            "request_kind": "MISSION",
            "success_conditions": ["blue box is held"],
            "required_capabilities": ["locate_object", "navigate_to_object", "grasp_object"],
            "retrieval_sketch": {
                "semantic_concepts": ["visual object search", "open-vocabulary grounding"],
                "target_descriptions": ["blue box", "rectangular container", "blue-colored object"],
                "skill_queries": ["visual search for colored objects", "grasping unknown objects"],
                "pattern_queries": ["search locate grasp mission pattern"],
                "scene_queries": [
                    "indoor room environments",
                    "tables shelves and floor search regions",
                    "cluttered vs clear workspaces",
                ],
                "experience_queries": ["prior successful visual searches"],
            },
        }
    ).model_dump(mode="json")
    ctx = retrieve_context("find the blue box and grab it", req)
    assert ctx["scene"] == []
    assert ctx["locations"] == {}
    assert "pattern:track_dynamic_target" not in {d["id"] for d in ctx["patterns"]}


def test_rag_utilization_reports_integration_trust_for_uncalibrated_location():
    bootstrap_runtime()
    sync_rag_knowledge()
    req = MissionRequirements.model_validate(
        {
            "request_kind": "MISSION",
            "success_conditions": ["marker is at marker station"],
            "required_capabilities": ["locate_object", "navigate_to_object", "grasp_object", "release_object"],
            "retrieval_sketch": {
                "target_descriptions": ["marker", "marker station"],
                "skill_queries": ["find marker navigate grasp release"],
                "pattern_queries": ["scene prior then local search"],
                "scene_queries": ["marker storage station whiteboard marker"],
                "experience_queries": [],
            },
        }
    ).model_dump(mode="json")
    ctx = retrieve_context("pick up the marker and return it to the marker station", req)
    assert "marker_station" in ctx["locations"]
    ir = TaskPlanIR.model_validate(
        {
            "mission_status": "EXECUTABLE",
            "goal": {"description": "return marker", "success_conditions": ["done"]},
            "termination_policy": {"success_conditions": ["done"]},
            "phases": [
                {
                    "id": "go_station",
                    "objective": "go station",
                    "nominal_action": {"skill": "NavigateToPoint", "arguments": {"x": 1.2, "y": -0.8, "speed": "normal"}},
                    "failure_modes": [],
                },
            ],
        }
    )
    usage = analyze_plan_rag_utilization(ir, ctx, location_policy={"bindings": [{
        "location_id": "marker_station", "skill": "NavigateToPoint",
        "arguments": {"x": 1.2, "y": -0.8, "speed": "normal"},
    }]})
    loc = usage["scene"]["locations"]["marker_station"]
    assert loc["referenced_in_plan"] is True
    assert loc["calibrated"] is False
    assert loc["actionable"] is True
    assert "trust mode" in loc["reason"]


def test_timing_profiler_uses_diverse_inputs_by_default():
    messages, mode = choose_messages(5, None, None)
    assert mode == "diverse"
    assert len(messages) == 5
    assert len(set(messages)) == 5
    assert messages == DEFAULT_DIVERSE_MESSAGES


def test_timing_profiler_explicit_message_still_repeats():
    messages, mode = choose_messages(3, "find the bottle", None)
    assert mode == "single_explicit"
    assert messages == ["find the bottle"] * 3


def test_ir_validator_rejects_out_of_range_setgripper_position():
    from agent.app.ir_validator import validate_task_plan_ir

    ir = TaskPlanIR.model_validate(
        {
            "mission_status": "EXECUTABLE",
            "goal": {"description": "close gripper", "success_conditions": ["command complete"]},
            "termination_policy": {"success_conditions": ["command complete"]},
            "phases": [
                {
                    "id": "grip",
                    "objective": "close gripper",
                    "nominal_action": {"skill": "SetGripper", "arguments": {"position": 101}},
                    "failure_modes": [
                        {
                            "failure": "EXECUTION_FAILED",
                            "classification": "TRANSIENT",
                            "recovery": [],
                            "max_attempts": 2,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                }
            ],
        }
    )
    result = validate_task_plan_ir(ir)
    assert result["valid"] is False
    assert any("maximum 100" in x for x in result["errors"])
