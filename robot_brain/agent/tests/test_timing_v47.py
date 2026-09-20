from pathlib import Path

import yaml

from agent.app.compiler_adapter import build_invocation_plan
from agent.app.schemas import TaskPlanIR
from agent.app.telemetry import record_model_call, record_stage, reset_trace, snapshot_trace, start_trace

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))


def make_ir(symbolic: str | None = "some_planner_symbol") -> TaskPlanIR:
    locate_verify_args = {} if symbolic is None else {"object": symbolic}
    navigate_args = {} if symbolic is None else {"object": symbolic}
    near_args = {} if symbolic is None else {"object": symbolic}
    pick_args = {} if symbolic is None else {"object": symbolic}
    held_args = {} if symbolic is None else {"object": symbolic}
    return TaskPlanIR.model_validate(
        {
            "schema_version": "1.0",
            "mission_status": "EXECUTABLE",
            "goal": {"description": "find and hold bottle", "success_conditions": ["held"]},
            "required_capabilities": [],
            "missing_capabilities": [],
            "required_user_information": [],
            "assumptions": [],
            "global_constraints": [],
            "termination_policy": {
                "success_conditions": ["held"],
                "failure_conditions": ["failed"],
                "max_mission_duration_sec": 300,
                "on_unrecoverable_failure": "MISSION_FAILURE",
            },
            "phases": [
                {
                    "id": "locate_bottle",
                    "objective": "locate",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "bottle"}},
                    "verification": [{"condition": "IsObjectLocated", "arguments": locate_verify_args}],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "approach_bottle",
                    "objective": "approach",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "NavigateToObject", "arguments": navigate_args},
                    "verification": [{"condition": "IsNearObject", "arguments": near_args}],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "grasp_bottle",
                    "objective": "grasp",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "PickObject", "arguments": pick_args},
                    "verification": [{"condition": "IsObjectHeld", "arguments": held_args}],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
            ],
        }
    )


def test_free_form_opaque_symbols_bind_to_unique_typed_producer():
    invocations = build_invocation_plan(make_ir("tracked_bottle"), DEMO)
    values = [inv["ports"].get("object") for inv in invocations if "object" in inv["ports"]]
    assert values
    assert set(values) == {"{locate_bottle_object}"}


def test_missing_required_opaque_inputs_bind_to_unique_typed_producer():
    invocations = build_invocation_plan(make_ir(None), DEMO)
    values = [inv["ports"].get("object") for inv in invocations if inv["skill"] != "FindObject"]
    assert values
    assert all(v == "{locate_bottle_object}" for v in values)


def test_telemetry_aggregates_without_storing_reasoning_text():
    token = start_trace({"pipeline": "hybrid"})
    try:
        record_stage("generate_ir", 1.25)
        record_model_call(
            role="planner",
            operation="planner.task_plan.attempt_1",
            model="qwen3.6-planner",
            elapsed_sec=1.2,
            prompt_chars=100,
            response_chars=50,
            reasoning_chars=321,
            usage={"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
        )
        snap = snapshot_trace(1.4)
    finally:
        reset_trace(token)
    assert snap["stage_summary"]["generate_ir"]["calls"] == 1
    assert snap["model_summary"]["planner"]["calls"] == 1
    assert snap["model_summary"]["planner"]["total_tokens"] == 50
    assert snap["model_summary"]["planner"]["reasoning_chars"] == 321
    assert "reasoning_content" not in str(snap)


def test_timed_node_factory_returns_wrapper_in_source():
    """Guard against the v4.7 startup regression: _timed_node must return wrapped."""
    import ast

    graph_path = ROOT / "app" / "graph.py"
    tree = ast.parse(graph_path.read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_timed_node"
    )
    assert any(
        isinstance(stmt, ast.Return)
        and isinstance(stmt.value, ast.Name)
        and stmt.value.id == "wrapped"
        for stmt in fn.body
    ), "_timed_node() must return the wrapped callable passed to StateGraph.add_node"
