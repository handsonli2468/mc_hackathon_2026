from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from .compact_plan import build_phase_for_action
from .location_grounding import find_location_id_for_target
from .registry import load_skill_registry, skill_map
from .schemas import TaskPlanIR
from .settings import settings


def _same_bound_value(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-6)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _matches_navigation_candidate(skill_id: str, arguments: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if skill_id != str(candidate.get("skill_id") or ""):
        return False
    # Speed is the only planner-selectable variation. Coordinates and arrival
    # heading must be copied exactly from the typed Location-RAG binding.
    planned = {k: v for k, v in (arguments or {}).items() if k != "speed"}
    bound = {k: v for k, v in (candidate.get("arguments") or {}).items() if k != "speed"}
    return set(planned) == set(bound) and all(_same_bound_value(planned[k], bound[k]) for k in bound)


def validate_location_navigation_provenance(
    plan: TaskPlanIR,
    rag_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require every map-coordinate action to match an actionable Location-RAG binding."""
    ctx = rag_context or {}
    grounding = ctx.get("location_grounding") or {}
    candidates: list[dict[str, Any]] = []
    for info in (grounding.get("locations") or {}).values():
        if info.get("actionable"):
            candidates.extend(info.get("navigation_candidates") or [])

    registry = skill_map(load_skill_registry())
    location_skills = {
        sid for sid, spec in registry.items()
        if set(spec.get("provides") or []).intersection({"navigate_to_location", "navigate_to_xy", "navigate_to_map_pose"})
    }
    errors: list[str] = []
    matches: list[dict[str, Any]] = []

    def check(owner: str, skill_id: str, arguments: dict[str, Any]) -> None:
        if skill_id not in location_skills:
            return
        candidate = next(
            (item for item in candidates if _matches_navigation_candidate(skill_id, arguments, item)),
            None,
        )
        if candidate is None:
            errors.append(
                f"{owner} uses {skill_id} coordinates/heading that do not match any actionable "
                "Location-RAG navigation_candidate retrieved for this mission."
            )
        else:
            matches.append({"owner": owner, "skill": skill_id, "arguments": dict(arguments or {})})

    for phase in plan.phases:
        check(f"Phase '{phase.id}' nominal action", phase.nominal_action.skill, phase.nominal_action.arguments)
        for failure_index, failure in enumerate(phase.failure_modes, 1):
            for recovery_index, recovery in enumerate(failure.recovery, 1):
                if recovery.skill:
                    check(
                        f"Phase '{phase.id}' recovery[{failure_index},{recovery_index}]",
                        recovery.skill,
                        recovery.arguments,
                    )
        for cleanup_index, cleanup in enumerate(phase.cleanup, 1):
            check(f"Phase '{phase.id}' cleanup[{cleanup_index}]", cleanup.skill, cleanup.arguments)
    return {"valid": not errors, "errors": errors, "matches": matches}


def _candidate_for_location(location_info: dict[str, Any], allowed: set[str]) -> dict[str, Any] | None:
    for candidate in location_info.get("navigation_candidates", []) or []:
        sid = str(candidate.get("skill_id") or "")
        if sid and sid in allowed:
            return candidate
    return None


def _norm_words(value: Any) -> set[str]:
    text = str(value or "").casefold().replace("_", " ").replace("-", " ")
    # Keep unicode word chars so CJK exact strings still survive; English semantic
    # expansions remain easy to match by token overlap.
    return {x for x in re.findall(r"[\w\u4e00-\u9fff]+", text, flags=re.UNICODE) if x}


def _target_matches(a: Any, b: Any) -> bool:
    aa = " ".join(str(a or "").strip().casefold().split())
    bb = " ".join(str(b or "").strip().casefold().split())
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    aw, bw = _norm_words(aa), _norm_words(bb)
    return bool(aw and bw and (aw <= bw or bw <= aw or len(aw & bw) >= min(len(aw), len(bw))))


def _search_prior_candidate(ctx: dict[str, Any], target: str, allowed: set[str]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    threshold = float(getattr(settings, "rag_search_prior_min_confidence", 0.70))
    for prior in ctx.get("search_priors", []) or []:
        if float(prior.get("confidence") or 0.0) < threshold:
            continue
        if not prior.get("actionable"):
            continue
        if not _target_matches(target, prior.get("target")):
            continue
        for candidate in prior.get("navigation_candidates", []) or []:
            sid = str(candidate.get("skill_id") or "")
            if sid and sid in allowed:
                return prior, candidate
    return None


def _same_navigation_phase(phase: Any, candidate: dict[str, Any]) -> bool:
    if str(phase.nominal_action.skill) != str(candidate.get("skill_id") or ""):
        return False
    return dict(phase.nominal_action.arguments or {}) == dict(candidate.get("arguments") or {})


def apply_location_grounding_policy(
    plan: TaskPlanIR,
    rag_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply deterministic typed-location policy after semantic planning.

    Two generic transformations are supported, both sourced entirely from RAG:
    1) exact named destinations are rewritten from object-relative navigation to an
       executable Location-RAG navigation candidate;
    2) an unresolved visual target may receive a high-confidence Scene-RAG search
       location before its first local visual-search phase.

    No concrete place/object name is hard-coded here.
    """
    out = deepcopy(plan)
    ctx = rag_context or {}
    grounding = ctx.get("location_grounding") or {}
    locations = grounding.get("locations", {}) or {}
    allowed = {str(x) for x in (ctx.get("allowed_skill_ids") or [])}
    skills = skill_map(load_skill_registry())

    def is_visual_grounding_action(skill_id: str) -> bool:
        provides = {str(x) for x in (skills.get(skill_id, {}).get("provides", []) or [])}
        return bool(provides.intersection({"search_object", "locate_object", "track_object", "refresh_object_tracking"}))

    def is_object_relative_navigation(skill_id: str) -> bool:
        spec = skills.get(skill_id, {})
        provides = {str(x) for x in (spec.get("provides", []) or [])}
        return bool(spec.get("requires_grounded_target")) or bool(provides.intersection({"navigate_to_object", "approach_object"}))

    if not getattr(settings, "location_grounding_enabled", True):
        return {
            "ir": out,
            "changed": False,
            "changes": [],
            "warnings": [],
            "bindings": [],
            "removed_visual_grounding_phases": [],
        }

    changes: list[str] = []
    warnings: list[str] = []
    bindings: list[dict[str, Any]] = []
    rewritten_locations: set[str] = set()

    # A) exact named destination rewrite.
    for index, phase in enumerate(list(out.phases)):
        sid = str(phase.nominal_action.skill)
        args = dict(phase.nominal_action.arguments or {})
        if not is_object_relative_navigation(sid):
            continue
        target = args.get("object_name")
        location_id = find_location_id_for_target(target, grounding)
        if not location_id:
            continue
        info = locations.get(location_id) or {}
        if not info.get("actionable"):
            warnings.append(
                f"Phase '{phase.id}' names known location '{location_id}', but it is not actionable: {info.get('reason', 'unknown reason')}."
            )
            continue
        candidate = _candidate_for_location(info, allowed)
        if candidate is None:
            warnings.append(
                f"Phase '{phase.id}' names actionable location '{location_id}', but no location navigation candidate is in allowed_skill_ids."
            )
            continue

        new_skill = str(candidate["skill_id"])
        new_args = deepcopy(candidate.get("arguments") or {})
        replacement = build_phase_for_action(phase.id, new_skill, new_args, objective=phase.objective)
        out.phases[index] = replacement
        rewritten_locations.add(location_id)
        bindings.append({
            "binding_type": "named_destination",
            "phase_id": phase.id,
            "location_id": location_id,
            "source_target": str(target),
            "old_skill": sid,
            "skill": new_skill,
            "arguments": new_args,
        })
        changes.append(
            f"Phase '{phase.id}' rewrote object-relative navigation for '{target}' to {new_skill} using retrieved location '{location_id}'."
        )

    removed: list[str] = []
    if rewritten_locations and getattr(settings, "location_rewrite_visual_preamble", True):
        kept = []
        for phase in out.phases:
            sid = str(phase.nominal_action.skill)
            args = dict(phase.nominal_action.arguments or {})
            target = args.get("object_name")
            location_id = find_location_id_for_target(target, grounding) if target else None
            if is_visual_grounding_action(sid) and location_id in rewritten_locations:
                removed.append(phase.id)
                changes.append(
                    f"Removed visual grounding phase '{phase.id}' for retrieved static location '{location_id}' because map navigation is available."
                )
                continue
            kept.append(phase)
        out.phases = kept

    # B) Scene-RAG search prior insertion. This intentionally keeps the visual search;
    # the location only changes viewpoint/region before perception grounds the object.
    if ctx.get("search_priors"):
        rebuilt = []
        inserted_keys: set[tuple[str, str]] = set()
        for phase in out.phases:
            sid = str(phase.nominal_action.skill)
            args = dict(phase.nominal_action.arguments or {})
            target = str(args.get("object_name") or "").strip()
            if is_visual_grounding_action(sid) and target:
                resolved = _search_prior_candidate(ctx, target, allowed)
                if resolved is not None:
                    prior, candidate = resolved
                    location_id = str(prior.get("location_id") or "")
                    key = (target.casefold(), location_id)
                    already_before = any(_same_navigation_phase(p, candidate) for p in rebuilt[-2:])
                    if key not in inserted_keys and not already_before:
                        phase_id = f"search_prior_{phase.id}"
                        search_phase = build_phase_for_action(
                            phase_id,
                            str(candidate.get("skill_id")),
                            deepcopy(candidate.get("arguments") or {}),
                            objective=f"Move to retrieved search region before local perception for {target}",
                        )
                        rebuilt.append(search_phase)
                        inserted_keys.add(key)
                        bindings.append({
                            "binding_type": "scene_search_prior",
                            "phase_id": phase_id,
                            "target": target,
                            "location_id": location_id,
                            "source_scene_id": prior.get("source_scene_id"),
                            "confidence": prior.get("confidence"),
                            "skill": candidate.get("skill_id"),
                            "arguments": deepcopy(candidate.get("arguments") or {}),
                        })
                        changes.append(
                            f"Inserted search-location navigation '{phase_id}' to retrieved location '{location_id}' before visual search for '{target}'."
                        )
            rebuilt.append(phase)
        out.phases = rebuilt

    return {
        "ir": out,
        "changed": bool(changes),
        "changes": changes,
        "warnings": warnings,
        "bindings": bindings,
        "removed_visual_grounding_phases": removed,
    }
