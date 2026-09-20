from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .registry import load_skill_registry, skill_map
from .schemas import (
    ActionSpec,
    CompactSemanticPlan,
    FailureClassification,
    FailureMode,
    Goal,
    MissionRequirements,
    MissionStatus,
    Phase,
    TaskPlanIR,
    TerminationPolicy,
    VerificationSpec,
)


def _safe_id(text: str, fallback: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_").lower()
    return out or fallback


def _failure_classification(name: str) -> FailureClassification:
    n = str(name).upper()
    if n in {"OBJECT_NOT_FOUND", "OBJECT_NOT_VISIBLE", "TRACKING_LOST"}:
        return FailureClassification.STALE_INFORMATION
    if n in {"OBJECT_MOVED", "NO_PATH"}:
        return FailureClassification.ENVIRONMENT_CHANGED
    if n in {
        "PERCEPTION_UNAVAILABLE",
        "NAVIGATION_TIMEOUT",
        "NO_VALID_GRASP",
        "EXECUTION_FAILED",
        "ROTATION_BLOCKED",
        "OBJECT_NOT_HELD",
        "NOT_AT_OBJECT",
    }:
        return FailureClassification.TRANSIENT
    return FailureClassification.UNKNOWN


def _default_attempts(skill_id: str, failure: str) -> int:
    sid = str(skill_id)
    f = str(failure).upper()
    if sid in {"VisualizeObject", "TrackObject"} or f in {"OBJECT_NOT_FOUND", "OBJECT_NOT_VISIBLE", "TRACKING_LOST"}:
        return 3
    if f in {"NO_VALID_GRASP", "OBJECT_NOT_HELD"}:
        return 3
    return 2


def _same_named_object_args(action_args: dict[str, Any], condition_spec: dict[str, Any]) -> dict[str, Any]:
    inputs = condition_spec.get("inputs", {}) or {}
    if "object_name" in inputs and "object_name" in action_args:
        return {"object_name": action_args["object_name"]}
    out: dict[str, Any] = {}
    for key in inputs:
        if key in action_args:
            out[key] = action_args[key]
    return out


def _phase_from_step(step: Any, index: int, skills: dict[str, dict[str, Any]]) -> Phase:
    sid = str(step.action)
    spec = skills.get(sid)
    if spec is None:
        # Keep the unknown skill visible so the deterministic IR validator can reject
        # it cleanly instead of silently substituting another robot ability.
        spec = {}
    args = deepcopy(step.arguments or {})
    recommended = list(spec.get("recommended_verification", []) or [])
    verification: list[VerificationSpec] = []
    if recommended:
        cond_id = str(recommended[0])
        cond_spec = skills.get(cond_id, {})
        verification.append(VerificationSpec(condition=cond_id, arguments=_same_named_object_args(args, cond_spec)))

    desired_state: list[str] = []
    for condition in verification:
        desired_state.extend(str(x) for x in (skills.get(condition.condition, {}).get("guarantees", []) or []))
    if not desired_state:
        desired_state.extend(str(x) for x in (spec.get("success_semantics", []) or []))

    timeout = (spec.get("timeout", {}) or {}).get("recommended_sec")
    failure_modes: list[FailureMode] = []
    for failure in spec.get("possible_failures", []) or []:
        failure_modes.append(
            FailureMode(
                failure=str(failure),
                classification=_failure_classification(str(failure)),
                recovery=[],
                max_attempts=_default_attempts(sid, str(failure)),
                timeout_sec=float(timeout) if timeout else None,
                escalation=None,
                on_exhaustion="MISSION_FAILURE",
            )
        )

    step_id = _safe_id(step.id, f"phase_{index + 1}")
    objective = str(step.objective or "").strip()
    if not objective:
        target = args.get("object_name")
        objective = f"Run {sid}" + (f" for {target}" if target else "") + "."

    return Phase(
        id=step_id,
        objective=objective,
        desired_state=list(dict.fromkeys(desired_state)),
        preconditions=[str(x) for x in (spec.get("preconditions", []) or [])],
        nominal_action=ActionSpec(skill=sid, arguments=args),
        verification=verification,
        failure_modes=failure_modes,
        stale_dependencies=[str(x) for x in (spec.get("stale_information", []) or [])],
        side_effects=[str(x) for x in (spec.get("side_effects", []) or [])],
        resource_requirements=[str(x) for x in (spec.get("resources", []) or [])],
        cleanup=[],
    )



def build_phase_for_action(
    phase_id: str,
    skill_id: str,
    arguments: dict[str, Any],
    objective: str = "",
    registry: dict[str, Any] | None = None,
) -> Phase:
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    class _Step:
        id = phase_id
        action = skill_id
        def __init__(self):
            self.arguments = arguments
            self.objective = objective
    return _phase_from_step(_Step(), 0, skills)


def normalize_compact_plan(
    plan: CompactSemanticPlan,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize harmless compact-planner output before full-IR enrichment.

    The compact planner is supposed to emit ACTION steps only because verification
    CONDITIONS are injected deterministically from the SkillManifest. Small language
    models occasionally append a condition such as ``IsObjectHeld`` as an explicit
    final step. Treat that as redundant only when it is exactly the recommended
    verification for the immediately preceding action with compatible arguments.

    Any other condition step is rejected so the normal quality escalation path can
    still protect plan semantics.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    out = plan.model_copy(deep=True)
    kept = []
    changes: list[str] = []
    warnings: list[str] = []

    for step in out.steps:
        sid = str(step.action)
        spec = skills.get(sid)
        if spec is None:
            kept.append(step)
            continue
        kind = str(spec.get("kind", "")).upper()
        if kind != "CONDITION":
            kept.append(step)
            continue

        if not kept:
            raise ValueError(f"Compact plan starts with CONDITION '{sid}'; ACTION steps are required.")

        previous = kept[-1]
        previous_spec = skills.get(str(previous.action), {}) or {}
        recommended = [str(x) for x in (previous_spec.get("recommended_verification", []) or [])]
        previous_args = dict(previous.arguments or {})
        condition_args = dict(step.arguments or {})
        compatible = True
        for key, value in condition_args.items():
            if key in previous_args and previous_args[key] != value:
                compatible = False
                break

        if sid in recommended and compatible:
            changes.append(
                f"Dropped redundant compact verification step '{step.id}' ({sid}); "
                f"verification is injected from SkillManifest for preceding action '{previous.action}'."
            )
            continue

        raise ValueError(
            f"Compact plan emitted CONDITION '{sid}' as an executable step. "
            "Only ACTION skills belong in CompactSemanticPlan.steps."
        )

    if not kept:
        raise ValueError("Compact plan contains no executable ACTION steps after normalization.")
    out.steps = kept
    return {
        "plan": out,
        "changed": bool(changes),
        "changes": changes,
        "warnings": warnings,
    }


def enrich_compact_plan(
    plan: CompactSemanticPlan,
    requirements: MissionRequirements,
    registry: dict[str, Any] | None = None,
) -> TaskPlanIR:
    """Expand a short semantic plan into the full TaskPlanIR contract.

    The LLM chooses semantic targets and an ordered action sequence. All repetitive
    contract material (verification, failure metadata, timeouts, resources and
    side-effects) comes from the active SkillManifest so it is consistent across runs.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    phases = [_phase_from_step(step, i, skills) for i, step in enumerate(plan.steps)]

    success_conditions = list(dict.fromkeys([
        *[str(x) for x in requirements.success_conditions],
        *[str(x) for x in plan.success_conditions],
    ]))
    goal = Goal(description=plan.goal_description, success_conditions=success_conditions)
    termination = TerminationPolicy(
        success_conditions=success_conditions,
        failure_conditions=["A bounded phase or recovery policy is exhausted."],
        max_mission_duration_sec=300,
        on_unrecoverable_failure="MISSION_FAILURE",
    )
    return TaskPlanIR(
        schema_version="1.0",
        mission_status=MissionStatus.EXECUTABLE,
        goal=goal,
        required_capabilities=list(dict.fromkeys(requirements.required_capabilities)),
        missing_capabilities=[],
        required_user_information=[],
        task_semantics=requirements.task_semantics,
        retrieval_sketch=requirements.retrieval_sketch,
        assumptions=list(dict.fromkeys([
            *[str(x) for x in requirements.assumptions],
            *[str(x) for x in plan.assumptions],
        ])),
        global_constraints=list(dict.fromkeys(str(x) for x in plan.global_constraints)),
        termination_policy=termination,
        phases=phases,
    )
