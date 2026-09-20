from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .registry import load_skill_registry, skill_map
from .schemas import TaskPlanIR

# v5.2/demo semantic aliases -> the team's current BT engine IDs.
# This is a migration layer only. The compiler and final XML use formal IDs exclusively.
_LEGACY_SKILL_ALIASES = {
    "FindObject": "VisualizeObject",
    "IsObjectLocated": "IsObjectFound",
}

_OBJECT_NAME_SKILLS = {
    "VisualizeObject",
    "IsObjectFound",
}


def _plain_target(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "{{" in text or "}}" in text or ".outputs." in text or ".nominal_action." in text:
        return None
    if text.startswith("{") and text.endswith("}"):
        return None
    if text.lower().startswith("tracked_"):
        return None
    return text


def _legacy_target_hint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("tracked_") and len(text) > len("tracked_"):
        return text[len("tracked_"):].replace("_", " ")
    return None


def _rename_args(skill_id: str, arguments: dict[str, Any], changes: list[str], owner: str) -> dict[str, Any]:
    args = deepcopy(arguments or {})
    if skill_id in _OBJECT_NAME_SKILLS:
        if "object_name" not in args:
            for old in ("object", "query", "target", "name"):
                if old in args:
                    args["object_name"] = args.pop(old)
                    changes.append(f"{owner}: renamed argument {old}->object_name for {skill_id}.")
                    break
        # Remove duplicate legacy synonyms after canonical object_name is present.
        if "object_name" in args:
            for old in ("object", "query", "target"):
                if old in args:
                    args.pop(old, None)
                    changes.append(f"{owner}: removed legacy argument {old} for {skill_id}.")
    elif skill_id == "RotateInPlace":
        if "angle_deg" not in args and "degrees" in args:
            args["angle_deg"] = args.pop("degrees")
            changes.append(f"{owner}: renamed argument degrees->angle_deg for RotateInPlace.")
        if "angle_deg" in args and "degrees" in args:
            args.pop("degrees", None)
    return args


def _map_skill(skill_id: str, registry_ids: set[str], changes: list[str], owner: str) -> str:
    if skill_id in registry_ids:
        return skill_id
    mapped = _LEGACY_SKILL_ALIASES.get(skill_id)
    if mapped and mapped in registry_ids:
        changes.append(f"{owner}: migrated legacy skill {skill_id}->{mapped}.")
        return mapped
    return skill_id


def normalize_plan_to_runtime_skill_contract(
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Migrate harmless legacy planner spellings to the formal BT-engine contract.

    The team's runtime contract uses named objects (``std::string object_name``), not
    opaque ``TrackedObject`` blackboard outputs. Therefore legacy symbolic references
    such as ``{{locate_bottle.outputs.object}}`` are not valid runtime values. When a
    mission has one unambiguous named target, they are converted to that literal name.

    Unknown or ambiguous values are intentionally left untouched so IR validation can
    reject them instead of guessing.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    registry_ids = set(skills)
    normalized = ir.model_copy(deep=True)
    changes: list[str] = []
    warnings: list[str] = []

    # Pass 1: canonical IDs and port names.
    for phase in normalized.phases:
        owner = f"Phase '{phase.id}' nominal_action"
        phase.nominal_action.skill = _map_skill(phase.nominal_action.skill, registry_ids, changes, owner)
        phase.nominal_action.arguments = _rename_args(
            phase.nominal_action.skill, phase.nominal_action.arguments, changes, owner
        )
        for idx, verification in enumerate(phase.verification, 1):
            owner = f"Phase '{phase.id}' verification[{idx}]"
            verification.condition = _map_skill(verification.condition, registry_ids, changes, owner)
            verification.arguments = _rename_args(
                verification.condition, verification.arguments, changes, owner
            )
        for fidx, failure in enumerate(phase.failure_modes, 1):
            for ridx, recovery in enumerate(failure.recovery, 1):
                if not recovery.skill:
                    continue
                owner = f"Phase '{phase.id}' recovery[{fidx},{ridx}]"
                recovery.skill = _map_skill(recovery.skill, registry_ids, changes, owner)
                recovery.arguments = _rename_args(recovery.skill, recovery.arguments, changes, owner)
        for cidx, cleanup in enumerate(phase.cleanup, 1):
            owner = f"Phase '{phase.id}' cleanup[{cidx}]"
            cleanup.skill = _map_skill(cleanup.skill, registry_ids, changes, owner)
            cleanup.arguments = _rename_args(cleanup.skill, cleanup.arguments, changes, owner)

    # Find unambiguous literal named target(s), preferring perception/search phases.
    preferred: list[str] = []
    all_literals: list[str] = []
    hints: list[str] = []
    for phase in normalized.phases:
        calls: list[tuple[str, dict[str, Any]]] = [(phase.nominal_action.skill, phase.nominal_action.arguments)]
        calls += [(v.condition, v.arguments) for v in phase.verification]
        calls += [(r.skill or "", r.arguments) for f in phase.failure_modes for r in f.recovery]
        for sid, args in calls:
            if sid not in _OBJECT_NAME_SKILLS:
                continue
            value = (args or {}).get("object_name")
            literal = _plain_target(value)
            if literal:
                all_literals.append(literal)
                if sid == "VisualizeObject":
                    preferred.append(literal)
            hint = _legacy_target_hint(value)
            if hint:
                hints.append(hint)

    def unique(values: list[str]) -> str | None:
        normalized_values = []
        for value in values:
            if value not in normalized_values:
                normalized_values.append(value)
        return normalized_values[0] if len(normalized_values) == 1 else None

    mission_target = unique(preferred) or unique(all_literals) or unique(hints)

    # Pass 2: replace legacy symbolic object references only when target is unique.
    if mission_target:
        for phase in normalized.phases:
            calls: list[tuple[str, dict[str, Any], str]] = [
                (phase.nominal_action.skill, phase.nominal_action.arguments, f"Phase '{phase.id}' nominal_action")
            ]
            calls += [
                (v.condition, v.arguments, f"Phase '{phase.id}' verification[{i}]")
                for i, v in enumerate(phase.verification, 1)
            ]
            calls += [
                (r.skill or "", r.arguments, f"Phase '{phase.id}' recovery[{fi},{ri}]")
                for fi, f in enumerate(phase.failure_modes, 1)
                for ri, r in enumerate(f.recovery, 1)
            ]
            for sid, args, owner in calls:
                if sid not in _OBJECT_NAME_SKILLS or "object_name" not in args:
                    continue
                value = args["object_name"]
                if _plain_target(value) is not None:
                    continue
                if isinstance(value, str):
                    args["object_name"] = mission_target
                    changes.append(
                        f"{owner}: resolved legacy/symbolic object reference {value!r} -> object_name={mission_target!r}."
                    )
    else:
        # Only warn when unresolved symbolic object names are actually present.
        unresolved = []
        for phase in normalized.phases:
            for sid, args in [
                (phase.nominal_action.skill, phase.nominal_action.arguments),
                *[(v.condition, v.arguments) for v in phase.verification],
            ]:
                if sid in _OBJECT_NAME_SKILLS and "object_name" in args and _plain_target(args["object_name"]) is None:
                    unresolved.append(f"{phase.id}:{sid}")
        if unresolved:
            warnings.append(
                "Could not infer one unique named target for legacy symbolic object references: "
                + ", ".join(unresolved)
                + ". IR validation will reject unresolved values rather than guessing."
            )

    return {
        "ir": normalized,
        "changed": bool(changes),
        "changes": changes,
        "warnings": warnings,
        "formal_skill_contract_version": registry.get("version"),
    }
