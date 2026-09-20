from __future__ import annotations

from collections import defaultdict
from typing import Any

from defusedxml import ElementTree as SafeET

from .compiler_adapter import build_invocation_plan
from .registry import load_builtin_registry, load_skill_registry, skill_map
from .schemas import TaskPlanIR

_WRAPPER_TAGS = {"Action", "Condition", "Control", "Decorator"}


def _unique_casefold_map(names: list[str]) -> dict[str, str]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for name in names:
        buckets[name.casefold()].append(name)
    return {key: values[0] for key, values in buckets.items() if len(values) == 1}


def normalize_compiled_bt_xml(
    xml: str,
    ir: TaskPlanIR,
    skill_registry: dict[str, Any] | None = None,
    builtin_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize BTGenBot serialization without changing control-flow semantics.

    Safe transformations only:
    - explicit <Action ID="X"> / <Condition ID="X"> -> canonical registered tag;
    - unique case-insensitive leaf/builtin ID correction;
    - authoritative TaskPlanIR input/output port binding;
    - removal of undeclared or model-invented, unplanned leaf attributes;
    - deterministic unique node names;
    - root main-tree/BTCPP metadata normalization.

    Sequence/Fallback/Retry topology is intentionally *not* rewritten here. Semantic
    control-flow mistakes must be rejected and repaired rather than silently changed.
    """
    skill_registry = skill_registry or load_skill_registry()
    builtin_registry = builtin_registry or load_builtin_registry()
    skills = skill_map(skill_registry)
    builtin_ids = [str(x.get("id")) for x in builtin_registry.get("nodes", []) if x.get("id")]
    leaf_case = _unique_casefold_map(list(skills))
    builtin_case = _unique_casefold_map(builtin_ids)
    invocations = build_invocation_plan(
        ir,
        skill_registry,
        include_compiled_search_recovery=True,
        include_compiled_ensure_state=True,
    )
    by_skill: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in invocations:
        by_skill[item["skill"]].append(item)
    occurrence: dict[str, int] = defaultdict(int)
    changes: list[str] = []

    try:
        root = SafeET.fromstring(xml)
    except Exception as exc:
        return {"xml": xml, "changes": [], "error": f"XML parse error before normalization: {exc}"}

    if root.tag == "root":
        if root.attrib.get("BTCPP_format") != "4":
            root.set("BTCPP_format", "4")
            changes.append("set root BTCPP_format=4")
        if not root.attrib.get("main_tree_to_execute"):
            trees = [x for x in list(root) if x.tag == "BehaviorTree" and x.attrib.get("ID")]
            if len(trees) == 1:
                root.set("main_tree_to_execute", trees[0].attrib["ID"])
                changes.append(f"set main_tree_to_execute={trees[0].attrib['ID']}")

    seen_names: set[str] = set()
    generic_counter: dict[str, int] = defaultdict(int)

    for el in root.iter():
        if el.tag in {"root", "BehaviorTree"}:
            continue

        original_tag = str(el.tag)
        candidate = original_tag
        wrapper_id = None
        if original_tag in _WRAPPER_TAGS and el.attrib.get("ID"):
            wrapper_id = str(el.attrib.get("ID"))
            candidate = wrapper_id

        canonical = None
        if candidate in skills or candidate in builtin_ids:
            canonical = candidate
        else:
            canonical = leaf_case.get(candidate.casefold()) or builtin_case.get(candidate.casefold())

        if canonical:
            if original_tag != canonical:
                changes.append(f"canonicalized node {original_tag}{' ID=' + wrapper_id if wrapper_id else ''} -> {canonical}")
                el.tag = canonical
            if wrapper_id is not None:
                el.attrib.pop("ID", None)

        tag = str(el.tag)
        invocation = None
        if tag in skills:
            items = by_skill.get(tag, [])
            idx = occurrence[tag]
            if items:
                invocation = items[min(idx, len(items) - 1)]
            occurrence[tag] += 1

            spec = skills[tag]
            declared = set((spec.get("inputs", {}) or {})) | set((spec.get("outputs", {}) or {}))
            # Remove hallucinated leaf attributes. The TaskPlanIR + SkillManifest are
            # authoritative for ports; 'name' is structural metadata.
            for attr in list(el.attrib):
                if attr != "name" and attr not in declared:
                    changes.append(f"removed undeclared {tag}.{attr}")
                    el.attrib.pop(attr, None)

            if invocation:
                # BTGenBot owns only the small phase topology.  TaskPlanIR owns
                # every executable leaf argument.  In particular, do not preserve
                # an optional declared port invented by the compiler (observed as
                # speed="0.1"/"0.3"/"0.5" even though the live ABI is the enum
                # slow|normal|fast).  If the planner omitted an optional port,
                # remove it and let the formal engine default apply.
                planned_ports = set(invocation["ports"])
                for attr in list(el.attrib):
                    if attr != "name" and attr in declared and attr not in planned_ports:
                        changes.append(f"removed unplanned {tag}.{attr}; engine default applies")
                        el.attrib.pop(attr, None)
                for port, value in invocation["ports"].items():
                    old = el.attrib.get(port)
                    if old != value:
                        el.set(port, value)
                        changes.append(f"bound {tag}.{port}={value}")
                desired_name = invocation["node_name"]
                if el.attrib.get("name") != desired_name:
                    el.set("name", desired_name)
                    changes.append(f"named {tag} as {desired_name}")

        # Every non-SubTree node gets a deterministic unique name for downstream
        # execution tracing. Invocation-owned names above take precedence.
        if tag != "SubTree":
            current_name = el.attrib.get("name")
            if not current_name or current_name in seen_names:
                generic_counter[tag] += 1
                base = f"{tag.lower()}_{generic_counter[tag]}"
                name = base
                suffix = 2
                while name in seen_names:
                    name = f"{base}_{suffix}"
                    suffix += 1
                el.set("name", name)
                changes.append(f"assigned unique name {name} to {tag}")
                current_name = name
            seen_names.add(current_name)

    return {"xml": SafeET.tostring(root, encoding="unicode"), "changes": changes, "error": None}
