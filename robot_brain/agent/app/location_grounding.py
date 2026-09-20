from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from .registry import load_skill_registry, skill_map
from .settings import settings

LOCATION_NAV_CAPABILITIES = {
    "navigate_to_location",
    "navigate_to_pose",
    "navigate_to_xy",
    "navigate_to_map_pose",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _world_map_version(world_state: dict[str, Any] | None) -> str | None:
    ws = world_state or {}
    candidates = [
        ws.get("map_version"),
        (ws.get("map") or {}).get("version") if isinstance(ws.get("map"), dict) else None,
        (ws.get("navigation") or {}).get("map_version") if isinstance(ws.get("navigation"), dict) else None,
        (ws.get("localization") or {}).get("map_version") if isinstance(ws.get("localization"), dict) else None,
        getattr(settings, "robot_map_version", "") or None,
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _location_aliases(location_id: str, location: dict[str, Any]) -> list[str]:
    meta = location.get("metadata") or {}
    aliases = [location_id, location_id.replace("_", " ")]
    for item in meta.get("aliases", []) or []:
        text = str(item).strip()
        if text:
            aliases.append(text)
    return list(dict.fromkeys(aliases))


def find_location_id_for_target(target: Any, grounding: dict[str, Any] | None) -> str | None:
    target_norm = _norm(target)
    if not target_norm:
        return None
    locations = (grounding or {}).get("locations", {}) or {}
    for location_id, info in locations.items():
        aliases = info.get("aliases", []) or [location_id]
        if target_norm in {_norm(x) for x in aliases if _norm(x)}:
            return str(location_id)
    return None


def _location_pose(location: dict[str, Any]) -> dict[str, Any]:
    meta = location.get("metadata") or {}
    pose_meta = meta.get("entry_pose") if isinstance(meta.get("entry_pose"), dict) else (meta.get("pose") if isinstance(meta.get("pose"), dict) else {})
    return {
        "x": location.get("x", pose_meta.get("x")),
        "y": location.get("y", pose_meta.get("y")),
        "yaw": location.get("yaw", pose_meta.get("yaw")),
        "yaw_unit": str(meta.get("yaw_unit") or "rad").strip().lower(),
    }


def _infer_location_binding(skill: dict[str, Any]) -> dict[str, Any] | None:
    explicit = skill.get("location_binding")
    if isinstance(explicit, dict) and explicit:
        return deepcopy(explicit)

    inputs = skill.get("inputs", {}) or {}
    provides = {str(x) for x in (skill.get("provides") or [])}
    if not provides.intersection(LOCATION_NAV_CAPABILITIES):
        return None
    if not {"x", "y"}.issubset(inputs):
        return None

    args: dict[str, Any] = {
        "x": {"source": "x"},
        "y": {"source": "y"},
    }
    if "yaw_deg" in inputs:
        args["yaw_deg"] = {
            "source": "yaw",
            "transform": "angle_to_degrees",
            "omit_if_missing": True,
        }
    elif "yaw" in inputs:
        args["yaw"] = {"source": "yaw", "omit_if_missing": True}

    return {
        "frames": ["map"],
        "location_types": ["STATIC_LOCATION"],
        "arguments": args,
        "inferred": True,
    }


def _format_for_port(value: Any, port_spec: dict[str, Any]) -> Any:
    type_name = str(port_spec.get("type") or "").strip().lower()
    if type_name in {"std::string", "string", "str"}:
        if isinstance(value, float):
            if abs(value - round(value)) < 1e-9:
                return str(int(round(value)))
            return f"{value:.6f}".rstrip("0").rstrip(".")
        return str(value)
    if type_name in {"double", "float", "number"}:
        return float(value)
    if type_name in {"int", "integer"}:
        return int(round(float(value)))
    return value


def _resolve_source(source: str, location: dict[str, Any], pose: dict[str, Any]) -> Any:
    source = str(source or "").strip()
    if source in pose:
        return pose.get(source)
    if source in location:
        return location.get(source)
    meta = location.get("metadata") or {}
    return meta.get(source)


def _transform_value(value: Any, transform: str | None, pose: dict[str, Any]) -> Any:
    if value is None:
        return None
    name = str(transform or "").strip().lower()
    if not name:
        return value
    if name in {"angle_to_degrees", "rad_to_deg", "radians_to_degrees"}:
        unit = str(pose.get("yaw_unit") or "rad").lower()
        numeric = float(value)
        return math.degrees(numeric) if unit in {"rad", "radian", "radians"} else numeric
    if name in {"angle_to_radians", "deg_to_rad", "degrees_to_radians"}:
        unit = str(pose.get("yaw_unit") or "rad").lower()
        numeric = float(value)
        return math.radians(numeric) if unit in {"deg", "degree", "degrees"} else numeric
    return value


def bind_location_arguments(location: dict[str, Any], skill: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    binding = _infer_location_binding(skill)
    if not binding:
        return None, ["skill does not declare/infer a location binding"]

    inputs = skill.get("inputs", {}) or {}
    args_spec = binding.get("arguments", {}) or {}
    pose = _location_pose(location)
    errors: list[str] = []
    arguments: dict[str, Any] = {}

    for port_name, rule in args_spec.items():
        if port_name not in inputs:
            errors.append(f"binding references unknown input port '{port_name}'")
            continue
        if isinstance(rule, str):
            rule = {"source": rule}
        if not isinstance(rule, dict):
            errors.append(f"binding for '{port_name}' must be an object or source string")
            continue
        source = str(rule.get("source") or port_name)
        value = _resolve_source(source, location, pose)
        if value is None and rule.get("omit_if_missing", False):
            continue
        if value is None:
            errors.append(f"location has no value for '{source}' required by '{port_name}'")
            continue
        try:
            value = _transform_value(value, rule.get("transform"), pose)
            arguments[port_name] = _format_for_port(value, inputs.get(port_name) or {})
        except Exception as exc:
            errors.append(f"failed to bind '{port_name}' from '{source}': {exc}")

    for port_name, port in inputs.items():
        if port.get("required") and port_name not in arguments:
            errors.append(f"required input '{port_name}' is not bound")

    return (arguments if not errors else None), errors


def _compatibility_reason(location: dict[str, Any], binding: dict[str, Any], world_state: dict[str, Any] | None) -> tuple[bool, str, list[str]]:
    meta = location.get("metadata") or {}
    calibrated = bool(meta.get("calibrated", False))
    trust_mode = str(getattr(settings, "location_knowledge_mode", "integration") or "integration").strip().lower()
    validated_mode = trust_mode in {"validated", "production", "strict"}
    warnings: list[str] = []
    if validated_mode and getattr(settings, "location_require_calibrated", True) and not calibrated:
        return False, "retrieved location is not calibrated; validated mode keeps it advisory only", []
    if not validated_mode and not calibrated:
        warnings.append("location is marked uncalibrated but LOCATION_KNOWLEDGE_MODE=integration trusts configured poses for pipeline testing")

    frame_id = str(location.get("frame_id") or meta.get("frame_id") or "map")
    frames = [str(x) for x in (binding.get("frames") or [])]
    if frames and frame_id not in frames:
        return False, f"location frame '{frame_id}' is not supported by the navigation skill", []

    location_type = str(location.get("location_type") or meta.get("type") or "STATIC_LOCATION")
    allowed_types = [str(x) for x in (binding.get("location_types") or [])]
    if allowed_types and location_type not in allowed_types:
        return False, f"location type '{location_type}' is not executable by the navigation skill", []

    current_map = _world_map_version(world_state)
    location_map = str(location.get("map_version") or meta.get("map_version") or "").strip() or None
    if validated_mode and current_map and location_map and current_map != location_map:
        return False, f"map version mismatch: robot={current_map}, location={location_map}", []
    if validated_mode and getattr(settings, "location_require_active_map_version", False) and location_map and not current_map:
        return False, "active robot map version is unknown, so the stored pose cannot be verified", []
    if location_map and not current_map:
        warnings.append(f"map version '{location_map}' is stored but active robot map version is unknown")
    if not validated_mode and current_map and location_map and current_map != location_map:
        warnings.append(f"integration mode ignored map version mismatch: robot={current_map}, location={location_map}")

    if validated_mode:
        return True, "location passed calibration/frame/map checks", warnings
    return True, "integration mode trusts the configured RAG location after frame/type/binding checks", warnings


def build_location_grounding(
    locations: dict[str, Any] | None,
    *,
    registry: dict[str, Any] | None = None,
    world_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve retrieved semantic locations into deterministic executable candidates.

    RAG decides which named locations are relevant. This resolver never invents a
    coordinate or capability: it combines a calibrated location record with an
    active SkillManifest location-binding contract and produces concrete arguments.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    nav_skills: dict[str, dict[str, Any]] = {}
    for sid, spec in skills.items():
        if str(spec.get("kind", "")).upper() != "ACTION":
            continue
        provides = {str(x) for x in (spec.get("provides") or [])}
        if provides.intersection(LOCATION_NAV_CAPABILITIES) and _infer_location_binding(spec):
            nav_skills[sid] = spec

    result_locations: dict[str, Any] = {}
    expanded: list[str] = []
    for location_id, location in (locations or {}).items():
        meta = location.get("metadata") or {}
        info: dict[str, Any] = {
            "location_id": str(location_id),
            "aliases": _location_aliases(str(location_id), location),
            "location_type": location.get("location_type") or meta.get("type"),
            "frame_id": location.get("frame_id") or meta.get("frame_id") or "map",
            "map_version": location.get("map_version") or meta.get("map_version"),
            "calibrated": bool(meta.get("calibrated", False)),
            "trust_mode": str(getattr(settings, "location_knowledge_mode", "integration")),
            "description": str(meta.get("description") or ""),
            "pose": _location_pose(location),
            "navigation_candidates": [],
            "actionable": False,
            "reason": "no compatible location-navigation skill is registered",
            "warnings": [],
        }
        compatibility_failures: list[str] = []
        for sid, spec in nav_skills.items():
            binding = _infer_location_binding(spec) or {}
            compatible, reason, warnings = _compatibility_reason(location, binding, world_state)
            if not compatible:
                compatibility_failures.append(reason)
                continue
            args, errors = bind_location_arguments(location, spec)
            if errors or args is None:
                compatibility_failures.extend(errors)
                continue
            info["navigation_candidates"].append({
                "skill_id": sid,
                "arguments": args,
                "provides": [str(x) for x in (spec.get("provides") or [])],
                "binding_source": "semantic_overlay" if spec.get("location_binding") else "inferred_from_formal_ports",
            })
            info["warnings"].extend(warnings)
            if sid not in expanded:
                expanded.append(sid)

        if info["navigation_candidates"]:
            info["actionable"] = True
            info["reason"] = "retrieved location has a deterministic navigation binding under the active trust mode"
        elif compatibility_failures:
            # Prefer the calibration reason because it is the most common and most
            # actionable operator issue; otherwise retain the first deterministic cause.
            calibration_reason = next((x for x in compatibility_failures if "not calibrated" in x), None)
            info["reason"] = calibration_reason or compatibility_failures[0]
        result_locations[str(location_id)] = info

    return {
        "enabled": bool(getattr(settings, "location_grounding_enabled", True)),
        "current_map_version": _world_map_version(world_state),
        "locations": result_locations,
        "expanded_skill_ids": expanded,
        "actionable_location_ids": [k for k, v in result_locations.items() if v.get("actionable")],
    }
