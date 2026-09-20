from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .registry import load_skill_registry, skill_map
from .schemas import FailureClassification, Phase, TaskPlanIR

_RETRYABLE = {
    FailureClassification.TRANSIENT,
    FailureClassification.STALE_INFORMATION,
    FailureClassification.ENVIRONMENT_CHANGED,
    FailureClassification.UNKNOWN,
}


def _sanitize_key(text: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_").lower()
    return key or "value"


def _unwrap_ir_reference(value: Any) -> tuple[str, str] | None:
    """Parse planner references such as {{phase.outputs.object}} or phase.outputs.object."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    while len(text) >= 2 and text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    m = re.fullmatch(r"([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", text)
    if not m:
        return None
    return m.group(1), m.group(2)


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def build_blackboard_bindings(ir: TaskPlanIR, registry: dict[str, Any] | None = None) -> dict[str, str]:
    """Assign stable BT blackboard keys to every declared nominal phase output."""
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    bindings: dict[str, str] = {}
    used: set[str] = set()

    for phase in ir.phases:
        skill = skills.get(phase.nominal_action.skill, {})
        for port in (skill.get("outputs", {}) or {}):
            base = _sanitize_key(f"{phase.id}_{port}")
            key = base
            suffix = 2
            while key in used:
                key = f"{base}_{suffix}"
                suffix += 1
            used.add(key)
            bindings[f"{phase.id}.outputs.{port}"] = key
    return bindings


def build_reference_aliases(
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
    bindings: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build deterministic aliases for planner-produced output references.

    The formal IR reference is ``phase_id.outputs.port``. In practice, planners may
    emit convenient symbolic forms such as ``FindObject.output``. We normalize only
    aliases that are unambiguous from the SkillManifest:

    - phase_id.outputs.port
    - phase_id.output            (only when the phase action has one output)
    - SkillId.outputs.port       (only when that skill is the unique producer)
    - SkillId.port               (same unique-producer rule)
    - SkillId.output             (only when the unique producer has one output)

    Ambiguous aliases are intentionally omitted rather than guessed.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    bindings = bindings or build_blackboard_bindings(ir, registry)
    aliases: dict[str, str] = dict(bindings)

    producers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for phase in ir.phases:
        sid = phase.nominal_action.skill
        spec = skills.get(sid, {})
        outputs = list((spec.get("outputs", {}) or {}).keys())
        for port in outputs:
            canonical = f"{phase.id}.outputs.{port}"
            key = bindings.get(canonical)
            if not key:
                continue
            producers[sid].append((port, key))
            aliases[canonical] = key
            aliases[f"{phase.id}.{port}"] = key
        if len(outputs) == 1:
            key = bindings.get(f"{phase.id}.outputs.{outputs[0]}")
            if key:
                aliases[f"{phase.id}.output"] = key

    for sid, values in producers.items():
        # Only expose skill-based aliases when the skill has one unique producer
        # invocation in this mission. Otherwise e.g. FindObject.output would be
        # ambiguous and must be rejected rather than guessed.
        phases_using_skill = [p for p in ir.phases if p.nominal_action.skill == sid]
        if len(phases_using_skill) != 1:
            continue
        by_port = {port: key for port, key in values}
        for port, key in by_port.items():
            aliases[f"{sid}.outputs.{port}"] = key
            aliases[f"{sid}.{port}"] = key
        if len(by_port) == 1:
            aliases[f"{sid}.output"] = next(iter(by_port.values()))
    return aliases


def _normalize_reference_text(value: str) -> str:
    text = value.strip()
    # Accept planner templating forms such as {{phase.outputs.object}} while keeping
    # real BehaviorTree blackboard references like {target_object} intact.
    while len(text) >= 4 and text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2].strip()
    return text


def resolve_plan_value(
    value: Any,
    bindings: dict[str, str],
    aliases: dict[str, str] | None = None,
) -> str:
    """Turn TaskPlanIR values into concrete BehaviorTree.CPP attribute values."""
    aliases = aliases or {}
    if isinstance(value, str):
        text = _normalize_reference_text(value)
        # A single-braced planner reference such as {phase.outputs.object} can
        # superficially look like a BehaviorTree blackboard key. Resolve known IR
        # references first; preserve unknown single-braced values as real BT keys.
        m_bb = re.fullmatch(r"\{([^{}]+)\}", text)
        if m_bb:
            inner = m_bb.group(1).strip()
            key = aliases.get(inner) or bindings.get(inner)
            if key:
                return "{" + key + "}"
            return text
        # Exact canonical/alias reference.
        key = aliases.get(text) or bindings.get(text)
        if key:
            return "{" + key + "}"
        # Backward-compatible canonical parser.
        ref = _unwrap_ir_reference(text)
        if ref:
            lookup = f"{ref[0]}.outputs.{ref[1]}"
            if lookup in bindings:
                return "{" + bindings[lookup] + "}"
    return _literal(value)




def _type_name(spec: dict[str, Any] | None) -> str:
    return str((spec or {}).get("type", "any"))


def _is_primitive_type(type_name: str) -> bool:
    return str(type_name or "any").lower() in {
        "string", "std::string", "str", "int", "integer", "unsigned int", "uint", "uint32_t", "float", "double", "number",
        "bool", "boolean", "any",
    }


def _typed_output_candidates(
    ir: TaskPlanIR,
    registry: dict[str, Any],
    bindings: dict[str, str],
) -> list[dict[str, Any]]:
    skills = skill_map(registry)
    candidates: list[dict[str, Any]] = []
    for phase_index, phase in enumerate(ir.phases):
        spec = skills.get(phase.nominal_action.skill, {})
        for port, port_spec in (spec.get("outputs", {}) or {}).items():
            canonical = f"{phase.id}.outputs.{port}"
            key = bindings.get(canonical)
            if key:
                candidates.append({
                    "phase_index": phase_index,
                    "phase_id": phase.id,
                    "skill": phase.nominal_action.skill,
                    "port": port,
                    "type": _type_name(port_spec),
                    "key": key,
                })
    return candidates


def _infer_unique_typed_binding(
    *,
    input_type: str,
    phase_index: int,
    role: str,
    candidates: list[dict[str, Any]],
) -> str | None:
    if _is_primitive_type(input_type):
        return None
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("type")) != str(input_type):
            continue
        producer_index = int(candidate.get("phase_index", -1))
        # Nominal actions can consume outputs from prior phases. Verification nodes
        # can additionally consume the current phase action's output. Never infer a
        # future producer.
        if producer_index < phase_index or (role.startswith("verify_") and producer_index == phase_index):
            eligible.append(candidate)
    if len(eligible) == 1:
        return "{" + str(eligible[0]["key"]) + "}"
    return None

def ensure_state_verifications(
    ir: TaskPlanIR,
    phase_index: int,
    registry: dict[str, Any] | None = None,
) -> list[Any]:
    """Return verification conditions that are safe to evaluate *before* the action.

    v5.3 uses these conditions to compile an explicit IF/ELSE "ensure state" guard:

        Fallback
        |- ConditionAlreadyTrue
        `- Sequence(Action -> Verification)

    This is only legal when every required verification input is already available
    before the phase action executes.  A locator verification such as
    ``IsObjectLocated(FindObject.object)`` is therefore *not* pre-checkable in the
    same phase, while ``IsNearObject`` and ``IsObjectHeld`` can be pre-checked when
    they consume a tracked object produced by an earlier phase.

    If even one verification cannot be proven safe, the phase gets no ensure-state
    precheck rather than guessing or reading a future blackboard value.
    """
    registry = registry or load_skill_registry()
    if phase_index < 0 or phase_index >= len(ir.phases):
        return []
    phase = ir.phases[phase_index]
    if not phase.verification:
        return []

    skills = skill_map(registry)
    action_spec = skills.get(phase.nominal_action.skill, {})
    # A trigger such as VisualizeObject must always execute: it selects the active
    # continuous camera query. A stale positive condition from a previous query is
    # not a safe reason to skip it.
    if action_spec.get("ensure_state_precheck_allowed") is False:
        return []
    bindings = build_blackboard_bindings(ir, registry)
    aliases = build_reference_aliases(ir, registry, bindings)
    typed_candidates = _typed_output_candidates(ir, registry, bindings)
    producer_by_key = {str(c["key"]): int(c["phase_index"]) for c in typed_candidates}
    ext_raw = (registry.get("blackboard", {}) or {}).get("externally_provided", {}) or {}
    external_keys = {str(k) for k in (ext_raw.keys() if isinstance(ext_raw, dict) else ext_raw)}

    safe: list[Any] = []
    for verification in phase.verification:
        spec = skills.get(verification.condition, {})
        if str(spec.get("kind", "")).upper() != "CONDITION":
            return []
        inputs = spec.get("inputs", {}) or {}
        for name, port_spec in inputs.items():
            if not port_spec.get("required"):
                continue
            input_type = _type_name(port_spec)
            if name in verification.arguments:
                resolved = resolve_plan_value(verification.arguments[name], bindings, aliases)
                m = re.fullmatch(r"\{([^{}]+)\}", resolved)
                if m:
                    key = m.group(1)
                    producer_index = producer_by_key.get(key)
                    if producer_index is None:
                        if key not in external_keys:
                            return []
                    elif producer_index >= phase_index:
                        return []
                elif not _is_primitive_type(input_type):
                    inferred = _infer_unique_typed_binding(
                        input_type=input_type,
                        phase_index=phase_index,
                        role="ensure_precheck",
                        candidates=typed_candidates,
                    )
                    if not inferred:
                        return []
            else:
                if _is_primitive_type(input_type):
                    return []
                inferred = _infer_unique_typed_binding(
                    input_type=input_type,
                    phase_index=phase_index,
                    role="ensure_precheck",
                    candidates=typed_candidates,
                )
                if not inferred:
                    return []
        safe.append(verification)
    return safe

def build_invocation_plan(
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
    *,
    include_recovery: bool = False,
    include_compiled_search_recovery: bool = False,
    include_compiled_ensure_state: bool = False,
    include_cleanup: bool = False,
) -> list[dict[str, Any]]:
    """Build authoritative leaf calls implied by TaskPlanIR.

    Failure-specific recovery and cleanup leaves are excluded by default because the
    current compiler contract cannot execute them faithfully. Planner-produced symbolic
    output references are normalized deterministically before XML binding.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    bindings = build_blackboard_bindings(ir, registry)
    aliases = build_reference_aliases(ir, registry, bindings)
    typed_candidates = _typed_output_candidates(ir, registry, bindings)
    invocations: list[dict[str, Any]] = []

    def add_invocation(phase_index: int, phase_id: str, role: str, skill_id: str, arguments: dict[str, Any]) -> None:
        spec = skills.get(skill_id, {})
        node_name = _sanitize_key(f"{phase_id}_{role}_{skill_id}")
        ports: dict[str, str] = {}
        inputs = spec.get("inputs", {}) or {}
        for name, value in (arguments or {}).items():
            resolved = resolve_plan_value(value, bindings, aliases)
            port_spec = inputs.get(name, {}) or {}
            input_type = _type_name(port_spec)
            if not _is_primitive_type(input_type) and not (resolved.startswith("{") and resolved.endswith("}")):
                inferred = _infer_unique_typed_binding(
                    input_type=input_type,
                    phase_index=phase_index,
                    role=role,
                    candidates=typed_candidates,
                )
                if inferred:
                    resolved = inferred
            ports[name] = resolved

        # A planner may omit or use a free-form symbolic label for an opaque input.
        # When exactly one type-compatible prior producer exists, bind it
        # deterministically instead of allowing a random structural failure. If more
        # than one candidate exists, leave the port unresolved so validation rejects
        # the ambiguity rather than guessing.
        for name, port_spec in inputs.items():
            if name in ports or not port_spec.get("required"):
                continue
            input_type = _type_name(port_spec)
            inferred = _infer_unique_typed_binding(
                input_type=input_type,
                phase_index=phase_index,
                role=role,
                candidates=typed_candidates,
            )
            if inferred:
                ports[name] = inferred

        for port in (spec.get("outputs", {}) or {}):
            key = bindings.get(f"{phase_id}.outputs.{port}") if role == "action" else None
            key = key or _sanitize_key(f"{node_name}_{port}")
            ports[port] = "{" + key + "}"
        invocations.append(
            {
                "phase_id": phase_id,
                "role": role,
                "skill": skill_id,
                "kind": str(spec.get("kind", "ACTION")).upper(),
                "ports": ports,
                "node_name": node_name,
            }
        )

    for phase_index, phase in enumerate(ir.phases):
        if include_compiled_ensure_state:
            for pidx, verification in enumerate(ensure_state_verifications(ir, phase_index, registry), 1):
                add_invocation(
                    phase_index,
                    phase.id,
                    f"ensure_precheck_{pidx}",
                    verification.condition,
                    verification.arguments,
                )
        add_invocation(phase_index, phase.id, "action", phase.nominal_action.skill, phase.nominal_action.arguments)
        for idx, verification in enumerate(phase.verification, 1):
            add_invocation(phase_index, phase.id, f"verify_{idx}", verification.condition, verification.arguments)
        if include_recovery:
            for fidx, failure in enumerate(phase.failure_modes, 1):
                for ridx, recovery in enumerate(failure.recovery, 1):
                    if recovery.skill:
                        add_invocation(
                            phase_index,
                            phase.id,
                            f"recovery_{fidx}_{ridx}",
                            recovery.skill,
                            recovery.arguments,
                        )
        elif include_compiled_search_recovery:
            for ridx, recovery in enumerate(search_activity_recovery_steps(phase, registry), 1):
                add_invocation(
                    phase_index,
                    phase.id,
                    f"search_recovery_{ridx}",
                    recovery.skill,
                    recovery.arguments,
                )
        if include_cleanup:
            for cidx, cleanup in enumerate(phase.cleanup, 1):
                add_invocation(phase_index, phase.id, f"cleanup_{cidx}", cleanup.skill, cleanup.arguments)
    return invocations


def _continuous_patrol_spec(spec: dict[str, Any]) -> bool:
    provides = {str(x) for x in (spec.get("provides", []) or [])}
    return (
        str(spec.get("search_policy_role", "")) == "continuous_patrol"
        or "continuous_search_motion" in provides
    )


def continuous_search_recovery_steps(
    phase: Phase | Any,
    registry: dict[str, Any] | None = None,
) -> list[Any]:
    """Return long-running patrol activities for continuous perception search."""
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    action_spec = skills.get(phase.nominal_action.skill, {})
    action_provides = {str(x) for x in (action_spec.get("provides", []) or [])}
    if not action_spec.get("continuous_search_trigger") and not action_provides.intersection(
        {"locate_object", "search_object", "trigger_continuous_object_search"}
    ):
        return []

    candidates: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for failure in phase.failure_modes:
        if failure.classification not in _RETRYABLE:
            continue
        for step in failure.recovery:
            if not step.skill or step.skill == phase.nominal_action.skill:
                continue
            if not _continuous_patrol_spec(skills.get(step.skill, {})):
                continue
            key = (step.skill, json.dumps(step.arguments, sort_keys=True, ensure_ascii=False))
            if key not in seen:
                seen.add(key)
                candidates.append(step)
    return candidates


def search_activity_recovery_steps(
    phase: Phase | Any,
    registry: dict[str, Any] | None = None,
) -> list[Any]:
    """Return the current patrol activity, or a legacy viewpoint recovery.

    Current live registries compile continuous camera search as
    ``VisualizeObject -> Timeout(ReactiveFallback(IsObjectFound, Patrol))``.
    The viewpoint branch remains only for old/demo registries that do not declare a
    continuous patrol role.
    """
    patrol = continuous_search_recovery_steps(phase, registry)
    return patrol or search_viewpoint_recovery_steps(phase, registry)


def search_viewpoint_recovery_steps(
    phase: Phase | Any,
    registry: dict[str, Any] | None = None,
) -> list[Any]:
    """Return legacy bounded viewpoint-changing search recoveries.

    Kept for compatibility with older registries. New live-node semantics use
    :func:`continuous_search_recovery_steps` instead.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    action_spec = skills.get(phase.nominal_action.skill, {})
    action_provides = {str(x) for x in (action_spec.get("provides", []) or [])}
    if not action_provides.intersection({"locate_object", "search_object"}):
        return []

    candidates: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for failure in phase.failure_modes:
        if failure.classification not in _RETRYABLE:
            continue
        for step in failure.recovery:
            if not step.skill or step.skill == phase.nominal_action.skill:
                continue
            spec = skills.get(step.skill, {})
            provides = {str(x) for x in (spec.get("provides", []) or [])}
            if not provides.intersection({"change_search_viewpoint", "rotate_in_place"}):
                continue
            key = (step.skill, json.dumps(step.arguments, sort_keys=True, ensure_ascii=False))
            if key not in seen:
                seen.add(key)
                candidates.append(step)
    return candidates


def phase_retry_attempts(phase: Phase | Any) -> int:
    attempts = 1
    for failure in phase.failure_modes:
        explicit_retry = any(getattr(step.strategy, "value", str(step.strategy)) == "RETRY" for step in failure.recovery)
        if failure.classification in _RETRYABLE or explicit_retry:
            attempts = max(attempts, int(failure.max_attempts))
    return attempts


def phase_timeout_msec(phase: Phase | Any) -> int | None:
    values = [float(f.timeout_sec) for f in phase.failure_modes if f.timeout_sec]
    if not values:
        return None
    return int(max(values) * 1000)


def _action_signature(skill_id: str, registry: dict[str, Any]) -> str:
    spec = skill_map(registry).get(skill_id, {})
    params = [*(spec.get("inputs", {}) or {}).keys(), *(spec.get("outputs", {}) or {}).keys()]
    return f"{skill_id} (parameters: {', '.join(params)})"


def phase_actions_text(phase: Phase, registry: dict[str, Any] | None = None) -> str:
    """Closed BTGenBot vocabulary for one semantic phase only."""
    registry = registry or load_skill_registry()
    ids = [phase.nominal_action.skill, *(v.condition for v in phase.verification)]
    seen: set[str] = set()
    signatures: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            signatures.append(_action_signature(sid, registry))
    return "[" + ", ".join(signatures) + "]"


def phase_compiler_task(phase: Phase) -> str:
    """Create a very small BTGenBot-native task.

    Retry/timeout/recovery policy is intentionally omitted: v4.3 showed that asking
    the 1B model to expand semantic failure policy caused runaway/repetitive XML.
    The server deterministically adds bounded RetryUntilSuccessful/Timeout wrappers
    after BTGenBot proposes the nominal action-verification topology.
    """
    verifications = [v.condition for v in phase.verification]
    if verifications:
        verify_text = ", then ".join(verifications)
        return (
            f"Run {phase.nominal_action.skill}. After it succeeds, run {verify_text} to verify the result. "
            "Use a Sequence so the action happens before every verification. "
            "Generate exactly these listed leaf nodes once each. "
            "Do not add retry, timeout, recovery, extra actions, or SubTrees; the server adds policy wrappers later."
        )
    return (
        f"Run {phase.nominal_action.skill} exactly once. "
        "Generate exactly this listed leaf node and no other leaf nodes. "
        "Do not add retry, timeout, recovery, extra actions, or SubTrees; the server adds policy wrappers later."
    )


def task_plan_to_compiler_task(ir: TaskPlanIR) -> str:
    """Human-readable diagnostic summary of the phase-wise compiler plan."""
    chunks = []
    for idx, phase in enumerate(ir.phases, 1):
        chunks.append(f"Phase {idx} ({phase.id}): {phase_compiler_task(phase)}")
    return "\n".join(chunks)
