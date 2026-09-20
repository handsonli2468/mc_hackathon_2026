from pathlib import Path

import yaml

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import validate_bt_xml
from agent.app.compiler_adapter import build_invocation_plan
from agent.app.ir_normalizer import compiler_capability_contract, normalize_task_plan_for_compiler
from agent.app.ir_validator import validate_task_plan_ir
from agent.app.phase_compiler import assemble_phase_subtrees, deterministic_phase_subtree
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import TaskPlanIR

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def make_ir_with_operational_cleanup() -> TaskPlanIR:
    return TaskPlanIR.model_validate(
        {
            "schema_version": "1.0",
            "mission_status": "EXECUTABLE",
            "goal": {"description": "find and hold bottle", "success_conditions": ["bottle held"]},
            "required_capabilities": [],
            "missing_capabilities": [],
            "required_user_information": [],
            "assumptions": [],
            "global_constraints": [],
            "termination_policy": {
                "success_conditions": ["bottle held"],
                "failure_conditions": ["exhausted"],
                "max_mission_duration_sec": 300,
                "on_unrecoverable_failure": "MISSION_FAILURE",
            },
            "phases": [
                {
                    "id": "locate_bottle",
                    "objective": "locate bottle",
                    "desired_state": ["localized"],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "bottle"}},
                    "verification": [
                        {
                            "condition": "IsObjectLocated",
                            "arguments": {"object": "locate_bottle.outputs.object"},
                        }
                    ],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [],
                },
                {
                    "id": "grasp_bottle",
                    "objective": "grasp bottle",
                    "desired_state": ["object held"],
                    "preconditions": ["localized"],
                    "nominal_action": {
                        "skill": "PickObject",
                        "arguments": {"object": "locate_bottle.outputs.object"},
                    },
                    "verification": [
                        {
                            "condition": "IsObjectHeld",
                            "arguments": {"object": "locate_bottle.outputs.object"},
                        }
                    ],
                    "failure_modes": [],
                    "stale_dependencies": [],
                    "side_effects": [],
                    "resource_requirements": [],
                    "cleanup": [
                        {"skill": "RotateInPlace", "arguments": {"degrees": 15}}
                    ],
                },
            ],
        }
    )


def test_cleanup_contract_is_explicit_and_currently_metadata_only():
    caps = compiler_capability_contract()
    assert caps["action_verification"] is True
    assert caps["bounded_retry"] is True
    assert caps["failure_specific_recovery"] is False
    assert caps["phase_cleanup_execution"] is False


def test_operational_cleanup_is_warning_not_random_planning_failure():
    ir = make_ir_with_operational_cleanup()
    result = validate_task_plan_ir(ir, DEMO)
    assert result["valid"], result
    assert any("not declared cleanup_eligible" in w for w in result["warnings"])


def test_compiler_ir_deterministically_defers_cleanup_but_preserves_original_ir():
    ir = make_ir_with_operational_cleanup()
    normalized = normalize_task_plan_for_compiler(ir, DEMO)
    compiler_ir = normalized["ir"]

    assert len(ir.phases[1].cleanup) == 1  # planner artifact is unchanged
    assert compiler_ir.phases[1].cleanup == []
    assert normalized["deferred_semantics"][0]["skill"] == "RotateInPlace"
    assert normalized["deferred_semantics"][0]["cleanup_eligible"] is False
    assert normalized["warnings"]


def test_cleanup_is_not_part_of_current_executable_invocation_plan():
    ir = make_ir_with_operational_cleanup()
    invocations = build_invocation_plan(ir, DEMO)
    assert all(not inv["role"].startswith("cleanup_") for inv in invocations)
    assert all(inv["skill"] != "RotateInPlace" for inv in invocations)


def test_observed_cleanup_variation_no_longer_breaks_valid_bt():
    original_ir = make_ir_with_operational_cleanup()
    compiler_ir = normalize_task_plan_for_compiler(original_ir, DEMO)["ir"]
    phase_subtrees = {
        phase.id: deterministic_phase_subtree(phase)
        for phase in compiler_ir.phases
    }
    assembled = assemble_phase_subtrees(compiler_ir, phase_subtrees, DEMO)
    normalized_bt = normalize_compiled_bt_xml(assembled, compiler_ir, DEMO, BUILTIN)
    assert normalized_bt["error"] is None, normalized_bt

    structural = validate_bt_xml(normalized_bt["xml"], skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(normalized_bt["xml"], compiler_ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic
    assert "RotateInPlace" not in normalized_bt["xml"]


def test_unknown_cleanup_skill_is_still_rejected_as_hallucination():
    ir = make_ir_with_operational_cleanup()
    ir.phases[1].cleanup[0].skill = "MagicCleanup"
    result = validate_task_plan_ir(ir, DEMO)
    assert not result["valid"]
    assert any("unknown skill 'MagicCleanup'" in e for e in result["errors"])
