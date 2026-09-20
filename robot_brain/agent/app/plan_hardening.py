from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .registry import load_skill_registry, skill_map
from .schemas import (
    FailureClassification,
    FailureMode,
    RecoveryStep,
    RecoveryStrategy,
    TaskPlanIR,
)
from .settings import settings

_RETRYABLE = {
    FailureClassification.TRANSIENT,
    FailureClassification.STALE_INFORMATION,
    FailureClassification.ENVIRONMENT_CHANGED,
    FailureClassification.UNKNOWN,
}


def _provides(spec: dict[str, Any], *names: str) -> bool:
    provided = {str(x) for x in (spec.get("provides", []) or [])}
    return any(name in provided for name in names)


def _search_phase(phase: Any, skills: dict[str, dict[str, Any]]) -> bool:
    return _provides(skills.get(phase.nominal_action.skill, {}), "locate_object", "search_object")


def _existing_viewpoint_recovery(phase: Any, skills: dict[str, dict[str, Any]]) -> bool:
    for failure in phase.failure_modes:
        if failure.classification not in _RETRYABLE:
            continue
        for step in failure.recovery:
            if step.skill and _provides(skills.get(step.skill, {}), "change_search_viewpoint", "rotate_in_place"):
                return True
    return False


def _is_continuous_patrol(spec: dict[str, Any]) -> bool:
    return (
        str(spec.get("search_policy_role", "")) == "continuous_patrol"
        or _provides(spec, "continuous_search_motion")
    )


def _existing_continuous_patrol(phase: Any, skills: dict[str, dict[str, Any]]) -> bool:
    return any(
        step.skill and _is_continuous_patrol(skills.get(step.skill, {}))
        for failure in phase.failure_modes
        for step in failure.recovery
    )


def _unique_continuous_patrol_skill(skills: dict[str, dict[str, Any]]) -> str | None:
    candidates = [
        skill_id for skill_id, spec in skills.items()
        if str(spec.get("kind", "")).upper() == "ACTION" and _is_continuous_patrol(spec)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _continuous_search_timeout(action_spec: dict[str, Any]) -> float:
    policy = action_spec.get("search_policy", {}) or {}
    timeout = policy.get("timeout_sec") or (action_spec.get("timeout", {}) or {}).get("recommended_sec") or 60
    return float(timeout)


def _harden_continuous_patrol_search(
    ir: TaskPlanIR,
    skills: dict[str, dict[str, Any]],
    patrol_skill: str,
) -> tuple[TaskPlanIR, list[str], list[str]]:
    changes: list[str] = []
    warnings: list[str] = []
    for phase in ir.phases:
        action_spec = skills.get(phase.nominal_action.skill, {})
        if not _search_phase(phase, skills):
            continue

        # Remove obsolete rotate-and-retry search recovery. Other recovery metadata
        # is preserved because it may describe transport/perception faults.
        for failure in phase.failure_modes:
            kept = [
                step for step in failure.recovery
                if not (step.skill and _provides(skills.get(step.skill, {}), "change_search_viewpoint", "rotate_in_place"))
            ]
            if len(kept) != len(failure.recovery):
                failure.recovery = kept
                changes.append(f"Phase '{phase.id}' removed obsolete rotate-and-retry search recovery.")

        if _existing_continuous_patrol(phase, skills):
            for failure in phase.failure_modes:
                if any(step.skill and _is_continuous_patrol(skills.get(step.skill, {})) for step in failure.recovery):
                    old_policy = (failure.classification, int(failure.max_attempts), failure.timeout_sec, failure.on_exhaustion)
                    failure.classification = FailureClassification.STALE_INFORMATION
                    failure.max_attempts = 1
                    failure.timeout_sec = _continuous_search_timeout(action_spec)
                    failure.on_exhaustion = "MISSION_FAILURE"
                    if old_policy != (failure.classification, int(failure.max_attempts), failure.timeout_sec, failure.on_exhaustion):
                        changes.append(
                            f"Phase '{phase.id}' normalized continuous Patrol search to max_attempts=1, "
                            f"timeout_sec={failure.timeout_sec:g}, on_exhaustion=MISSION_FAILURE."
                        )
            continue

        target_failure = next(
            (f for f in phase.failure_modes if str(f.failure) == "OBJECT_NOT_FOUND"),
            _pick_retryable_failure(phase),
        )
        if target_failure is None:
            existing_names = {str(f.failure) for f in phase.failure_modes}
            target_failure = FailureMode(
                failure=_default_failure_name(action_spec, existing_names),
                classification=FailureClassification.STALE_INFORMATION,
                recovery=[],
                max_attempts=1,
                timeout_sec=_continuous_search_timeout(action_spec),
                escalation=None,
                on_exhaustion="MISSION_FAILURE",
            )
            phase.failure_modes.append(target_failure)

        target_failure.recovery.append(
            RecoveryStep(
                strategy=RecoveryStrategy.REFRESH_INFORMATION,
                skill=patrol_skill,
                arguments={},
            )
        )
        target_failure.classification = FailureClassification.STALE_INFORMATION
        target_failure.max_attempts = 1
        target_failure.timeout_sec = _continuous_search_timeout(action_spec)
        target_failure.on_exhaustion = "MISSION_FAILURE"
        changes.append(
            f"Phase '{phase.id}' now uses {patrol_skill} as the opaque long-running activity in "
            "Timeout(ReactiveFallback(IsObjectFound, Patrol))."
        )
        warnings.append(
            f"Phase '{phase.id}' search timeout is {target_failure.timeout_sec:g}s; timeout returns FAILURE, "
            "halts Patrol, and prevents later mission steps from running."
        )
    return ir, changes, warnings


def _unique_viewpoint_skill(skills: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    candidates: list[tuple[str, dict[str, Any]]] = []
    for skill_id, spec in skills.items():
        if str(spec.get("kind", "")).upper() != "ACTION":
            continue
        if _provides(spec, "change_search_viewpoint", "rotate_in_place"):
            candidates.append((skill_id, spec))
    return candidates[0] if len(candidates) == 1 else None


def _viewpoint_arguments(spec: dict[str, Any]) -> dict[str, Any] | None:
    inputs = spec.get("inputs", {}) or {}
    required = {name for name, port in inputs.items() if isinstance(port, dict) and port.get("required")}
    profile = spec.get("recovery_profile", {}) or {}
    angle_port = str(profile.get("angle_port") or "")
    if angle_port and angle_port in inputs and not (required - {angle_port}):
        return {angle_port: float(settings.auto_search_recovery_degrees)}
    for candidate in ("angle_deg", "degrees"):
        if candidate in inputs and not (required - {candidate}):
            return {candidate: float(settings.auto_search_recovery_degrees)}
    # Do not guess how to call a more complicated viewpoint skill.
    return None


def _rotation_degrees(arguments: dict[str, Any]) -> float:
    for key in ("angle_deg", "degrees"):
        if key in arguments:
            return float(arguments[key])
    raise KeyError("viewpoint recovery has no angle_deg/degrees argument")


def _attempts_for_degrees(degrees: float) -> int:
    d = abs(float(degrees))
    if d <= 0:
        return 2
    return max(2, min(settings.max_policy_attempts, int(math.ceil(360.0 / d))))


def _pick_retryable_failure(phase: Any) -> Any | None:
    for failure in phase.failure_modes:
        if failure.classification in _RETRYABLE:
            return failure
    return None


def _default_failure_name(action_spec: dict[str, Any], existing: set[str]) -> str:
    for failure in action_spec.get("possible_failures", []) or []:
        name = str(failure)
        if name not in existing:
            return name
    base = "OBSERVATION_NOT_SATISFIED"
    if base not in existing:
        return base
    idx = 2
    while f"{base}_{idx}" in existing:
        idx += 1
    return f"{base}_{idx}"


def harden_plan_for_quality(
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply only high-confidence, deterministic robustness hardening.

    Current registries deterministically attach the single declared continuous Patrol
    activity to search phases. Legacy/demo registries without that role retain the old
    bounded viewpoint-recovery behavior for compatibility.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    hardened = ir.model_copy(deep=True)
    changes: list[str] = []
    warnings: list[str] = []
    applied = False

    if not settings.plan_auto_harden_search_recovery:
        return {
            "ir": hardened,
            "changed": False,
            "changes": [],
            "warnings": ["Deterministic search-recovery hardening is disabled by settings."],
        }

    patrol_skill = _unique_continuous_patrol_skill(skills)
    if patrol_skill:
        hardened, changes, warnings = _harden_continuous_patrol_search(hardened, skills, patrol_skill)
        return {
            "ir": hardened,
            "changed": bool(changes),
            "changes": changes,
            "warnings": warnings,
        }

    viewpoint = _unique_viewpoint_skill(skills)
    if viewpoint is None:
        # Ambiguity is intentionally not resolved by guessing.
        return {
            "ir": hardened,
            "changed": False,
            "changes": [],
            "warnings": [
                "Search-recovery auto-hardening was not applied because the SkillManifest does not expose exactly one viewpoint-changing ACTION."
            ],
        }

    recovery_skill, recovery_spec = viewpoint
    recovery_args = _viewpoint_arguments(recovery_spec)
    if recovery_args is None:
        return {
            "ir": hardened,
            "changed": False,
            "changes": [],
            "warnings": [
                f"Search-recovery auto-hardening was not applied because '{recovery_skill}' requires arguments that cannot be filled deterministically."
            ],
        }

    needed_attempts = _attempts_for_degrees(_rotation_degrees(recovery_args))

    for phase in hardened.phases:
        action_spec = skills.get(phase.nominal_action.skill, {})
        if not _search_phase(phase, skills) or _existing_viewpoint_recovery(phase, skills):
            continue

        target_failure = _pick_retryable_failure(phase)
        created_failure = False
        if target_failure is None:
            existing_names = {str(f.failure) for f in phase.failure_modes}
            timeout = ((action_spec.get("timeout") or {}).get("recommended_sec"))
            target_failure = FailureMode(
                failure=_default_failure_name(action_spec, existing_names),
                classification=FailureClassification.STALE_INFORMATION,
                recovery=[],
                max_attempts=needed_attempts,
                timeout_sec=float(timeout) if timeout else None,
                escalation=None,
                on_exhaustion="MISSION_FAILURE",
            )
            phase.failure_modes.append(target_failure)
            created_failure = True

        target_failure.recovery.append(
            RecoveryStep(
                strategy=RecoveryStrategy.REFRESH_INFORMATION,
                skill=recovery_skill,
                arguments=deepcopy(recovery_args),
            )
        )
        old_attempts = int(target_failure.max_attempts)
        if old_attempts < needed_attempts:
            target_failure.max_attempts = needed_attempts

        applied = True
        changes.append(
            f"Phase '{phase.id}' auto-added observable search recovery {recovery_skill}({recovery_args}) "
            f"to failure '{target_failure.failure}'."
        )
        if old_attempts < needed_attempts:
            changes.append(
                f"Phase '{phase.id}' search retry bound strengthened {old_attempts}->{needed_attempts} "
                "for an approximately full bounded viewpoint sweep."
            )
        if created_failure:
            warnings.append(
                f"Phase '{phase.id}' had no retryable search failure policy; deterministic hardening created a bounded "
                f"'{target_failure.failure}' policy so a false observation changes viewpoint before retry."
            )
        else:
            warnings.append(
                f"Phase '{phase.id}' omitted a viewpoint-changing search recovery. Deterministic hardening inserted the unique "
                f"registered recovery '{recovery_skill}' before the critic to avoid spending a full LLM pass on a deterministic robustness fix."
            )

    return {
        "ir": hardened,
        "changed": applied,
        "changes": changes,
        "warnings": warnings,
    }
