from __future__ import annotations

from typing import Any, TypedDict
from time import perf_counter

from langgraph.graph import END, START, StateGraph

from .bt_xml import extract_bt_xml, normalize_bt_xml, validate_bt_xml
from .bt_engine_contract import validate_runtime_contract
from .bt_normalizer import normalize_compiled_bt_xml
from .bt_semantic import validate_bt_semantics
from .phase_compiler import (
    assemble_phase_subtrees,
    deterministic_phase_subtree,
    recovery_compilation_warnings,
    prepare_phase_candidate_xml,
    validate_phase_candidate,
)
from .compiler_adapter import task_plan_to_compiler_task, phase_compiler_task, phase_actions_text
from .ir_validator import validate_task_plan_ir
from .ir_normalizer import compiler_capability_contract, normalize_task_plan_for_compiler
from .metrics import bt_metrics
from .quality_gate import assess_plan_quality
from .plan_hardening import harden_plan_for_quality
from .grounding_policy import apply_grounding_policy
from .location_policy import apply_location_grounding_policy, validate_location_navigation_provenance
from .conversation_grounding import normalize_requirement_grounding_assumptions
from .mission_semantics import normalize_mission_semantics, validate_goal_effect_closure
from .compact_plan import enrich_compact_plan, normalize_compact_plan
from .skill_contract_normalizer import normalize_plan_to_runtime_skill_contract
from .model_client import compiler_chat, planner_chat, planner_json
from .prompts import (
    bt_repair_messages,
    compiler_messages,
    compiler_repair_messages,
    critic_messages,
    direct_bt_messages,
    ir_repair_messages,
    planner_messages,
    compact_planner_messages,
    requirement_messages,
)
from .registry import check_capabilities, load_builtin_registry, load_skill_registry, skill_registry_as_bt_nodes
from .rag import retrieve_context, validate_plan_closed_set, validate_bt_xml_closed_set, analyze_plan_rag_utilization
from .schemas import (
    CompactSemanticPlan,
    Goal,
    MissionRequirements,
    MissionStatus,
    RequestKind,
    RequirementHint,
    TaskPlanDraft,
    TaskPlanIR,
    TerminationPolicy,
)
from .settings import settings
from .telemetry import record_stage


class BrainState(TypedDict, total=False):
    message: str
    history: list[dict[str, str]]
    world_state: dict[str, Any]
    pipeline_mode: str
    seed_requirements: dict[str, Any] | None
    requirements: dict[str, Any]
    requirement_normalization: dict[str, Any]
    mission_semantic_normalization: dict[str, Any]
    goal_effect_closure: dict[str, Any]
    rag_context: dict[str, Any]
    rag_validation: dict[str, Any]
    rag_utilization: dict[str, Any]
    capability_check: dict[str, Any]
    bt_engine_contract: dict[str, Any]
    status: str
    assistant_message: str
    questions: list[dict[str, str]]
    missing_capabilities: list[str]
    compact_semantic_plan: dict[str, Any] | None
    compact_planner_used: bool
    planner_escalated: bool
    planner_task_plan_ir: dict[str, Any] | None
    task_plan_ir: dict[str, Any] | None
    plan_hardening: dict[str, Any]
    skill_contract_normalization: dict[str, Any]
    grounding_policy: dict[str, Any]
    location_grounding_policy: dict[str, Any]
    compiler_task_plan_ir: dict[str, Any] | None
    ir_validation: dict[str, Any]
    ir_normalization: dict[str, Any]
    compiler_capabilities: dict[str, Any]
    ir_repair_attempts: int
    critic_warning: str | None
    critic_attempted: bool
    quality_gate: dict[str, Any]
    quality_gate_before_critic: dict[str, Any]
    quality_gate_after_critic: dict[str, Any]
    compiler_task: str
    compiler_phases: list[dict[str, Any]]
    compiler_warnings: list[str]
    compiler_fallback_count: int
    raw_bt_response: str
    bt_normalization: dict[str, Any]
    bt_xml: str | None
    bt_validation: dict[str, Any]
    bt_repair_attempts: int
    metrics: dict[str, Any]


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _draft_from_ir(ir: TaskPlanIR) -> TaskPlanDraft:
    return TaskPlanDraft(
        schema_version="1.0",
        goal=ir.goal,
        assumptions=ir.assumptions,
        global_constraints=ir.global_constraints,
        termination_policy=ir.termination_policy,
        phases=ir.phases,
    )


def _assemble_ir(draft: TaskPlanDraft, requirements: MissionRequirements) -> TaskPlanIR:
    # The LLM owns semantic planning. Deterministic orchestration owns mission status,
    # capability availability and missing-user-information state.
    success_conditions = _dedupe([
        *requirements.success_conditions,
        *draft.goal.success_conditions,
    ])
    goal = Goal(description=draft.goal.description, success_conditions=success_conditions)

    termination = TerminationPolicy(
        success_conditions=_dedupe([
            *draft.termination_policy.success_conditions,
            *success_conditions,
        ]),
        failure_conditions=_dedupe(draft.termination_policy.failure_conditions),
        max_mission_duration_sec=draft.termination_policy.max_mission_duration_sec,
        on_unrecoverable_failure=draft.termination_policy.on_unrecoverable_failure,
    )

    return TaskPlanIR(
        schema_version="1.0",
        mission_status=MissionStatus.EXECUTABLE,
        goal=goal,
        required_capabilities=_dedupe(requirements.required_capabilities),
        missing_capabilities=[],
        required_user_information=[],
        task_semantics=requirements.task_semantics,
        retrieval_sketch=requirements.retrieval_sketch,
        assumptions=_dedupe([*requirements.assumptions, *draft.assumptions]),
        global_constraints=_dedupe(draft.global_constraints),
        termination_policy=termination,
        phases=draft.phases,
    )




def _task_thinking_budget(state: BrainState) -> int:
    """Choose a bounded thinking budget without disabling Qwen thinking mode.

    Simple missions get the 4k budget that should fit the observed bottle task.
    Larger capability sets / world state automatically receive more reasoning room.
    The deterministic quality gate and conditional critic protect quality if a first
    draft is too shallow.
    """
    try:
        req = MissionRequirements.model_validate(state.get("requirements") or {})
        cap_count = len(req.required_capabilities)
    except Exception:
        cap_count = 0
    world_size = len(str(state.get("world_state") or {}))
    msg_size = len(str(state.get("message") or ""))
    if cap_count >= 10 or world_size > 8000 or msg_size > 2000:
        return settings.planner_task_thinking_budget_complex
    if cap_count >= 7 or world_size > 2500 or msg_size > 800:
        return settings.planner_task_thinking_budget_medium
    return settings.planner_task_thinking_budget

async def extract_requirements(state: BrainState) -> BrainState:
    if state.get("seed_requirements"):
        try:
            req = MissionRequirements.model_validate(state["seed_requirements"])
            norm = normalize_requirement_grounding_assumptions(req, state.get("world_state"), state.get("message"))
            semantic_norm = normalize_mission_semantics(norm["requirements"])
            return {
                **state,
                "requirements": semantic_norm["requirements"].model_dump(mode="json"),
                "requirement_normalization": {k:v for k,v in norm.items() if k != "requirements"},
                "mission_semantic_normalization": {k:v for k,v in semantic_norm.items() if k != "requirements"},
            }
        except Exception:
            pass
    try:
        req = await planner_json(
            MissionRequirements,
            requirement_messages(state["message"], state.get("history", []), state.get("world_state")),
            retries=1,
            thinking_token_budget=settings.planner_requirements_thinking_budget,
            operation="planner.requirements",
        )
        norm = normalize_requirement_grounding_assumptions(req, state.get("world_state"), state.get("message"))
        semantic_norm = normalize_mission_semantics(norm["requirements"])
        return {
            **state,
            "requirements": semantic_norm["requirements"].model_dump(mode="json"),
            "requirement_normalization": {k:v for k,v in norm.items() if k != "requirements"},
            "mission_semantic_normalization": {k:v for k,v in semantic_norm.items() if k != "requirements"},
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"Requirement extraction failed: {exc}",
        }


async def retrieve_rag_node(state: BrainState) -> BrainState:
    if state.get("status") == "PLANNING_FAILURE":
        return state
    try:
        req = MissionRequirements.model_validate(state.get("requirements") or {})
    except Exception as exc:
        return {**state, "status": "PLANNING_FAILURE", "assistant_message": f"RAG prerequisite validation failed: {exc}"}
    if req.request_kind != RequestKind.MISSION or req.status_hint != RequirementHint.READY or req.required_user_information:
        return {**state, "rag_context": {"enabled": settings.rag_enabled, "skipped": True, "reason": "mission_not_ready"}}
    if not settings.rag_enabled:
        if settings.rag_required:
            return {**state, "status": "PLANNING_FAILURE", "assistant_message": "RAG is required but disabled."}
        return {**state, "rag_context": {"enabled": False, "skipped": True, "reason": "disabled"}}
    try:
        ctx = retrieve_context(state.get("message", ""), state.get("requirements") or {}, world_state=state.get("world_state"))
        return {**state, "rag_context": ctx}
    except Exception as exc:
        if settings.rag_required:
            return {**state, "status": "PLANNING_FAILURE", "assistant_message": f"RAG retrieval failed: {exc}", "rag_context": {"enabled": True, "error": str(exc)}}
        return {**state, "rag_context": {"enabled": True, "error": str(exc)}}


async def capability_check_node(state: BrainState) -> BrainState:
    if state.get("status") == "PLANNING_FAILURE":
        return state
    contract = validate_runtime_contract()
    if not contract.get("valid"):
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "bt_engine_contract": contract,
            "assistant_message": "Active skill registry does not match the formal team BT-engine contract.",
        }
    req = MissionRequirements.model_validate(state["requirements"])
    if req.request_kind == RequestKind.CHAT:
        return {**state, "status": "CHAT", "assistant_message": req.message or "How can I help?"}
    if req.status_hint == RequirementHint.NEED_MORE_INFO or req.required_user_information:
        return {
            **state,
            "status": "NEED_MORE_INFO",
            "assistant_message": req.message or "I need more information.",
            "questions": [x.model_dump() for x in req.required_user_information],
        }
    if req.status_hint == RequirementHint.UNSAFE:
        return {**state, "status": "UNSAFE", "assistant_message": req.message or "The requested mission has a safety issue."}
    if req.status_hint == RequirementHint.INVALID_REQUEST:
        return {
            **state,
            "status": "INVALID_REQUEST",
            "assistant_message": req.message or "The request could not be interpreted as a robot mission.",
        }

    check = check_capabilities(req.required_capabilities, load_skill_registry())
    if not check["ok"]:
        return {
            **state,
            "status": "UNSUPPORTED",
            "assistant_message": "The current skill library cannot complete this mission.",
            "missing_capabilities": check["missing"],
            "capability_check": check,
        }
    rag_ctx = state.get("rag_context") or {}
    rag_missing = list(rag_ctx.get("missing_capabilities", []) or [])
    if settings.rag_required and rag_missing:
        return {
            **state,
            "status": "UNSUPPORTED",
            "assistant_message": "RAG retrieval could not cover all required robot capabilities.",
            "missing_capabilities": rag_missing,
            "capability_check": {**check, "rag_coverage_ok": False, "rag_missing": rag_missing},
        }
    return {**state, "status": "EXECUTABLE", "capability_check": {**check, "rag_coverage_ok": not rag_missing}, "bt_engine_contract": contract}


def route_after_gate(state: BrainState) -> str:
    if state.get("status") != "EXECUTABLE":
        return "finish_dialogue"
    return "direct_generate" if state.get("pipeline_mode") == "direct" else "generate_ir"



def _use_compact_planner(state: BrainState) -> bool:
    if not settings.compact_planner_enabled:
        return False
    try:
        req = MissionRequirements.model_validate(state.get("requirements") or {})
        if len(req.required_capabilities) > settings.compact_planner_max_capabilities:
            return False
    except Exception:
        return False
    # The compact enricher relies on the formal SkillManifest metadata; if contract
    # validation already passed this is the preferred normal path. Very large inputs
    # stay on the full planner because they are unlikely to be latency-sensitive
    # everyday missions.
    if len(str(state.get("message") or "")) > 1200:
        return False
    if len(str(state.get("world_state") or {})) > 6000:
        return False
    return True


async def _generate_full_plan(state: BrainState, *, escalated: bool) -> BrainState:
    requirements = MissionRequirements.model_validate(state["requirements"])
    draft = await planner_json(
        TaskPlanDraft,
        planner_messages(
            state["message"],
            state["requirements"],
            state.get("history", []),
            state.get("world_state"),
            state.get("rag_context"),
        ),
        retries=1,
        temperature=0.1,
        thinking_token_budget=(settings.compact_planner_retry_thinking_budget if escalated else _task_thinking_budget(state)),
        retry_thinking_token_budget=settings.planner_task_retry_thinking_budget,
        operation="planner.task_plan_escalated" if escalated else "planner.task_plan",
    )
    ir = _assemble_ir(draft, requirements)
    ir_json = ir.model_dump(mode="json")
    return {
        **state,
        "planner_task_plan_ir": ir_json,
        "task_plan_ir": ir_json,
        "compact_planner_used": False if escalated else bool(state.get("compact_planner_used", False)),
        "planner_escalated": bool(escalated),
        "plan_hardening": {"changed": False, "changes": [], "warnings": []},
        "location_grounding_policy": {"changed": False, "changes": [], "warnings": [], "bindings": [], "removed_visual_grounding_phases": []},
        "grounding_policy": {"changed": False, "changes": [], "warnings": []},
        "skill_contract_normalization": {"changed": False, "changes": [], "warnings": []},
        "ir_repair_attempts": int(state.get("ir_repair_attempts", 0) or 0),
        "critic_warning": None,
        "critic_attempted": False,
        "quality_gate": {},
        "quality_gate_before_critic": {},
        "quality_gate_after_critic": {},
    }

async def generate_ir(state: BrainState) -> BrainState:
    try:
        requirements = MissionRequirements.model_validate(state["requirements"])
        if _use_compact_planner(state):
            try:
                compact = await planner_json(
                    CompactSemanticPlan,
                    compact_planner_messages(
                        state["message"],
                        state["requirements"],
                        state.get("history", []),
                        state.get("world_state"),
                        state.get("rag_context"),
                    ),
                    retries=0,
                    temperature=0.1,
                    thinking_token_budget=settings.compact_planner_thinking_budget,
                    operation="planner.compact_plan",
                )
                compact_norm = normalize_compact_plan(compact)
                normalized_compact = compact_norm["plan"]
                ir = enrich_compact_plan(normalized_compact, requirements)
                ir_json = ir.model_dump(mode="json")
                return {
                    **state,
                    "compact_semantic_plan": compact.model_dump(mode="json"),
                    "compact_plan_normalization": {
                        "changed": bool(compact_norm.get("changed")),
                        "changes": list(compact_norm.get("changes", [])),
                        "warnings": list(compact_norm.get("warnings", [])),
                        "normalized_plan": normalized_compact.model_dump(mode="json"),
                    },
                    "compact_planner_used": True,
                    "planner_escalated": False,
                    "planner_task_plan_ir": ir_json,
                    "task_plan_ir": ir_json,
                    "plan_hardening": {"changed": False, "changes": [], "warnings": []},
                    "location_grounding_policy": {"changed": False, "changes": [], "warnings": [], "bindings": [], "removed_visual_grounding_phases": []},
                    "grounding_policy": {"changed": False, "changes": [], "warnings": []},
                    "skill_contract_normalization": {"changed": False, "changes": [], "warnings": []},
                    "ir_repair_attempts": 0,
                    "critic_warning": None,
                    "critic_attempted": False,
                    "quality_gate": {},
                    "quality_gate_before_critic": {},
                    "quality_gate_after_critic": {},
                }
            except Exception as compact_exc:
                # Quality is protected: a malformed compact answer immediately falls
                # back to the existing full 3072-token planner, still with thinking on.
                full = await _generate_full_plan(state, escalated=True)
                full["compact_semantic_plan"] = None
                full["compact_plan_normalization"] = {"changed": False, "changes": [], "warnings": [], "normalized_plan": None}
                full["compact_planner_used"] = False
                full["compact_fallback_reason"] = str(compact_exc)
                return full
        return await _generate_full_plan(state, escalated=False)
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"Semantic plan generation failed: {exc}",
        }


async def escalate_full_plan(state: BrainState) -> BrainState:
    try:
        result = await _generate_full_plan(state, escalated=True)
        result["compact_semantic_plan"] = state.get("compact_semantic_plan")
        return result
    except Exception as exc:
        return {**state, "status": "PLANNING_FAILURE", "assistant_message": f"Full-plan quality escalation failed: {exc}"}


async def critic(state: BrainState) -> BrainState:
    if state.get("status") != "EXECUTABLE" or not settings.enable_plan_critic:
        return state
    try:
        current_ir = TaskPlanIR.model_validate(state["task_plan_ir"])
        current_draft = _draft_from_ir(current_ir)
        revised = await planner_json(
            TaskPlanDraft,
            critic_messages(current_draft.model_dump(mode="json"), state.get("quality_gate"), state.get("rag_context")),
            retries=1,
            temperature=0.0,
            thinking_token_budget=settings.planner_critic_thinking_budget,
            operation="planner.critic",
        )
        requirements = MissionRequirements.model_validate(state["requirements"])
        revised_ir = _assemble_ir(revised, requirements)
        return {**state, "task_plan_ir": revised_ir.model_dump(mode="json"), "critic_warning": None, "critic_attempted": True}
    except Exception as exc:
        # A critic is optional improvement, not a validator. Never destroy an
        # already schema-valid plan because the critic call failed.
        return {**state, "critic_warning": f"Plan critic failed; retained original draft: {exc}", "critic_attempted": True}


async def validate_ir(state: BrainState) -> BrainState:
    if state.get("status") != "EXECUTABLE" or not state.get("task_plan_ir"):
        return state
    hardening = {"changed": False, "changes": [], "warnings": []}
    grounding = {"changed": False, "changes": [], "warnings": [], "grounded_targets": []}
    location_grounding = {"changed": False, "changes": [], "warnings": [], "bindings": [], "removed_visual_grounding_phases": []}
    contract_norm = {"changed": False, "changes": [], "warnings": []}
    rag_validation = {"valid": True, "errors": [], "used_skill_ids": []}
    rag_utilization = {}
    location_provenance = {"valid": True, "errors": [], "matches": []}
    goal_closure = {"valid": True, "operation": "GENERAL", "issues": []}
    try:
        ir = TaskPlanIR.model_validate(state["task_plan_ir"])
        # v5.3 first canonicalizes harmless legacy planner spellings to the team's
        # formal runtime node/port contract. The original Qwen artifact remains in
        # planner_task_plan_ir for observability.
        contract_norm = normalize_plan_to_runtime_skill_contract(ir)
        ir = contract_norm["ir"]
        # v6.2 resolves exact calibrated named destinations before visual-grounding
        # enforcement. This lets a station/waypoint become NavigateToPoint while
        # preserving visual grounding for movable objects.
        location_grounding = apply_location_grounding_policy(ir, state.get("rag_context"))
        ir = location_grounding["ir"]
        grounding = apply_grounding_policy(ir, state.get("world_state"))
        ir = grounding["ir"]
        # Then apply only narrow, registry-proven robustness hardening before the
        # expensive critic.
        hardening = harden_plan_for_quality(ir)
        ir = hardening["ir"]
        rag_validation = validate_plan_closed_set(ir, state.get("rag_context"))
        rag_utilization = analyze_plan_rag_utilization(ir, state.get("rag_context"), location_policy=location_grounding)
        result = validate_task_plan_ir(ir)
        location_provenance = validate_location_navigation_provenance(ir, state.get("rag_context"))
        if not location_provenance.get("valid"):
            result.setdefault("errors", []).extend(location_provenance.get("errors", []))
            result["valid"] = False
        requirements_for_closure = MissionRequirements.model_validate(state.get("requirements") or {})
        goal_closure = validate_goal_effect_closure(ir, requirements_for_closure)
        if not goal_closure.get("valid"):
            result.setdefault("errors", []).extend(
                f"Mission goal closure: {x}" for x in goal_closure.get("issues", [])
            )
            result["valid"] = False
        if not rag_validation.get("valid"):
            result.setdefault("errors", []).extend(rag_validation.get("errors", []))
            result["valid"] = False
    except Exception as exc:
        result = {"valid": False, "errors": [f"TaskPlanIR schema validation failed: {exc}"], "warnings": []}
        ir = None
    if state.get("critic_warning"):
        result.setdefault("warnings", []).append(str(state["critic_warning"]))
    for warning in contract_norm.get("warnings", []):
        if warning not in result.setdefault("warnings", []):
            result["warnings"].append(warning)
    for warning in location_grounding.get("warnings", []):
        if warning not in result.setdefault("warnings", []):
            result["warnings"].append(warning)
    for warning in grounding.get("warnings", []):
        if warning not in result.setdefault("warnings", []):
            result["warnings"].append(warning)
    for warning in hardening.get("warnings", []):
        if warning not in result.setdefault("warnings", []):
            result["warnings"].append(warning)
    updates: dict[str, Any] = {**state, "ir_validation": result, "rag_validation": rag_validation, "rag_utilization": rag_utilization, "goal_effect_closure": goal_closure, "skill_contract_normalization": {
        "changed": bool(contract_norm.get("changed")),
        "changes": list(contract_norm.get("changes", [])),
        "warnings": list(contract_norm.get("warnings", [])),
        "formal_skill_contract_version": contract_norm.get("formal_skill_contract_version"),
    }, "location_grounding_policy": {
        "changed": bool(location_grounding.get("changed")),
        "changes": list(location_grounding.get("changes", [])),
        "warnings": list(location_grounding.get("warnings", [])),
        "bindings": list(location_grounding.get("bindings", [])),
        "removed_visual_grounding_phases": list(location_grounding.get("removed_visual_grounding_phases", [])),
        "provenance": location_provenance,
    }, "grounding_policy": {
        "changed": bool(grounding.get("changed")),
        "changes": list(grounding.get("changes", [])),
        "warnings": list(grounding.get("warnings", [])),
        "grounded_targets": list(grounding.get("grounded_targets", [])),
    }, "plan_hardening": {
        "changed": bool(hardening.get("changed")),
        "changes": list(hardening.get("changes", [])),
        "warnings": list(hardening.get("warnings", [])),
    }}
    if ir is not None:
        updates["task_plan_ir"] = ir.model_dump(mode="json")
    if state.get("compact_planner_used") and not result.get("valid") and not state.get("compact_fallback_reason"):
        errors = [str(x) for x in result.get("errors", [])]
        updates["compact_fallback_reason"] = "; ".join(errors[:4]) or "Compact plan failed deterministic IR validation."
    return updates


def route_ir(state: BrainState) -> str:
    if state.get("status") != "EXECUTABLE":
        return "finish_failure"
    if state.get("ir_validation", {}).get("valid"):
        return "quality_gate"
    if state.get("compact_planner_used") and not state.get("planner_escalated"):
        return "escalate_full_plan"
    if int(state.get("ir_repair_attempts", 0)) < settings.max_ir_repairs:
        return "repair_ir"
    return "finish_failure"


async def quality_gate_node(state: BrainState) -> BrainState:
    if state.get("status") != "EXECUTABLE" or not state.get("task_plan_ir"):
        return state
    ir = TaskPlanIR.model_validate(state["task_plan_ir"])
    gate = assess_plan_quality(ir, world_state=state.get("world_state"))
    gate_updates: dict[str, Any] = {"quality_gate": gate}
    if state.get("critic_attempted"):
        gate_updates["quality_gate_after_critic"] = gate
    elif not state.get("quality_gate_before_critic"):
        gate_updates["quality_gate_before_critic"] = gate

    # After a critic has already spent a full thinking pass, unresolved critical
    # quality issues become targeted IR-repair errors rather than triggering the
    # critic forever.  This protects quality while keeping the fast path cheap.
    if state.get("critic_attempted") and gate.get("critical_issues"):
        validation = dict(state.get("ir_validation") or {})
        errors = list(validation.get("errors", []))
        errors.extend(f"Quality gate: {x}" for x in gate["critical_issues"] if f"Quality gate: {x}" not in errors)
        validation["errors"] = errors
        validation["valid"] = False
        return {**state, **gate_updates, "ir_validation": validation}
    return {**state, **gate_updates}


def route_quality_gate(state: BrainState) -> str:
    if state.get("status") != "EXECUTABLE":
        return "finish_failure"
    if not state.get("ir_validation", {}).get("valid"):
        if int(state.get("ir_repair_attempts", 0)) < settings.max_ir_repairs:
            return "repair_ir"
        return "finish_failure"

    mode = settings.plan_critic_mode
    if state.get("quality_gate", {}).get("needs_critic") and state.get("compact_planner_used") and not state.get("planner_escalated"):
        return "escalate_full_plan"
    if not settings.enable_plan_critic or mode in {"off", "never", "disabled"}:
        return "compile_bt"
    if state.get("critic_attempted"):
        return "compile_bt"
    if mode == "always":
        return "critic"
    # Default v5 behavior: the expensive critic is an on-demand quality fallback.
    return "critic" if state.get("quality_gate", {}).get("needs_critic") else "compile_bt"


async def repair_ir(state: BrainState) -> BrainState:
    try:
        previous_ir = TaskPlanIR.model_validate(state["task_plan_ir"]) if state.get("task_plan_ir") else None
        previous_draft = _draft_from_ir(previous_ir).model_dump(mode="json") if previous_ir else None
        repaired = await planner_json(
            TaskPlanDraft,
            ir_repair_messages(
                previous_draft,
                state.get("ir_validation", {}).get("errors", []),
                state["message"],
                state["requirements"],
                state.get("rag_context"),
            ),
            retries=1,
            temperature=0.0,
            thinking_token_budget=settings.planner_repair_thinking_budget,
            operation="planner.ir_repair",
        )
        requirements = MissionRequirements.model_validate(state["requirements"])
        ir = _assemble_ir(repaired, requirements)
        ir_json = ir.model_dump(mode="json")
        return {
            **state,
            "planner_task_plan_ir": ir_json,
            "task_plan_ir": ir_json,
            "plan_hardening": {"changed": False, "changes": [], "warnings": []},
            "skill_contract_normalization": {"changed": False, "changes": [], "warnings": []},
            "ir_repair_attempts": int(state.get("ir_repair_attempts", 0)) + 1,
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"TaskPlanIR repair failed: {exc}",
        }


async def _compile_ir_phasewise(ir: TaskPlanIR) -> dict[str, Any]:
    """Compile each semantic phase independently with BTGenBot-2.

    v4.3 demonstrated a characteristic small-model degeneration mode: a multi-phase
    prompt could expand into repeated invented subtrees until max_tokens truncated the
    document. v5 keeps each request inside BTGenBot-2's released short Task+Actions
    distribution and lets deterministic code assemble retry/timeout policy afterwards.
    """
    phase_subtrees: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    fallback_count = 0

    for phase in ir.phases:
        phase_started = perf_counter()
        task = phase_compiler_task(phase)
        actions = phase_actions_text(phase)
        last_text = ""
        last_errors: list[str] = []
        accepted: str | None = None
        attempts_used = 0
        attempt_diagnostics: list[dict[str, Any]] = []

        for attempt in range(settings.compiler_phase_retries + 1):
            attempts_used = attempt + 1
            if attempt == 0:
                messages = compiler_messages(task, actions)
                temperature = settings.compiler_temperature
            else:
                messages = compiler_repair_messages(last_text, last_errors, task, actions)
                temperature = settings.compiler_repair_temperature

            last_text = await compiler_chat(
                messages,
                temperature=temperature,
                max_tokens=settings.compiler_max_output_tokens,
                operation=f"compiler.phase.{phase.id}.attempt_{attempt + 1}",
            )
            prepared = prepare_phase_candidate_xml(last_text, phase)
            if not prepared.get("xml"):
                last_errors = [str(prepared.get("error") or "No usable phase XML was produced.")]
                attempt_diagnostics.append({
                    "attempt": attempt + 1,
                    "temperature": temperature,
                    "sanitization_changes": prepared.get("changes", []),
                    "sanitization_warnings": prepared.get("warnings", []),
                    "errors": list(last_errors),
                })
                continue

            check = validate_phase_candidate(str(prepared["xml"]), phase)
            last_errors = list(check.get("errors", []))
            attempt_diagnostics.append({
                "attempt": attempt + 1,
                "temperature": temperature,
                "sanitization_changes": prepared.get("changes", []),
                "sanitization_warnings": [*prepared.get("warnings", []), *check.get("warnings", [])],
                "errors": list(last_errors),
                "accepted": bool(check.get("valid") and check.get("subtree_xml")),
            })
            if check.get("valid") and check.get("subtree_xml"):
                accepted = str(check["subtree_xml"])
                break

        source = "btgenbot-2"
        if accepted is None:
            if settings.compiler_deterministic_fallback:
                accepted = deterministic_phase_subtree(phase)
                source = "deterministic_fallback"
                fallback_count += 1
            else:
                diagnostics.append(
                    {
                        "phase_id": phase.id,
                        "task": task,
                        "actions": actions,
                        "source": "failed",
                        "attempts": attempts_used,
                        "raw_response": last_text,
                        "errors": last_errors,
                        "attempt_diagnostics": attempt_diagnostics,
                    }
                )
                return {
                    "ok": False,
                    "error": f"BTGenBot-2 could not compile phase '{phase.id}' after bounded attempts.",
                    "phase_subtrees": phase_subtrees,
                    "diagnostics": diagnostics,
                    "fallback_count": fallback_count,
                }

        phase_subtrees[phase.id] = accepted
        diagnostics.append(
            {
                "phase_id": phase.id,
                "task": task,
                "actions": actions,
                "source": source,
                "attempts": attempts_used,
                "raw_response": last_text,
                "errors": last_errors if source == "deterministic_fallback" else [],
                "attempt_diagnostics": attempt_diagnostics,
                "accepted_subtree": accepted,
            }
        )
        record_stage(
            f"compiler_phase:{phase.id}",
            perf_counter() - phase_started,
            {"source": source, "attempts": attempts_used},
        )

    assemble_started = perf_counter()
    assembled_raw = assemble_phase_subtrees(
        ir,
        phase_subtrees,
        load_skill_registry(),
        load_builtin_registry(),
    )
    normalization = normalize_compiled_bt_xml(assembled_raw, ir)
    record_stage("compiler_assemble_normalize", perf_counter() - assemble_started)
    return {
        "ok": bool(normalization.get("xml")),
        "error": normalization.get("error"),
        "assembled_raw_xml": assembled_raw,
        "normalization": normalization,
        "diagnostics": diagnostics,
        "fallback_count": fallback_count,
        "warnings": recovery_compilation_warnings(ir),
    }


async def compile_bt(state: BrainState) -> BrainState:
    if not settings.enable_btgenbot:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": "Hybrid pipeline requires BTGenBot-2, but ENABLE_BTGENBOT=0.",
        }
    try:
        planner_ir = TaskPlanIR.model_validate(state["task_plan_ir"])
        normalized = normalize_task_plan_for_compiler(planner_ir)
        compiler_ir = normalized["ir"]
        result = await _compile_ir_phasewise(compiler_ir)
        compiler_warnings = [
            *normalized.get("warnings", []),
            *result.get("warnings", []),
        ]
        return {
            **state,
            "compiler_task_plan_ir": compiler_ir.model_dump(mode="json"),
            "ir_normalization": {
                "changes": normalized.get("changes", []),
                "warnings": normalized.get("warnings", []),
                "deferred_semantics": normalized.get("deferred_semantics", []),
            },
            "compiler_capabilities": normalized.get("capabilities", compiler_capability_contract()),
            "compiler_task": task_plan_to_compiler_task(compiler_ir),
            "compiler_phases": result.get("diagnostics", []),
            "compiler_warnings": compiler_warnings,
            "compiler_fallback_count": int(result.get("fallback_count", 0)),
            "raw_bt_response": result.get("assembled_raw_xml") or "",
            "bt_normalization": result.get("normalization", {"xml": None, "changes": [], "error": result.get("error")}),
            "bt_xml": (result.get("normalization") or {}).get("xml"),
            "bt_repair_attempts": 0,
            "compiler_error": result.get("error"),
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"BTGenBot-2 phase compilation failed: {exc}",
        }


async def direct_generate(state: BrainState) -> BrainState:
    try:
        text = await planner_chat(
            direct_bt_messages(state["message"], state["requirements"], state.get("world_state"), state.get("rag_context")),
            temperature=0.1,
            thinking_token_budget=settings.planner_direct_thinking_budget,
            operation="planner.direct_bt",
        )
        extracted = extract_bt_xml(text)
        return {
            **state,
            "raw_bt_response": text,
            "bt_xml": normalize_bt_xml(extracted) if extracted else None,
            "bt_repair_attempts": 0,
            "task_plan_ir": None,
            "ir_validation": {},
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"Direct BT generation failed: {exc}",
        }


async def validate_bt(state: BrainState) -> BrainState:
    xml = state.get("bt_xml")
    if not xml:
        result = {
            "valid": False,
            "errors": ["No complete <root>...</root> XML document found."],
            "warnings": [],
        }
    else:
        structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(), load_builtin_registry())
        if state.get("pipeline_mode") == "hybrid" and (state.get("compiler_task_plan_ir") or state.get("task_plan_ir")):
            try:
                semantic_ir = TaskPlanIR.model_validate(state.get("compiler_task_plan_ir") or state["task_plan_ir"])
                semantic = validate_bt_semantics(xml, semantic_ir)
            except Exception as exc:
                semantic = {"valid": False, "errors": [f"Semantic BT validation failed: {exc}"], "warnings": []}
        else:
            semantic = {"valid": True, "errors": [], "warnings": []}
        rag_bt = validate_bt_xml_closed_set(xml, state.get("rag_context"))
        result = {
            "valid": bool(structural.get("valid")) and bool(semantic.get("valid")) and bool(rag_bt.get("valid")),
            "errors": [*structural.get("errors", []), *semantic.get("errors", []), *rag_bt.get("errors", [])],
            "warnings": [*structural.get("warnings", []), *semantic.get("warnings", [])],
            "structural": structural,
            "semantic": semantic,
            "rag_closed_set": rag_bt,
        }
    return {**state, "bt_validation": result, "metrics": bt_metrics(xml, result)}


def route_bt(state: BrainState) -> str:
    if state.get("status") != "EXECUTABLE":
        return "finish_failure"
    if state.get("bt_validation", {}).get("valid"):
        return "finish_success"
    if int(state.get("bt_repair_attempts", 0)) >= settings.max_bt_repairs:
        return "finish_failure"
    return "repair_direct_bt" if state.get("pipeline_mode") == "direct" else "repair_compiled_bt"


async def repair_compiled_bt(state: BrainState) -> BrainState:
    """Re-run bounded phase-wise compilation using the same normalized compiler contract."""
    try:
        if state.get("compiler_task_plan_ir"):
            compiler_ir = TaskPlanIR.model_validate(state["compiler_task_plan_ir"])
            normalized = {
                "ir": compiler_ir,
                "warnings": state.get("ir_normalization", {}).get("warnings", []),
                "changes": state.get("ir_normalization", {}).get("changes", []),
                "deferred_semantics": state.get("ir_normalization", {}).get("deferred_semantics", []),
                "capabilities": state.get("compiler_capabilities") or compiler_capability_contract(),
            }
        else:
            planner_ir = TaskPlanIR.model_validate(state["task_plan_ir"])
            normalized = normalize_task_plan_for_compiler(planner_ir)
            compiler_ir = normalized["ir"]
        result = await _compile_ir_phasewise(compiler_ir)
        return {
            **state,
            "compiler_task_plan_ir": compiler_ir.model_dump(mode="json"),
            "ir_normalization": {
                "changes": normalized.get("changes", []),
                "warnings": normalized.get("warnings", []),
                "deferred_semantics": normalized.get("deferred_semantics", []),
            },
            "compiler_capabilities": normalized.get("capabilities", compiler_capability_contract()),
            "compiler_phases": result.get("diagnostics", []),
            "compiler_warnings": [*normalized.get("warnings", []), *result.get("warnings", [])],
            "compiler_fallback_count": int(result.get("fallback_count", 0)),
            "raw_bt_response": result.get("assembled_raw_xml") or "",
            "bt_normalization": result.get("normalization", {"xml": None, "changes": [], "error": result.get("error")}),
            "bt_xml": (result.get("normalization") or {}).get("xml"),
            "bt_repair_attempts": int(state.get("bt_repair_attempts", 0)) + 1,
            "compiler_error": result.get("error"),
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"BT compiler repair failed: {exc}",
        }


async def repair_direct_bt(state: BrainState) -> BrainState:
    try:
        task = state["message"]
        text = await planner_chat(
            bt_repair_messages(
                state.get("bt_xml") or state.get("raw_bt_response", ""),
                state.get("bt_validation", {}).get("errors", []),
                task,
                state.get("rag_context"),
            ),
            temperature=0.0,
            thinking_token_budget=settings.planner_repair_thinking_budget,
            operation="planner.direct_bt_repair",
        )
        extracted = extract_bt_xml(text)
        return {
            **state,
            "raw_bt_response": text,
            "bt_xml": normalize_bt_xml(extracted) if extracted else None,
            "bt_repair_attempts": int(state.get("bt_repair_attempts", 0)) + 1,
        }
    except Exception as exc:
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": f"Direct BT repair failed: {exc}",
        }


async def finish_dialogue(state: BrainState) -> BrainState:
    return {**state, "bt_xml": None, "task_plan_ir": None}


async def finish_success(state: BrainState) -> BrainState:
    label = "TaskPlanIR → BTGenBot-2" if state.get("pipeline_mode") == "hybrid" else "Direct Qwen → XML"
    return {
        **state,
        "status": "SUCCESS",
        "assistant_message": f"{label} produced a BehaviorTree that passed deterministic validation.",
    }


async def finish_failure(state: BrainState) -> BrainState:
    if state.get("status") == "EXECUTABLE":
        return {
            **state,
            "status": "PLANNING_FAILURE",
            "assistant_message": state.get("assistant_message") or "The planning pipeline exhausted its bounded repair attempts.",
        }
    return state


def _timed_node(name: str, fn):
    """Wrap one LangGraph node with observational stage timing.

    The wrapper must itself be returned to ``StateGraph.add_node``.  The first
    v4.7-timing build accidentally omitted ``return wrapped``, which meant
    ``add_node(name, None)`` and caused LangGraph to raise ``RuntimeError`` at
    application import time.
    """
    async def wrapped(state: BrainState) -> BrainState:
        started = perf_counter()
        details = None
        try:
            result = await fn(state)
            details = {
                "status": result.get("status"),
                "ir_repairs": int(result.get("ir_repair_attempts", 0) or 0),
                "bt_repairs": int(result.get("bt_repair_attempts", 0) or 0),
            }
            return result
        finally:
            # Stage telemetry is observational only and does not alter graph state.
            try:
                record_stage(name, perf_counter() - started, details)
            except Exception:
                pass

    return wrapped


def build_graph():
    g = StateGraph(BrainState)
    for name, fn in {
        "extract_requirements": extract_requirements,
        "retrieve_rag": retrieve_rag_node,
        "capability_check": capability_check_node,
        "generate_ir": generate_ir,
        "escalate_full_plan": escalate_full_plan,
        "critic": critic,
        "validate_ir": validate_ir,
        "quality_gate": quality_gate_node,
        "repair_ir": repair_ir,
        "compile_bt": compile_bt,
        "direct_generate": direct_generate,
        "validate_bt": validate_bt,
        "repair_compiled_bt": repair_compiled_bt,
        "repair_direct_bt": repair_direct_bt,
        "finish_dialogue": finish_dialogue,
        "finish_success": finish_success,
        "finish_failure": finish_failure,
    }.items():
        g.add_node(name, _timed_node(name, fn))

    g.add_edge(START, "extract_requirements")
    g.add_edge("extract_requirements", "retrieve_rag")
    g.add_edge("retrieve_rag", "capability_check")
    g.add_conditional_edges(
        "capability_check",
        route_after_gate,
        {
            "finish_dialogue": "finish_dialogue",
            "direct_generate": "direct_generate",
            "generate_ir": "generate_ir",
        },
    )
    g.add_edge("generate_ir", "validate_ir")
    g.add_edge("critic", "validate_ir")
    g.add_conditional_edges(
        "validate_ir",
        route_ir,
        {"quality_gate": "quality_gate", "escalate_full_plan": "escalate_full_plan", "repair_ir": "repair_ir", "finish_failure": "finish_failure"},
    )
    g.add_conditional_edges(
        "quality_gate",
        route_quality_gate,
        {"critic": "critic", "escalate_full_plan": "escalate_full_plan", "compile_bt": "compile_bt", "repair_ir": "repair_ir", "finish_failure": "finish_failure"},
    )
    g.add_edge("repair_ir", "validate_ir")
    g.add_edge("escalate_full_plan", "validate_ir")
    g.add_edge("compile_bt", "validate_bt")
    g.add_edge("direct_generate", "validate_bt")
    g.add_conditional_edges(
        "validate_bt",
        route_bt,
        {
            "finish_success": "finish_success",
            "finish_failure": "finish_failure",
            "repair_compiled_bt": "repair_compiled_bt",
            "repair_direct_bt": "repair_direct_bt",
        },
    )
    g.add_edge("repair_compiled_bt", "validate_bt")
    g.add_edge("repair_direct_bt", "validate_bt")
    for name in ("finish_dialogue", "finish_success", "finish_failure"):
        g.add_edge(name, END)
    return g.compile()


brain_graph = build_graph()
