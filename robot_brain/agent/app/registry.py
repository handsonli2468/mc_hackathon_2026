from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .settings import settings


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML registry must be an object: {path}")
    return data


def load_skill_registry() -> dict[str, Any]:
    data = _load_yaml(settings.config_root / "skill_registry.yaml")
    data.setdefault("version", "1.0")
    data.setdefault("blackboard", {"externally_provided": {}})
    data.setdefault("skills", [])
    return data


def load_builtin_registry() -> dict[str, Any]:
    data = _load_yaml(settings.config_root / "builtin_bt_nodes.yaml")
    data.setdefault("nodes", [])
    return data


def load_bt_engine_semantic_overlay() -> dict[str, Any]:
    """Load planner-side semantics layered over the live BT-engine model.

    The BT engine is authoritative for node IDs, kinds, ports, types, and defaults.
    This overlay only supplies semantics that /nodes cannot know (capabilities,
    preconditions, verification policy, resource usage, grounding behavior, etc.).
    """
    data = _load_yaml(settings.config_root / "bt_engine_semantic_overlay.yaml")
    data.setdefault("version", "1.0")
    data.setdefault("nodes", {})
    if not isinstance(data.get("nodes"), dict):
        raise ValueError("bt_engine_semantic_overlay.yaml field 'nodes' must be an object.")
    return data


def load_bt_skill_policy() -> dict[str, Any]:
    data = _load_yaml(settings.config_root / "bt_skill_policy.yaml")
    data.setdefault("version", "1.0")
    data.setdefault("disabled_nodes", [])
    data.setdefault("required_planner_nodes", [])
    return data


def load_bt_engine_formal_registry() -> dict[str, Any]:
    """Load the generated formal registry containing every live custom engine node."""
    data = _load_yaml(settings.config_root / "bt_engine_registry.generated.yaml")
    data.setdefault("version", "1.0")
    data.setdefault("nodes", [])
    return data


def skill_map(registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = registry or load_skill_registry()
    return {str(s["id"]): s for s in registry.get("skills", []) if isinstance(s, dict) and s.get("id")}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def capability_index(registry: dict[str, Any] | None = None) -> dict[str, str]:
    registry = registry or load_skill_registry()
    out: dict[str, str] = {}
    for skill in registry.get("skills", []):
        sid = str(skill.get("id", ""))
        if not sid:
            continue
        for value in [sid, *(skill.get("provides", []) or [])]:
            out[_norm(str(value))] = sid
    return out


def effective_capabilities(skill: dict[str, Any], arguments: dict[str, Any] | None = None) -> set[str]:
    """Return capabilities whose optional argument rules match this invocation.

    Most skills expose unconditional capabilities. Multi-mode actions such as
    SetGripper can expose different effects for different port values without
    hard-coding a robot-specific node ID in mission validators.
    """
    provided = {str(x) for x in (skill.get("provides", []) or [])}
    rules = skill.get("capability_rules", {}) or {}
    args = arguments or {}
    for capability, rule in rules.items():
        if not isinstance(rule, dict):
            continue
        port = str(rule.get("port") or "")
        if not port or port not in args:
            provided.discard(str(capability))
            continue
        value = args.get(port)
        try:
            if "equals" in rule:
                expected = rule["equals"]
                try:
                    equal = float(value) == float(expected)
                except (TypeError, ValueError):
                    equal = str(value) == str(expected)
                if not equal:
                    provided.discard(str(capability))
            if "min_exclusive" in rule and float(value) <= float(rule["min_exclusive"]):
                provided.discard(str(capability))
            if "min" in rule and float(value) < float(rule["min"]):
                provided.discard(str(capability))
            if "max" in rule and float(value) > float(rule["max"]):
                provided.discard(str(capability))
        except (TypeError, ValueError):
            provided.discard(str(capability))
    return provided


def check_capabilities(required: list[str], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    index = capability_index(registry)
    matched: dict[str, str] = {}
    missing: list[str] = []
    for cap in required:
        key = _norm(cap)
        if key in index:
            matched[cap] = index[key]
        else:
            missing.append(cap)
    return {"ok": not missing, "matched": matched, "missing": missing}


def skill_registry_for_prompt(registry: dict[str, Any] | None = None) -> str:
    registry = registry or load_skill_registry()
    return yaml.safe_dump(registry, sort_keys=False, allow_unicode=True)


def capability_catalog_for_prompt(registry: dict[str, Any] | None = None) -> str:
    registry = registry or load_skill_registry()
    payload = []
    for s in registry.get("skills", []):
        payload.append(
            {
                "skill": s.get("id"),
                "kind": s.get("kind"),
                "provides": s.get("provides", []),
                "description": s.get("description", ""),
            }
        )
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def skill_registry_as_bt_nodes(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_skill_registry()
    ext = (registry.get("blackboard", {}) or {}).get("externally_provided", {}) or {}
    nodes = []
    for skill in registry.get("skills", []):
        inputs = skill.get("inputs", {}) or {}
        outputs = skill.get("outputs", {}) or {}
        nodes.append(
            {
                "id": skill.get("id"),
                "kind": "Condition" if str(skill.get("kind", "ACTION")).upper() == "CONDITION" else "Action",
                "description": skill.get("description", ""),
                "children": {"min": 0, "max": 0},
                "ports": {"input": inputs, "output": outputs},
                "continuous_search_trigger": skill.get("continuous_search_trigger", False),
                "search_policy": skill.get("search_policy", {}),
                "search_policy_role": skill.get("search_policy_role"),
            }
        )
    return {
        "version": registry.get("version"),
        "blackboard": {"externally_provided": ext},
        "nodes": nodes,
    }


def allowed_actions_text(registry: dict[str, Any] | None = None) -> str:
    """BTGenBot-native-ish closed action vocabulary.

    Keep this close to the released BTGenBot-2 Task/Actions interface: parameter
    names only, without custom '[output]' syntax that the model was not trained on.
    Port directions are described separately by bt_port_contract_for_prompt().
    """

    registry = registry or load_skill_registry()
    items = []
    for skill in registry.get("skills", []):
        params = [*(skill.get("inputs", {}) or {}).keys(), *(skill.get("outputs", {}) or {}).keys()]
        items.append(f"{skill.get('id')} (parameters: {', '.join(params)})")
    return "[" + ", ".join(items) + "]"


def _is_primitive(type_name: str) -> bool:
    return str(type_name or "string").lower() in {
        "string",
        "std::string",
        "str",
        "int",
        "integer",
        "unsigned int",
        "uint",
        "uint32_t",
        "float",
        "double",
        "number",
        "bool",
        "boolean",
        "any",
    }


def bt_port_contract_for_prompt(registry: dict[str, Any] | None = None) -> str:
    registry = registry or load_skill_registry()
    lines = [
        "BehaviorTree.CPP leaf-port binding contract:",
        "- XML attribute names MUST be the declared port names; never invent generic attributes such as output= or result=.",
        "- Every OUTPUT port MUST be bound to a blackboard key using {key} syntax.",
        "- Opaque/non-primitive INPUT types (for example TrackedObject or PoseStamped) MUST read from a blackboard key using {key} syntax.",
        "- Primitive INPUT types (string/int/float/bool) may use literals or blackboard references.",
        "- Reuse exactly the same blackboard key when passing one skill's output into later skills.",
        "",
    ]
    for skill in registry.get("skills", []):
        sid = str(skill.get("id", ""))
        if not sid:
            continue
        lines.append(f"{sid} [{str(skill.get('kind', 'ACTION')).upper()}]")
        inputs = skill.get("inputs", {}) or {}
        outputs = skill.get("outputs", {}) or {}
        if not inputs and not outputs:
            lines.append("  ports: none")
        for name, spec in inputs.items():
            t = str(spec.get("type", "any"))
            required = "required" if spec.get("required") else "optional"
            binding = "literal-or-{blackboard}" if _is_primitive(t) else "{blackboard}-only"
            lines.append(f"  input  {name}: {t}, {required}, binding={binding}")
        for name, spec in outputs.items():
            t = str(spec.get("type", "any"))
            required = "required" if spec.get("required") else "optional"
            lines.append(f"  output {name}: {t}, {required}, binding={{blackboard}}-only")
    return "\n".join(lines)


def bt_binding_example_for_prompt(registry: dict[str, Any] | None = None) -> str:
    """Create a small deterministic port-binding example from the active registry."""

    registry = registry or load_skill_registry()
    skills = registry.get("skills", []) or []
    producer = None
    producer_port = None
    producer_type = None
    for skill in skills:
        outputs = skill.get("outputs", {}) or {}
        if outputs:
            producer = skill
            producer_port, spec = next(iter(outputs.items()))
            producer_type = str(spec.get("type", "any"))
            break
    if producer is None or producer_port is None:
        return (
            "Current runtime registry declares no leaf output ports.\n"
            "Do not invent blackboard outputs or synthetic object handles. Use the declared primitive input ports directly; "
            "for named-object nodes, reuse the same literal object_name string (for example object_name=\"bottle\")."
        )

    consumer = None
    consumer_port = None
    for skill in skills:
        if skill is producer:
            continue
        for name, spec in (skill.get("inputs", {}) or {}).items():
            if str(spec.get("type", "any")) == producer_type:
                consumer = skill
                consumer_port = name
                break
        if consumer:
            break

    producer_inputs = producer.get("inputs", {}) or {}
    producer_attrs = [f'name="example_{str(producer.get("id")).lower()}"']
    for name, spec in producer_inputs.items():
        t = str(spec.get("type", "string"))
        if _is_primitive(t):
            sample = "example" if t.lower() in {"string", "str", "any"} else ("1" if t.lower() not in {"bool", "boolean"} else "true")
            producer_attrs.append(f'{name}="{sample}"')
        else:
            producer_attrs.append(f'{name}="{{input_{name}}}"')
    producer_attrs.append(f'{producer_port}="{{shared_{producer_port}}}"')
    pxml = f"<{producer.get('id')} {' '.join(producer_attrs)}/>"

    if not consumer or not consumer_port:
        return "Correct output binding example:\n" + pxml

    cxml = (
        f'<{consumer.get("id")} name="example_{str(consumer.get("id")).lower()}" '
        f'{consumer_port}="{{shared_{producer_port}}}"/>'
    )
    return (
        "Correct producer/consumer binding example:\n"
        + pxml
        + "\n"
        + cxml
        + "\nThe output port name itself becomes the XML attribute name; its value is the shared blackboard reference."
    )
