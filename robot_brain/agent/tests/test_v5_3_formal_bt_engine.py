from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET

from agent.app.bt_engine_contract import validate_runtime_contract
from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import validate_bt_xml
from agent.app.phase_compiler import assemble_phase_subtrees, deterministic_phase_subtree
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import TaskPlanIR
from agent.app.skill_contract_normalizer import normalize_plan_to_runtime_skill_contract

ROOT = Path(__file__).resolve().parents[1]
FORMAL = yaml.safe_load((ROOT / "defaults" / "skill_registry.yaml").read_text(encoding="utf-8"))
BUILTINS = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def formal_ir() -> TaskPlanIR:
    return TaskPlanIR.model_validate({
        "schema_version": "1.0",
        "mission_id": None,
        "mission_status": "EXECUTABLE",
        "goal": {"description": "find bottle and approach it", "success_conditions": ["robot near bottle"]},
        "required_capabilities": [
            "search_object", "verify_object_located", "navigate_to_object",
        ],
        "missing_capabilities": [],
        "required_user_information": [],
        "assumptions": [],
        "global_constraints": [],
        "termination_policy": {
            "success_conditions": ["robot near bottle"],
            "failure_conditions": ["attempts exhausted"],
            "max_mission_duration_sec": 300,
            "on_unrecoverable_failure": "MISSION_FAILURE",
        },
        "phases": [
            {
                "id": "locate_bottle",
                "objective": "make bottle visible",
                "desired_state": ["object_visible"],
                "preconditions": [],
                "nominal_action": {"skill": "VisualizeObject", "arguments": {"object_name": "bottle"}},
                "verification": [{"condition": "IsObjectFound", "arguments": {"object_name": "bottle"}}],
                "failure_modes": [{
                    "failure": "OBJECT_NOT_FOUND", "classification": "STALE_INFORMATION",
                    "recovery": [{"strategy": "REFRESH_INFORMATION", "skill": "Patrol", "arguments": {}}],
                    "max_attempts": 1, "timeout_sec": 60, "escalation": None,
                    "on_exhaustion": "MISSION_FAILURE",
                }],
                "stale_dependencies": ["object_visibility"], "side_effects": [], "resource_requirements": ["CAMERA"], "cleanup": [],
            },
            {
                "id": "approach_bottle",
                "objective": "reach bottle",
                "desired_state": ["robot_at_object"],
                "preconditions": ["object_visible"],
                "nominal_action": {"skill": "NavigateToDetectedObject", "arguments": {"speed": "normal"}},
                "verification": [],
                "failure_modes": [{
                    "failure": "NAVIGATION_TIMEOUT", "classification": "TRANSIENT", "recovery": [],
                    "max_attempts": 2, "timeout_sec": 60, "escalation": None,
                    "on_exhaustion": "MISSION_FAILURE",
                }],
                "stale_dependencies": ["object_visibility"], "side_effects": [], "resource_requirements": ["BASE"], "cleanup": [],
            },

        ],
    })


def test_formal_registry_matches_exported_tree_nodes_model():
    result = validate_runtime_contract(FORMAL, BUILTINS)
    assert result["valid"], result["errors"]
    assert result["engine_node_count"] == 7
    assert result["builtin_node_count"] == 13
    assert result["effective_node_count"] == 20
    assert set(s["id"] for s in FORMAL["skills"]) == {
        "IsObjectFound", "NavigateToDetectedObject", "NavigateToPoint",
        "Patrol", "RotateInPlace", "SetGripper", "VisualizeObject",
    }
    set_gripper = next(s for s in FORMAL["skills"] if s["id"] == "SetGripper")
    assert set_gripper["inputs"]["position"]["min"] == 0
    assert set_gripper["inputs"]["position"]["max"] == 100


def test_live_custom_only_sync_reports_separate_builtin_and_effective_counts(tmp_path):
    root = SafeET.fromstring((ROOT / "defaults" / "bt_engine_nodes.xml").read_text(encoding="utf-8"))
    model = root.find(".//TreeNodesModel")
    assert model is not None
    custom_ids = {skill["id"] for skill in FORMAL["skills"]}
    for node in list(model):
        if node.attrib.get("ID") not in custom_ids:
            model.remove(node)
    custom_path = tmp_path / "custom_nodes.xml"
    custom_path.write_text(SafeET.tostring(root, encoding="unicode"), encoding="utf-8")

    result = validate_runtime_contract(FORMAL, BUILTINS, engine_model_path=custom_path)
    assert result["valid"], result["errors"]
    assert result["engine_node_count"] == 7
    assert result["engine_leaf_count"] == 7
    assert result["builtin_node_count"] == 13
    assert result["effective_node_count"] == 20


def test_legacy_ir_is_migrated_to_formal_named_object_contract():
    ir = formal_ir().model_copy(deep=True)
    ir.phases[0].nominal_action.skill = "FindObject"
    ir.phases[0].nominal_action.arguments = {"query": "bottle"}
    ir.phases[0].verification[0].condition = "IsObjectLocated"
    ir.phases[0].verification[0].arguments = {"object": "{{locate_bottle.nominal_action.outputs.object}}"}
    ir.phases[0].failure_modes[0].recovery[0].skill = "RotateInPlace"
    ir.phases[0].failure_modes[0].recovery[0].arguments = {"degrees": 45}

    result = normalize_plan_to_runtime_skill_contract(ir, FORMAL)
    out = result["ir"]
    assert result["changed"]
    assert out.phases[0].nominal_action.skill == "VisualizeObject"
    assert out.phases[0].nominal_action.arguments == {"object_name": "bottle"}
    assert out.phases[0].verification[0].condition == "IsObjectFound"
    assert out.phases[0].verification[0].arguments == {"object_name": "bottle"}
    assert out.phases[0].failure_modes[0].recovery[0].arguments == {"angle_deg": 45}
    # Deprecated acquisition aliases are intentionally not migrated to a disabled
    # runtime node. SetGripper semantics are configured from the live ABI instead.


def test_formal_bottle_tree_uses_continuous_patrol_search_and_exact_ports():
    ir = formal_ir()
    phase_subtrees = {p.id: deterministic_phase_subtree(p) for p in ir.phases}
    raw = assemble_phase_subtrees(ir, phase_subtrees, FORMAL, BUILTINS)
    normalized = normalize_compiled_bt_xml(raw, ir, FORMAL, BUILTINS)
    assert normalized["error"] is None
    xml = normalized["xml"]
    root = SafeET.fromstring(xml)

    monitor = next(x for x in root.iter() if x.tag == "ReactiveFallback")
    assert [x.tag for x in list(monitor)] == ["IsObjectFound", "Patrol"]
    timeout = next(x for x in root.iter() if x.tag == "Timeout" and x.attrib.get("name") == "locate_bottle_search_timeout")
    assert timeout.attrib["msec"] == "60000"
    search_sequence = next(
        x for x in root.iter()
        if x.tag == "Sequence" and x.attrib.get("name") == "locate_bottle_continuous_search"
    )
    # The search policy itself must not be wrapped in the obsolete rotate/retry
    # topology. A later navigation phase may independently have a bounded retry.
    assert not any(
        x.tag in {"RetryUntilSuccessful", "ForceFailure", "RotateInPlace"}
        for x in search_sequence.iter()
    )

    # Runtime contract is literal named-object ports, not synthetic tracked-object outputs.
    for tag in ["VisualizeObject", "IsObjectFound"]:
        for el in [x for x in root.iter() if x.tag == tag]:
            assert el.attrib.get("object_name") == "bottle"
    assert "FindObject" not in xml
    assert "PickObject" not in xml
    assert "IsNearObject" not in xml
    assert "IsObjectLocated" not in xml
    assert "RecoveryNode" not in xml
    assert "AlwaysFailure" not in xml

    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(FORMAL), BUILTINS)
    semantic = validate_bt_semantics(xml, ir, FORMAL)
    assert structural["valid"], structural["errors"]
    assert semantic["valid"], semantic["errors"]


def test_shipped_formal_bottle_example_is_structurally_valid():
    xml = (ROOT.parent / "examples" / "formal_bottle_bt.xml").read_text(encoding="utf-8")
    result = validate_bt_xml(xml, skill_registry_as_bt_nodes(FORMAL), BUILTINS)
    assert result["valid"], result["errors"]


def test_normalizer_removes_compiler_invented_optional_speed_and_uses_engine_default():
    ir = formal_ir().model_copy(deep=True)
    ir.phases[1].nominal_action.arguments = {}
    phase_subtrees = {
        ir.phases[0].id: deterministic_phase_subtree(ir.phases[0]),
        ir.phases[1].id: '<NavigateToDetectedObject speed="0.3"/>',
    }
    raw = assemble_phase_subtrees(ir, phase_subtrees, FORMAL, BUILTINS)
    normalized = normalize_compiled_bt_xml(raw, ir, FORMAL, BUILTINS)

    assert normalized["error"] is None
    root = SafeET.fromstring(normalized["xml"])
    navigate = next(x for x in root.iter() if x.tag == "NavigateToDetectedObject")
    assert "speed" not in navigate.attrib
    assert any("removed unplanned NavigateToDetectedObject.speed" in x for x in normalized["changes"])
    structural = validate_bt_xml(normalized["xml"], skill_registry_as_bt_nodes(FORMAL), BUILTINS)
    assert structural["valid"], structural["errors"]


def test_normalizer_overrides_compiler_speed_with_explicit_ir_value():
    ir = formal_ir().model_copy(deep=True)
    phase_subtrees = {
        ir.phases[0].id: deterministic_phase_subtree(ir.phases[0]),
        ir.phases[1].id: '<NavigateToDetectedObject speed="0.3"/>',
    }
    raw = assemble_phase_subtrees(ir, phase_subtrees, FORMAL, BUILTINS)
    normalized = normalize_compiled_bt_xml(raw, ir, FORMAL, BUILTINS)

    assert normalized["error"] is None
    root = SafeET.fromstring(normalized["xml"])
    navigate = next(x for x in root.iter() if x.tag == "NavigateToDetectedObject")
    assert navigate.attrib["speed"] == "normal"
    structural = validate_bt_xml(normalized["xml"], skill_registry_as_bt_nodes(FORMAL), BUILTINS)
    assert structural["valid"], structural["errors"]
