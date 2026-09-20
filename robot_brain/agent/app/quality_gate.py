from __future__ import annotations

import math
from typing import Any

from .registry import effective_capabilities, load_skill_registry, skill_map
from .schemas import FailureClassification, RecoveryStrategy, TaskPlanIR

_RETRYABLE = {
    FailureClassification.TRANSIENT,
    FailureClassification.STALE_INFORMATION,
    FailureClassification.ENVIRONMENT_CHANGED,
    FailureClassification.UNKNOWN,
}


def _provides(spec: dict[str, Any], *names: str) -> bool:
    provided = {str(x) for x in (spec.get("provides", []) or [])}
    return any(name in provided for name in names)


def _viewpoint_recovery(phase: Any, skills: dict[str, dict[str, Any]]) -> list[tuple[Any, Any]]:
    out: list[tuple[Any, Any]] = []
    for failure in phase.failure_modes:
        if failure.classification not in _RETRYABLE:
            continue
        for step in failure.recovery:
            if not step.skill:
                continue
            spec = skills.get(step.skill, {})
            if _provides(spec, "change_search_viewpoint", "rotate_in_place"):
                out.append((failure, step))
    return out


def _continuous_patrol(phase: Any, skills: dict[str, dict[str, Any]]) -> list[tuple[Any, Any]]:
    out: list[tuple[Any, Any]] = []
    for failure in phase.failure_modes:
        for step in failure.recovery:
            if not step.skill:
                continue
            spec = skills.get(step.skill, {})
            if (
                str(spec.get("search_policy_role", "")) == "continuous_patrol"
                or _provides(spec, "continuous_search_motion")
            ):
                out.append((failure, step))
    return out


def _rotation_attempts_needed(step: Any) -> int | None:
    try:
        raw = step.arguments.get("angle_deg", step.arguments.get("degrees"))
        degrees = abs(float(raw))
    except Exception:
        return None
    if degrees <= 0:
        return None
    # Search viewpoints at 0, d, 2d, ... . ceil(360/d) gives a bounded full sweep.
    return max(2, min(10, int(math.ceil(360.0 / degrees))))



def _norm_target(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _grounded_targets(world_state: dict[str, Any] | None) -> set[str]:
    ws = world_state or {}
    raw = ws.get("grounded_targets", {})
    out: set[str] = set()
    if isinstance(raw, dict):
        out.update(_norm_target(k) for k, v in raw.items() if v is not False)
    elif isinstance(raw, list):
        out.update(_norm_target(x) for x in raw)
    return {x for x in out if x}


def _target(arguments: dict[str, Any] | None) -> str | None:
    value = (arguments or {}).get("object_name")
    return str(value).strip() if value is not None and str(value).strip() else None


def _grounds_target(phase: Any, skills: dict[str, dict[str, Any]]) -> str | None:
    action_spec = skills.get(phase.nominal_action.skill, {})
    provided = {str(x) for x in (action_spec.get("provides", []) or [])}
    if not provided.intersection({"search_object", "locate_object", "track_object", "refresh_object_tracking"}):
        return None
    t = _target(phase.nominal_action.arguments)
    if not t:
        return None
    for verification in phase.verification:
        vprov = {str(x) for x in (skills.get(verification.condition, {}).get("provides", []) or [])}
        if vprov.intersection({"verify_object_visible", "verify_object_located"}):
            vt = _target(verification.arguments) or t
            if _norm_target(vt) == _norm_target(t):
                return t
    return None

def assess_plan_quality(ir: TaskPlanIR, registry: dict[str, Any] | None = None, world_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic robustness gate used to decide whether the expensive critic is needed.

    This is intentionally stricter than schema validation but narrower than an LLM critic.
    Passing the gate means the plan already contains the high-value safety/robustness
    patterns that the critic was repeatedly adding in v4.x. Failing the gate does not
    silently rewrite the plan: it asks the thinking-mode critic to revise it.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    critical: list[str] = []
    advisories: list[str] = []
    grounded = _grounded_targets(world_state)
    ctx = (world_state or {}).get("conversation_grounding") or {}
    recipient_query = str(ctx.get("recipient_visual_query") or "").strip()
    grasp_seen = False
    latest_grounded_target: str | None = None

    for phase in ir.phases:
        action_spec = skills.get(phase.nominal_action.skill, {})
        provides = effective_capabilities(action_spec, phase.nominal_action.arguments)
        if provides.intersection({"acquire_object", "grasp_object", "pick_object"}):
            grasp_seen = True
        if action_spec.get("requires_grounded_target"):
            nav_target = _target(phase.nominal_action.arguments) or latest_grounded_target
            if not nav_target and len(grounded) == 1:
                nav_target = next(iter(grounded))
            if not nav_target or _norm_target(nav_target) not in grounded:
                critical.append(
                    f"Phase '{phase.id}' navigates to the latest detected object before an explicit target has observable perception grounding. "
                    "Use the bounded VisualizeObject + ReactiveFallback(IsObjectFound, Patrol) search policy first, "
                    "or provide the target explicitly in world_state.grounded_targets."
                )
            if grasp_seen and recipient_query and nav_target and _norm_target(nav_target) != _norm_target(recipient_query):
                critical.append(
                    f"Phase '{phase.id}' post-grasp navigation targets '{nav_target}', but the pending recipient was visually described as '{recipient_query}'. "
                    "Do not replace the recipient with a nearby landmark."
                )
        verification_ids = {v.condition for v in phase.verification}
        recommended = set(action_spec.get("recommended_verification", []) or [])

        # Positive IsObjectHeld is the opposite of a successful release/open-gripper
        # postcondition. Without an explicit negative-condition representation, accepting
        # it can make the ensure-state Fallback skip OpenGripper entirely. Reject this
        # semantic contradiction instead of treating a schema-valid tree as safe.
        if provides.intersection({"release_object", "open_gripper", "gripper_open"}):
            for verification in phase.verification:
                v_spec = skills.get(verification.condition, {})
                v_provides = {str(x) for x in (v_spec.get("provides", []) or [])}
                if v_provides.intersection({"verify_object_held"}):
                    critical.append(
                        f"Phase '{phase.id}' uses positive '{verification.condition}' after '{phase.nominal_action.skill}'. "
                        "That verifies the object is still held, which contradicts release/open-gripper semantics. "
                        "Use an observable release/open condition or an explicit inverted held-condition representation."
                    )

        if action_spec.get("verification_required") and not verification_ids:
            critical.append(
                f"Phase '{phase.id}' action '{phase.nominal_action.skill}' requires observable verification."
            )
        elif recommended and verification_ids.isdisjoint(recommended):
            critical.append(
                f"Phase '{phase.id}' should use one of the recommended verification conditions for "
                f"'{phase.nominal_action.skill}': {sorted(recommended)}."
            )

        possible_failures = set(action_spec.get("possible_failures", []) or [])
        if possible_failures and not phase.failure_modes:
            critical.append(
                f"Phase '{phase.id}' action '{phase.nominal_action.skill}' declares real-world failure modes "
                "but the plan contains no bounded failure policy."
            )

        continuous_patrol = _continuous_patrol(phase, skills)
        for failure in phase.failure_modes:
            if failure.classification in _RETRYABLE and int(failure.max_attempts) < 2 and not continuous_patrol:
                advisories.append(
                    f"Phase '{phase.id}' retryable failure '{failure.failure}' has max_attempts=1."
                )

        # Current live search is a continuous camera query monitored while an opaque
        # Patrol action remains RUNNING. Legacy registries may still use bounded
        # viewpoint-changing recovery.
        if _provides(action_spec, "locate_object", "search_object"):
            if action_spec.get("continuous_search_trigger"):
                if not continuous_patrol:
                    critical.append(
                        f"Phase '{phase.id}' triggers continuous object search but has no registered Patrol activity "
                        "for Timeout(ReactiveFallback(IsObjectFound, Patrol))."
                    )
                if not any(f.timeout_sec and float(f.timeout_sec) > 0 for f, _ in continuous_patrol):
                    critical.append(
                        f"Phase '{phase.id}' continuous search must have a finite timeout whose exhaustion is mission failure."
                    )
            else:
                recoveries = _viewpoint_recovery(phase, skills)
                if not recoveries:
                    critical.append(
                        f"Phase '{phase.id}' searches for an object but has no registered viewpoint-changing "
                        "recovery for a failed/false observation."
                    )
                else:
                    # Compatibility path for older registries with fixed-angle turns.
                    for failure, step in recoveries:
                        needed = _rotation_attempts_needed(step)
                        if needed is None:
                            continue
                        if int(failure.max_attempts) < needed:
                            advisories.append(
                                f"Phase '{phase.id}' viewpoint recovery '{step.skill}' turns "
                                f"{step.arguments.get('angle_deg', step.arguments.get('degrees'))} degrees but max_attempts={failure.max_attempts}; "
                                f"the compiler will safely strengthen the executable search sweep to about {needed} bounded attempts."
                            )

        if phase.stale_dependencies:
            refresh = False
            for failure in phase.failure_modes:
                for step in failure.recovery:
                    strategy = getattr(step.strategy, "value", str(step.strategy))
                    if strategy == RecoveryStrategy.REFRESH_INFORMATION.value:
                        refresh = True
                        break
                if refresh:
                    break
            if not refresh:
                advisories.append(
                    f"Phase '{phase.id}' depends on stale-prone state {phase.stale_dependencies} but has no explicit "
                    "REFRESH_INFORMATION recovery."
                )

        grounded_target = _grounds_target(phase, skills)
        if grounded_target:
            grounded.add(_norm_target(grounded_target))
            latest_grounded_target = grounded_target

    return {
        "pass": not critical,
        "needs_critic": bool(critical),
        "critical_issues": critical,
        "advisories": advisories,
    }
