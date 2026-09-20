from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .compact_plan import build_phase_for_action
from .registry import effective_capabilities, load_skill_registry, skill_map
from .schemas import TaskPlanIR


def _norm_target(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _initial_grounded(world_state: dict[str, Any] | None) -> set[str]:
    ws = world_state or {}
    raw = ws.get("grounded_targets", {})
    out: set[str] = set()
    if isinstance(raw, dict):
        out.update(_norm_target(k) for k, v in raw.items() if v is not False)
    elif isinstance(raw, list):
        out.update(_norm_target(x) for x in raw)
    return {x for x in out if x}


def _arg_target(obj: Any) -> str | None:
    args = getattr(obj, "arguments", {}) or {}
    value = args.get("object_name")
    return str(value).strip() if value is not None and str(value).strip() else None


def _phase_visually_grounds(phase: Any, skills: dict[str, dict[str, Any]]) -> str | None:
    sid = phase.nominal_action.skill
    spec = skills.get(sid, {})
    provides = {str(x) for x in (spec.get("provides", []) or [])}
    if not provides.intersection({"search_object", "locate_object", "track_object", "refresh_object_tracking"}):
        return None
    target = _arg_target(phase.nominal_action)
    if not target:
        return None
    for verification in phase.verification:
        v_spec = skills.get(verification.condition, {})
        v_provides = {str(x) for x in (v_spec.get("provides", []) or [])}
        if not v_provides.intersection({"verify_object_visible", "verify_object_located"}):
            continue
        if _norm_target(_arg_target(verification) or target) == _norm_target(target):
            return target
    return None


def _requires_detected_target(phase: Any, skills: dict[str, dict[str, Any]]) -> bool:
    return bool(skills.get(phase.nominal_action.skill, {}).get("requires_grounded_target"))


def _unique_semantic_target(ir: TaskPlanIR) -> str | None:
    values = [
        *ir.task_semantics.object_targets,
        *ir.task_semantics.destination_targets,
        *ir.task_semantics.recipient_targets,
    ]
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and _norm_target(text) not in {_norm_target(x) for x in unique}:
            unique.append(text)
    return unique[0] if len(unique) == 1 else None


def _looks_dynamic_or_person(query: str) -> bool:
    q = _norm_target(query)
    return any(token in q for token in ["person", "people", "human", "man", "woman", "的人", "人", "user", "recipient"])


def _unique_phase_id(base: str, used: set[str]) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower() or "phase"
    if candidate not in used:
        used.add(candidate)
        return candidate
    i = 2
    while f"{candidate}_{i}" in used:
        i += 1
    out = f"{candidate}_{i}"
    used.add(out)
    return out


def apply_grounding_policy(
    ir: TaskPlanIR,
    world_state: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enforce observable target grounding before object-relative navigation.

    This is intentionally conservative: a latest-detection navigation action must be
    preceded by a verified perception selection (or an explicit grounded target in
    world_state). A pending delivery-to-user context inserts the recipient's visual
    query immediately before the post-acquisition navigation when needed.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    out = ir.model_copy(deep=True)
    changes: list[str] = []
    warnings: list[str] = []
    grounded = _initial_grounded(world_state)
    used_ids = {p.id for p in out.phases}
    ctx = (world_state or {}).get("conversation_grounding") or {}
    recipient_query = str(ctx.get("recipient_visual_query") or "").strip()

    # If this turn resolved a delivery recipient, ensure the last post-acquisition
    # detected-object navigation consumes a fresh perception target for that person.
    if recipient_query:
        grasp_seen = False
        candidate_indexes: list[int] = []
        for i, phase in enumerate(out.phases):
            provides = effective_capabilities(skills.get(phase.nominal_action.skill, {}), phase.nominal_action.arguments)
            if provides.intersection({"acquire_object", "grasp_object", "pick_object"}):
                grasp_seen = True
            if grasp_seen and _requires_detected_target(phase, skills):
                candidate_indexes.append(i)
        if candidate_indexes:
            i = candidate_indexes[-1]
            phase = out.phases[i]
            prior = out.phases[i - 1] if i > 0 else None
            prior_target = _phase_visually_grounds(prior, skills) if prior is not None else None
            if _norm_target(prior_target) != _norm_target(recipient_query):
                locate_id = _unique_phase_id(f"ground_{phase.id}_recipient", used_ids)
                locate = build_phase_for_action(
                    locate_id,
                    "VisualizeObject",
                    {"object_name": recipient_query},
                    objective=f"Visually select the mission recipient '{recipient_query}' before detected-object navigation.",
                    registry=registry,
                )
                out.phases.insert(i, locate)
                changes.append(
                    f"Inserted phase '{locate_id}' so detected-object navigation consumes the intended recipient rather than a stale/nearby detection."
                )

    new_phases = []
    latest_grounded: str | None = None
    fallback_target = _unique_semantic_target(out)
    for phase in out.phases:
        if _requires_detected_target(phase, skills):
            target = _arg_target(phase.nominal_action) or latest_grounded or fallback_target
            key = _norm_target(target)
            if target and key not in grounded:
                locate_id = _unique_phase_id(f"ground_{phase.id}", used_ids)
                locate = build_phase_for_action(
                    locate_id,
                    "VisualizeObject",
                    {"object_name": target},
                    objective=f"Visually ground the navigation target '{target}' before moving toward it.",
                    registry=registry,
                )
                new_phases.append(locate)
                grounded.add(key)
                changes.append(
                    f"Inserted phase '{locate_id}' to select '{target}' before detected-object navigation in phase '{phase.id}'."
                )
                latest_grounded = target
            elif not target:
                warnings.append(
                    f"Phase '{phase.id}' consumes the latest detected object, but no unique target could be inferred; the quality gate will require an explicit preceding perception phase."
                )
        new_phases.append(phase)
        target = _phase_visually_grounds(phase, skills)
        if target:
            grounded.add(_norm_target(target))
            latest_grounded = target

    out.phases = new_phases
    if changes:
        warnings.append(
            "The target-grounding policy modified the executable semantic plan before the quality gate; the original compact/full planner artifact remains available for comparison."
        )
    return {"ir": out, "changed": bool(changes), "changes": changes, "warnings": warnings, "grounded_targets": sorted(grounded)}
