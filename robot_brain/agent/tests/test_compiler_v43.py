from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import validate_bt_xml
from agent.app.compiler_adapter import task_plan_to_compiler_task
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import TaskPlanIR

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def make_ir() -> TaskPlanIR:
    return TaskPlanIR.model_validate(
        {
            "schema_version": "1.0",
            "mission_status": "EXECUTABLE",
            "goal": {
                "description": "找到水瓶並確定真的拿在手上",
                "success_conditions": ["object_is_held"],
            },
            "required_capabilities": [
                "locate_object",
                "verify_object_located",
                "navigate_to_object",
                "verify_near_object",
                "pick_object",
                "verify_object_held",
            ],
            "missing_capabilities": [],
            "required_user_information": [],
            "assumptions": [],
            "global_constraints": [],
            "termination_policy": {
                "success_conditions": ["object_is_held"],
                "failure_conditions": ["recovery_exhausted"],
                "max_mission_duration_sec": 300,
                "on_unrecoverable_failure": "MISSION_FAILURE",
            },
            "phases": [
                {
                    "id": "locate_bottle",
                    "objective": "locate bottle",
                    "desired_state": ["object_pose_valid"],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "水瓶"}},
                    "verification": [
                        {
                            "condition": "IsObjectLocated",
                            "arguments": {"object": "{{locate_bottle.outputs.object}}"},
                        }
                    ],
                    "failure_modes": [
                        {
                            "failure": "PERCEPTION_UNAVAILABLE",
                            "classification": "TRANSIENT",
                            "recovery": [],
                            "max_attempts": 2,
                            "timeout_sec": 20,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                    "stale_dependencies": ["object_pose"],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "navigate_to_bottle",
                    "objective": "navigate",
                    "desired_state": ["robot_near_target"],
                    "preconditions": ["object_pose_valid"],
                    "nominal_action": {
                        "skill": "NavigateToObject",
                        "arguments": {"object": "{{locate_bottle.outputs.object}}"},
                    },
                    "verification": [
                        {
                            "condition": "IsNearObject",
                            "arguments": {"object": "{{locate_bottle.outputs.object}}"},
                        }
                    ],
                    "failure_modes": [
                        {
                            "failure": "NAVIGATION_TIMEOUT",
                            "classification": "TRANSIENT",
                            "recovery": [],
                            "max_attempts": 2,
                            "timeout_sec": 60,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                    "stale_dependencies": ["object_pose"],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "pick_bottle",
                    "objective": "pick",
                    "desired_state": ["object_is_held"],
                    "preconditions": ["robot_near_target"],
                    "nominal_action": {
                        "skill": "PickObject",
                        "arguments": {"object": "{{locate_bottle.outputs.object}}"},
                    },
                    "verification": [
                        {
                            "condition": "IsObjectHeld",
                            "arguments": {"object": "{{locate_bottle.outputs.object}}"},
                        }
                    ],
                    "failure_modes": [
                        {
                            "failure": "EXECUTION_FAILED",
                            "classification": "TRANSIENT",
                            "recovery": [],
                            "max_attempts": 2,
                            "timeout_sec": 30,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                    "stale_dependencies": ["object_pose"],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
            ],
        }
    )


def test_normalizer_repairs_btgenbot_serialization_but_not_topology():
    raw = '''<root main_tree_to_execute="MainTree" BTCPP_format="4">
      <BehaviorTree ID="MainTree">
        <Sequence name="root_sequence">
          <Fallback name="find_and_verify">
            <Action ID="FindObject" query="water_bottle"/>
            <Action ID="isObjectLocated" object="water_bottle"/>
          </Fallback>
          <Sequence name="navigate_near_bottle">
            <Action ID="NavigateToObject" object="water_bottle"/>
            <Action ID="IsNearObject" object="water_bottle"/>
          </Sequence>
          <Sequence name="pick_it_up">
            <Action ID="PickObject" object="water_bottle"/>
            <Action ID="isObjectHeld" object="water_bottle"/>
          </Sequence>
        </Sequence>
      </BehaviorTree>
    </root>'''
    ir = make_ir()
    normalized = normalize_compiled_bt_xml(raw, ir, DEMO, BUILTIN)
    root = SafeET.fromstring(normalized["xml"])

    find = next(e for e in root.iter() if e.tag == "FindObject")
    located = next(e for e in root.iter() if e.tag == "IsObjectLocated")
    nav = next(e for e in root.iter() if e.tag == "NavigateToObject")
    held = next(e for e in root.iter() if e.tag == "IsObjectHeld")
    assert find.attrib["query"] == "水瓶"
    assert find.attrib["object"] == "{locate_bottle_object}"
    assert located.attrib["object"] == "{locate_bottle_object}"
    assert nav.attrib["object"] == "{locate_bottle_object}"
    assert held.attrib["object"] == "{locate_bottle_object}"
    # The unsafe topology is intentionally not silently rewritten.
    assert any(e.tag == "Fallback" for e in root.iter())

    structural = validate_bt_xml(normalized["xml"], skill_registry_as_bt_nodes(DEMO), BUILTIN)
    assert not structural["valid"]
    assert any("before a guaranteed producer" in e for e in structural["errors"])

    semantic = validate_bt_semantics(normalized["xml"], ir, DEMO)
    assert not semantic["valid"]
    assert any("sequential control flow" in e for e in semantic["errors"])
    assert any("RetryUntilSuccessful" in e for e in semantic["errors"])


def test_semantic_validator_accepts_expected_sequence_and_retry():
    ir = make_ir()
    xml = '''<root main_tree_to_execute="MainTree" BTCPP_format="4"><BehaviorTree ID="MainTree">
    <Sequence name="mission">
      <RetryUntilSuccessful name="retry_locate" num_attempts="2"><Sequence name="locate_seq">
        <FindObject name="locate_bottle_action_findobject" query="水瓶" object="{locate_bottle_object}"/>
        <IsObjectLocated name="locate_bottle_verify_1_isobjectlocated" object="{locate_bottle_object}"/>
      </Sequence></RetryUntilSuccessful>
      <RetryUntilSuccessful name="retry_nav" num_attempts="2"><Fallback name="nav_ensure">
        <IsNearObject name="navigate_to_bottle_ensure_precheck_1_isnearobject" object="{locate_bottle_object}"/>
        <Sequence name="nav_seq">
          <NavigateToObject name="navigate_to_bottle_action_navigatetoobject" object="{locate_bottle_object}"/>
          <IsNearObject name="navigate_to_bottle_verify_1_isnearobject" object="{locate_bottle_object}"/>
        </Sequence>
      </Fallback></RetryUntilSuccessful>
      <RetryUntilSuccessful name="retry_pick" num_attempts="2"><Fallback name="pick_ensure">
        <IsObjectHeld name="pick_bottle_ensure_precheck_1_isobjectheld" object="{locate_bottle_object}"/>
        <Sequence name="pick_seq">
          <PickObject name="pick_bottle_action_pickobject" object="{locate_bottle_object}"/>
          <IsObjectHeld name="pick_bottle_verify_1_isobjectheld" object="{locate_bottle_object}"/>
        </Sequence>
      </Fallback></RetryUntilSuccessful>
    </Sequence></BehaviorTree></root>'''
    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(xml, ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic


def test_compiler_task_is_phasewise_and_does_not_ask_small_model_to_compile_policy():
    text = task_plan_to_compiler_task(make_ir())
    assert "Phase 1 (locate_bottle): Run FindObject" in text
    assert "run IsObjectLocated to verify" in text
    assert "server adds policy wrappers later" in text
    assert "RetryUntilSuccessful num_attempts=2" not in text
    assert "classification=" not in text
    assert "TaskPlanIR" not in text
