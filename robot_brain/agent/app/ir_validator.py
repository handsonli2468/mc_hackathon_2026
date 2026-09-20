from __future__ import annotations

from typing import Any

from .registry import check_capabilities, load_skill_registry, skill_map
from .schemas import ActionSpec, TaskPlanIR, MissionStatus, VerificationSpec, FailureClassification
from .settings import settings
from .ir_normalizer import compiler_capability_contract


def _required_inputs(skill: dict[str, Any]) -> set[str]:
    return {k for k, v in (skill.get("inputs", {}) or {}).items() if isinstance(v, dict) and v.get("required")}


def _check_arguments(owner: str, skill_id: str, arguments: dict[str, Any], skill: dict[str, Any], errors: list[str]) -> None:
    inputs = skill.get("inputs", {}) or {}
    missing = _required_inputs(skill) - set(arguments)
    if missing:
        errors.append(f"{owner} skill '{skill_id}' is missing arguments: {sorted(missing)}")
    declared = set(inputs.keys())
    extra = set(arguments) - declared
    if extra:
        errors.append(f"{owner} skill '{skill_id}' uses undeclared arguments: {sorted(extra)}")
    for name, spec in inputs.items():
        if name not in arguments or not isinstance(spec, dict):
            continue
        value = arguments[name]
        type_name = str(spec.get("type") or "").lower()
        if type_name in {"int", "integer", "unsigned int", "uint", "uint32_t"}:
            try:
                parsed_int = int(str(value))
                if str(value).strip() not in {str(parsed_int), f"+{parsed_int}"}:
                    raise ValueError
                if type_name in {"unsigned int", "uint", "uint32_t"} and parsed_int < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{owner} skill '{skill_id}' argument '{name}' must be an integer; got {value!r}.")
                continue
        if spec.get("literal_only"):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{owner} skill '{skill_id}' argument '{name}' must be a non-empty literal string.")
                continue
            text = value.strip()
            if "{{" in text or "}}" in text or ".outputs." in text or ".nominal_action." in text or (text.startswith("{") and text.endswith("}")):
                errors.append(
                    f"{owner} skill '{skill_id}' argument '{name}' must be a runtime literal name, not a planner/blackboard reference: {value!r}."
                )
        allowed = spec.get("allowed_values") or spec.get("enum") or []
        if allowed and str(value) not in {str(x) for x in allowed}:
            errors.append(
                f"{owner} skill '{skill_id}' argument '{name}' must be one of {list(map(str, allowed))}; got {value!r}."
            )
        if spec.get("min") is not None or spec.get("max") is not None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"{owner} skill '{skill_id}' argument '{name}' must be numeric; got {value!r}.")
                continue
            if spec.get("min") is not None and number < float(spec["min"]):
                errors.append(f"{owner} skill '{skill_id}' argument '{name}' is below minimum {spec['min']}.")
            if spec.get("max") is not None and number > float(spec["max"]):
                errors.append(f"{owner} skill '{skill_id}' argument '{name}' exceeds maximum {spec['max']}.")


def validate_task_plan_ir(ir: TaskPlanIR, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    errors: list[str] = []
    warnings: list[str] = []

    if ir.mission_status != MissionStatus.EXECUTABLE:
        errors.append(f"TaskPlanIR produced after capability gating must be EXECUTABLE, got {ir.mission_status.value}.")
    if ir.missing_capabilities:
        errors.append("Executable TaskPlanIR must not contain missing_capabilities.")
    if ir.required_user_information:
        errors.append("Executable TaskPlanIR must not contain required_user_information.")
    if not ir.phases:
        errors.append("Executable TaskPlanIR must contain at least one phase.")

    cap = check_capabilities(ir.required_capabilities, registry)
    if cap["missing"]:
        errors.append("TaskPlanIR requires unavailable capabilities: " + ", ".join(cap["missing"]))

    phase_ids = [p.id for p in ir.phases]
    if len(phase_ids) != len(set(phase_ids)):
        errors.append("Phase IDs must be unique.")

    if not ir.termination_policy.success_conditions:
        errors.append("Termination policy must contain observable success conditions.")
    if ir.termination_policy.max_mission_duration_sec is None:
        warnings.append("Termination policy has no max_mission_duration_sec; consider a finite mission-level timeout.")

    for phase in ir.phases:
        action = skills.get(phase.nominal_action.skill)
        if action is None:
            errors.append(f"Phase '{phase.id}' uses unknown action skill '{phase.nominal_action.skill}'.")
        elif str(action.get("kind", "")).upper() != "ACTION":
            errors.append(f"Phase '{phase.id}' nominal_action '{phase.nominal_action.skill}' is not an ACTION skill.")
        else:
            _check_arguments(
                f"Phase '{phase.id}' nominal action",
                phase.nominal_action.skill,
                phase.nominal_action.arguments,
                action,
                errors,
            )
            recommended = set(action.get("recommended_verification", []) or [])
            actual = {v.condition for v in phase.verification}
            if action.get("verification_required") and not actual:
                errors.append(f"Phase '{phase.id}' action '{phase.nominal_action.skill}' requires observable verification.")
            elif recommended and actual.isdisjoint(recommended):
                warnings.append(
                    f"Phase '{phase.id}' does not use recommended verification for '{phase.nominal_action.skill}': {sorted(recommended)}"
                )

        for verification in phase.verification:
            spec = skills.get(verification.condition)
            if spec is None:
                errors.append(f"Phase '{phase.id}' uses unknown verification condition '{verification.condition}'.")
            elif str(spec.get("kind", "")).upper() != "CONDITION":
                errors.append(f"Phase '{phase.id}' verification '{verification.condition}' is not a CONDITION skill.")
            else:
                _check_arguments(
                    f"Phase '{phase.id}' verification",
                    verification.condition,
                    verification.arguments,
                    spec,
                    errors,
                )

        for failure in phase.failure_modes:
            if failure.max_attempts > settings.max_policy_attempts:
                errors.append(
                    f"Phase '{phase.id}' failure '{failure.failure}' exceeds max allowed attempts {settings.max_policy_attempts}."
                )
            recovery_strategies = {getattr(r.strategy, "value", str(r.strategy)) for r in failure.recovery}
            classification_value = getattr(failure.classification, "value", str(failure.classification))
            if classification_value in {"PERMANENT", "CAPABILITY_LIMIT", "SAFETY"}:
                if failure.max_attempts > 1 or recovery_strategies.intersection({"RETRY", "REFRESH_INFORMATION", "LOCAL_ADJUSTMENT", "REPLAN_LOCAL"}):
                    errors.append(
                        f"Phase '{phase.id}' failure '{failure.failure}' is classified {classification_value} "
                        "but also requests retry/recovery that assumes the outcome can change. Reclassify it as "
                        "TRANSIENT/STALE_INFORMATION/ENVIRONMENT_CHANGED when bounded recovery is meaningful, or remove retry recovery."
                    )
            for recovery in failure.recovery:
                if recovery.skill:
                    spec = skills.get(recovery.skill)
                    if spec is None:
                        errors.append(f"Phase '{phase.id}' recovery uses unknown skill '{recovery.skill}'.")
                    else:
                        _check_arguments(
                            f"Phase '{phase.id}' recovery",
                            recovery.skill,
                            recovery.arguments,
                            spec,
                            errors,
                        )

        compiler_caps = compiler_capability_contract()
        for cleanup in phase.cleanup:
            spec = skills.get(cleanup.skill)
            if spec is None:
                errors.append(f"Phase '{phase.id}' cleanup uses unknown skill '{cleanup.skill}'.")
                continue
            if str(spec.get("kind", "")).upper() != "ACTION":
                errors.append(f"Phase '{phase.id}' cleanup '{cleanup.skill}' must be an ACTION skill.")
                continue
            _check_arguments(f"Phase '{phase.id}' cleanup", cleanup.skill, cleanup.arguments, spec, errors)
            if not bool(spec.get("cleanup_eligible", False)):
                warnings.append(
                    f"Phase '{phase.id}' cleanup skill '{cleanup.skill}' is not declared cleanup_eligible in SkillManifest. "
                    "Treat operational search/navigation/manipulation adjustments as failure recovery instead of cleanup. "
                    "v5 will preserve this field in the original TaskPlanIR but defer it from executable BT compilation."
                )
            elif not compiler_caps.get("phase_cleanup_execution", False):
                warnings.append(
                    f"Phase '{phase.id}' cleanup skill '{cleanup.skill}' is cleanup-eligible, but the current compiler "
                    "does not yet implement interruption/halt cleanup execution. It will be deferred from executable BT compilation."
                )

        if phase.desired_state and not phase.verification:
            warnings.append(f"Phase '{phase.id}' declares desired_state but has no explicit verification condition.")

    return {"valid": not errors, "errors": errors, "warnings": warnings}
