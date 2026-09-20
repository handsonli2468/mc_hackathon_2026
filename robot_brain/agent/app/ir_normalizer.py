from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .registry import load_skill_registry, skill_map
from .schemas import TaskPlanIR


# This is an explicit declaration of what the current TaskPlanIR -> BT compiler
# actually preserves as executable BehaviorTree semantics. Keeping it machine-
# readable prevents the planner/compiler/validator from silently disagreeing.
_COMPILER_CAPABILITIES: dict[str, Any] = {
    "contract_version": "1.4",
    "action_verification": True,
    "bounded_retry": True,
    "timeout": True,
    "formal_bt_engine_contract": True,
    "team_recovery_node": False,
    "named_object_literal_contract": True,
    "observable_search_recovery": True,
    "continuous_perception_patrol_search": True,
    "observable_ensure_state_guard": True,
    "failure_specific_recovery": False,
    "phase_cleanup_execution": False,
    "notes": {
        "formal_bt_engine_contract": (
            "Generated custom node IDs and ports are aligned with the team's exported TreeNodesModel."
        ),
        "team_recovery_node": (
            "The current ABI has no RecoveryNode; current continuous search uses standard Sequence, Timeout, and ReactiveFallback nodes."
        ),
        "named_object_literal_contract": (
            "The formal runtime nodes consume std::string object_name literals; no synthetic TrackedObject blackboard output is invented."
        ),
        "observable_search_recovery": (
            "Legacy registries may compile bounded viewpoint-changing recovery; the current live registry uses continuous patrol search."
        ),
        "continuous_perception_patrol_search": (
            "VisualizeObject triggers the camera once, then Timeout(ReactiveFallback(IsObjectFound, Patrol)) monitors the continuous query. "
            "A timeout halts Patrol, returns FAILURE, and stops the mission Sequence before downstream actions."
        ),
        "observable_ensure_state_guard": (
            "When a verification condition can be evaluated using state guaranteed before a phase action, "
            "the compiler may emit an IF/ELSE Fallback that skips the mutating action if the desired state is already true."
        ),
        "failure_specific_recovery": (
            "Recovery that depends on a specific runtime error code remains semantic metadata until the runtime "
            "skill contract exposes observable failure-discriminator conditions for safe branch selection."
        ),
        "phase_cleanup_execution": (
            "Phase cleanup remains semantic metadata until interruption/halt semantics are "
            "defined and compiled explicitly."
        ),
    },
}


def compiler_capability_contract() -> dict[str, Any]:
    return deepcopy(_COMPILER_CAPABILITIES)


def normalize_task_plan_for_compiler(
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the executable compiler-facing IR without changing the planner artifact.

    The planner-facing TaskPlanIR is retained verbatim for observability. Fields that
    the current BT compiler cannot faithfully execute are deterministically deferred
    instead of being inconsistently ignored by the compiler but required by the BT
    semantic validator.

    v6.5 compiler contract:
      * nominal action + verification: executable
      * bounded retry / timeout: executable and compiled deterministically
      * continuous perception + patrol search: executable
      * legacy observable search/viewpoint recovery: executable
      * observable ensure-state IF/ELSE guards: executable when inputs exist before the phase
      * failure-specific error-code recovery: metadata-only for now
      * phase.cleanup: metadata-only for now

    A cleanup skill must additionally opt in with ``cleanup_eligible: true`` in its
    SkillManifest before it can ever become executable cleanup in a future compiler.
    Operational skills placed in cleanup are retained only in the original IR and are
    explicitly diagnosed; they are never silently executed after mission success.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    normalized = ir.model_copy(deep=True)
    changes: list[str] = []
    warnings: list[str] = []
    deferred: list[dict[str, Any]] = []

    # Deterministic robustness hardening for observable search recovery.  The planner
    # artifact stays untouched, while the executable compiler IR may increase a too-small
    # retry bound so fixed-angle viewpoint changes cover an approximately full circle.
    # This is finite (<= schema max 10) and explicitly recorded below.
    for phase in normalized.phases:
        action_spec = skills.get(phase.nominal_action.skill, {})
        action_provides = {str(x) for x in (action_spec.get("provides", []) or [])}
        if not action_provides.intersection({"locate_object", "search_object"}):
            continue
        for failure in phase.failure_modes:
            for recovery in failure.recovery:
                if not recovery.skill:
                    continue
                recovery_spec = skills.get(recovery.skill, {})
                provides = {str(x) for x in (recovery_spec.get("provides", []) or [])}
                if not provides.intersection({"change_search_viewpoint", "rotate_in_place"}):
                    continue
                try:
                    degrees = abs(float(recovery.arguments.get("angle_deg", recovery.arguments.get("degrees"))))
                except Exception:
                    continue
                if degrees <= 0:
                    continue
                needed = max(2, min(10, int(math.ceil(360.0 / degrees))))
                if int(failure.max_attempts) < needed:
                    old_attempts = int(failure.max_attempts)
                    failure.max_attempts = needed
                    changes.append(
                        f"Phase '{phase.id}' search sweep strengthened max_attempts {old_attempts}->{needed} "
                        f"for {recovery.skill} {degrees:g} degree viewpoint steps."
                    )
                    warnings.append(
                        f"Phase '{phase.id}' planner retry bound was too small for a useful bounded viewpoint sweep; "
                        f"compiler IR uses max_attempts={needed} while the original TaskPlanIR is preserved unchanged."
                    )

    for phase in normalized.phases:
        if not phase.cleanup:
            continue

        original_cleanup = list(phase.cleanup)
        phase.cleanup = []
        for idx, cleanup in enumerate(original_cleanup, 1):
            spec = skills.get(cleanup.skill, {})
            eligible = bool(spec.get("cleanup_eligible", False))
            if eligible:
                reason = (
                    "skill is cleanup-eligible, but the current compiler contract does not "
                    "support executable phase cleanup / halt semantics"
                )
            else:
                reason = (
                    "skill is not declared cleanup_eligible in SkillManifest and is treated as "
                    "operational/recovery behavior rather than executable cleanup"
                )

            deferred.append(
                {
                    "phase_id": phase.id,
                    "field": f"cleanup[{idx - 1}]",
                    "skill": cleanup.skill,
                    "arguments": deepcopy(cleanup.arguments),
                    "cleanup_eligible": eligible,
                    "reason": reason,
                }
            )
            changes.append(
                f"Deferred phase '{phase.id}' cleanup skill '{cleanup.skill}' from executable compiler IR."
            )
            warnings.append(
                f"Phase '{phase.id}' cleanup skill '{cleanup.skill}' was preserved in the original TaskPlanIR "
                f"but omitted from executable BT compilation because {reason}."
            )

    return {
        "ir": normalized,
        "compiler_ir": normalized.model_dump(mode="json"),
        "capabilities": compiler_capability_contract(),
        "changes": changes,
        "warnings": warnings,
        "deferred_semantics": deferred,
    }
