from pathlib import Path

import yaml

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import validate_bt_xml
from agent.app.ir_normalizer import compiler_capability_contract, normalize_task_plan_for_compiler
from agent.app.phase_compiler import assemble_phase_subtrees, deterministic_phase_subtree
from agent.app.quality_gate import assess_plan_quality
from agent.app.registry import skill_registry_as_bt_nodes
from agent.app.schemas import TaskPlanIR

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def make_search_ir(attempts: int = 8) -> TaskPlanIR:
    return TaskPlanIR.model_validate(
        {
            "schema_version": "1.0",
            "mission_status": "EXECUTABLE",
            "goal": {"description": "find bottle", "success_conditions": ["bottle localized"]},
            "required_capabilities": [],
            "missing_capabilities": [],
            "required_user_information": [],
            "assumptions": [],
            "global_constraints": [],
            "termination_policy": {
                "success_conditions": ["bottle localized"],
                "failure_conditions": ["search exhausted"],
                "max_mission_duration_sec": 300,
                "on_unrecoverable_failure": "MISSION_FAILURE",
            },
            "phases": [
                {
                    "id": "locate_bottle",
                    "objective": "find bottle",
                    "desired_state": ["localized"],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "bottle"}},
                    "verification": [
                        {"condition": "IsObjectLocated", "arguments": {"object": "FindObject.object"}}
                    ],
                    "failure_modes": [
                        {
                            "failure": "OBJECT_NOT_FOUND",
                            "classification": "STALE_INFORMATION",
                            "recovery": [
                                {
                                    "strategy": "REFRESH_INFORMATION",
                                    "skill": "RotateInPlace",
                                    "arguments": {"degrees": 45},
                                }
                            ],
                            "max_attempts": attempts,
                            "timeout_sec": 20,
                            "escalation": None,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                    "stale_dependencies": ["object_pose"],
                    "side_effects": [],
                    "resource_requirements": ["CAMERA"],
                    "cleanup": [],
                }
            ],
        }
    )


def test_quality_gate_accepts_bounded_full_viewpoint_sweep():
    gate = assess_plan_quality(make_search_ir(8), DEMO)
    assert gate["pass"], gate
    assert not gate["needs_critic"]


def test_quality_gate_marks_weak_two_view_search_as_hardenable_advisory():
    gate = assess_plan_quality(make_search_ir(2), DEMO)
    assert gate["pass"], gate
    assert not gate["needs_critic"]
    assert any("compiler will safely strengthen" in x for x in gate["advisories"])


def test_v5_compiles_search_false_branch_to_rotate_then_retry():
    ir = make_search_ir(8)
    phase_subtrees = {ir.phases[0].id: deterministic_phase_subtree(ir.phases[0])}
    assembled = assemble_phase_subtrees(ir, phase_subtrees, DEMO, BUILTIN)
    normalized = normalize_compiled_bt_xml(assembled, ir, DEMO, BUILTIN)
    assert normalized["error"] is None, normalized
    xml = normalized["xml"]
    assert "locate_bottle_observation_fallback" in xml
    assert "<RotateInPlace" in xml
    assert 'degrees="45"' in xml
    assert "<ForceFailure" in xml
    assert "<AlwaysFailure" not in xml
    assert 'num_attempts="8"' in xml

    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(xml, ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic


def test_compiler_ir_strengthens_short_search_sweep_without_mutating_planner_ir():
    planner_ir = make_search_ir(2)
    normalized = normalize_task_plan_for_compiler(planner_ir, DEMO)
    compiler_ir = normalized["ir"]
    assert planner_ir.phases[0].failure_modes[0].max_attempts == 2
    assert compiler_ir.phases[0].failure_modes[0].max_attempts == 8
    assert any("search sweep strengthened" in x for x in normalized["changes"])


def test_compiler_contract_declares_observable_search_recovery():
    caps = compiler_capability_contract()
    assert caps["observable_search_recovery"] is True
    assert caps["failure_specific_recovery"] is False
