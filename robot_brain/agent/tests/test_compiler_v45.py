from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_xml import validate_bt_xml
from agent.app.compiler_adapter import build_invocation_plan
from agent.app.phase_compiler import validate_phase_candidate
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import TaskPlanIR

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def make_ir_with_skill_output_alias() -> TaskPlanIR:
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
                    "id": "locate_object",
                    "objective": "locate",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "water bottle"}},
                    "verification": [
                        {"condition": "IsObjectLocated", "arguments": {"object": "FindObject.output"}}
                    ],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "navigate_to_object",
                    "objective": "navigate",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "NavigateToObject", "arguments": {"object": "FindObject.output"}},
                    "verification": [
                        {"condition": "IsNearObject", "arguments": {"object": "FindObject.output"}}
                    ],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "pick_object",
                    "objective": "pick",
                    "desired_state": [],
                    "preconditions": [],
                    "nominal_action": {"skill": "PickObject", "arguments": {"object": "FindObject.output"}},
                    "verification": [
                        {"condition": "IsObjectHeld", "arguments": {"object": "FindObject.output"}}
                    ],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
            ],
        }
    )


def test_skill_output_alias_maps_to_unique_producer_blackboard():
    ir = make_ir_with_skill_output_alias()
    invocations = build_invocation_plan(ir, DEMO)
    object_values = []
    for inv in invocations:
        if "object" in inv["ports"]:
            object_values.append(inv["ports"]["object"])
    assert object_values
    assert set(object_values) == {"{locate_object_object}"}


def test_observed_v44_blackboard_failure_now_normalizes_and_validates():
    ir = make_ir_with_skill_output_alias()
    raw = '''<root BTCPP_format="4" main_tree_to_execute="MainTree"><BehaviorTree ID="MainTree"><Sequence name="mission_sequence">
      <Sequence><FindObject/><IsObjectLocated/></Sequence>
      <Sequence><NavigateToObject/><IsNearObject/></Sequence>
      <Sequence><PickObject/><IsObjectHeld/></Sequence>
    </Sequence></BehaviorTree></root>'''
    result = normalize_compiled_bt_xml(raw, ir, DEMO, BUILTIN)
    assert result["error"] is None
    root = SafeET.fromstring(result["xml"])
    for tag in ["FindObject", "IsObjectLocated", "NavigateToObject", "IsNearObject", "PickObject", "IsObjectHeld"]:
        el = next(root.iter(tag))
        if tag == "FindObject":
            assert el.attrib["object"] == "{locate_object_object}"
        else:
            assert el.attrib["object"] == "{locate_object_object}"
    structural = validate_bt_xml(result["xml"], skill_registry_as_bt_nodes(DEMO), BUILTIN)
    assert structural["valid"], structural


def test_phase_candidate_accepts_single_wrapperless_subtree_but_not_extra_siblings():
    phase = make_ir_with_skill_output_alias().phases[1]
    good = '''<root BTCPP_format="4"><Sequence><Action ID="NavigateToObject"/><Condition ID="IsNearObject"/></Sequence></root>'''
    result = validate_phase_candidate(good, phase, BUILTIN)
    assert result["valid"], result
    assert result["subtree_xml"].startswith("<Sequence")
    assert result["warnings"]

    bad = '''<root BTCPP_format="4"><Sequence><Action ID="NavigateToObject"/><Condition ID="IsNearObject"/></Sequence><Action ID="NavigateToObject"/></root>'''
    result = validate_phase_candidate(bad, phase, BUILTIN)
    assert not result["valid"]
    assert any("does not contain exactly one phase subtree" in e for e in result["errors"])
