from __future__ import annotations

from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET

from .registry import (
    load_bt_engine_semantic_overlay,
    load_bt_skill_policy,
    load_builtin_registry,
    load_skill_registry,
    skill_map,
)
from .settings import settings


LEAF_KINDS = {"ACTION", "CONDITION"}


def _normalized_type(value: str | None) -> str:
    text = str(value or "").strip()
    aliases = {
        "string": "std::string",
        "str": "std::string",
        "float": "double",
        "integer": "int",
        "boolean": "bool",
    }
    return aliases.get(text, text)


def _find_tree_nodes_model(root: Any) -> Any:
    if getattr(root, "tag", None) == "TreeNodesModel":
        return root
    model = root.find("TreeNodesModel")
    if model is None:
        model = root.find(".//TreeNodesModel")
    return model


def parse_bt_engine_tree_nodes_model(xml_text: str) -> dict[str, dict[str, Any]]:
    """Parse a BehaviorTree.CPP TreeNodesModel without dropping any declared node.

    In addition to input ports used by the original v6.0.1 contract checker, this
    parser preserves output/inout ports and their human-readable descriptions so
    synchronization can build a complete formal registry from the live engine.
    """
    root = SafeET.fromstring(xml_text)
    model = _find_tree_nodes_model(root)
    if model is None:
        raise ValueError("BT-engine XML does not contain <TreeNodesModel>.")

    out: dict[str, dict[str, Any]] = {}
    for node in list(model):
        node_id = str(node.attrib.get("ID", "")).strip()
        if not node_id:
            continue
        # BehaviorTree.CPP also serializes registered tree manifests as
        # <SubTree ID="GoHome"/> entries.  GoHome is a callable tree ID, not a
        # node type that may appear as <GoHome/> in generated XML.  The generic
        # <SubTree ID="..."/> construct is defined by builtin_bt_nodes.yaml, so
        # these manifests must not inflate the custom-node count or require a
        # planner semantic overlay.
        if str(node.tag).lower() == "subtree":
            continue
        if node_id in out:
            raise ValueError(f"Duplicate BT-engine node ID: {node_id}")

        ports: dict[str, dict[str, dict[str, Any]]] = {"input": {}, "output": {}, "inout": {}}
        node_description = str(node.attrib.get("description", "") or "").strip()
        for child in list(node):
            tag = str(child.tag)
            if tag not in {"input_port", "output_port", "inout_port"}:
                if tag == "description" and not node_description:
                    node_description = str(child.text or "").strip()
                continue
            direction = {"input_port": "input", "output_port": "output", "inout_port": "inout"}[tag]
            name = str(child.attrib.get("name", "")).strip()
            if not name:
                continue
            spec: dict[str, Any] = {
                "type": _normalized_type(child.attrib.get("type")),
                "default": child.attrib.get("default"),
            }
            desc = str(child.text or child.attrib.get("description", "") or "").strip()
            if desc:
                spec["description"] = desc
            ports[direction][name] = spec

        out[node_id] = {
            "id": node_id,
            "kind": str(node.tag).upper(),
            "description": node_description,
            "inputs": ports["input"],
            "outputs": ports["output"],
            "inouts": ports["inout"],
        }
    return out


def load_bt_engine_tree_nodes_model(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or (settings.config_root / "bt_engine_nodes.xml")
    if not path.exists():
        source_fallback = Path(__file__).resolve().parents[1] / "defaults" / "bt_engine_nodes.xml"
        project_fallback = settings.project_root / "agent" / "defaults" / "bt_engine_nodes.xml"
        path = source_fallback if source_fallback.exists() else project_fallback
    return parse_bt_engine_tree_nodes_model(path.read_text(encoding="utf-8"))


def _coerce_default(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    t = _normalized_type(type_name).lower()
    try:
        if t == "int":
            return int(str(value).strip())
        if t == "double":
            return float(str(value).strip())
        if t == "bool":
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return value
    return value


def _formal_port(spec: dict[str, Any], *, direction: str) -> dict[str, Any]:
    normalized_type = _normalized_type(spec.get("type"))
    result: dict[str, Any] = {
        "type": normalized_type,
        "required": direction in {"input", "inout"} and spec.get("default") is None,
    }
    if spec.get("default") is not None:
        result["default"] = _coerce_default(spec.get("default"), normalized_type)
    if spec.get("description"):
        result["description"] = spec.get("description")
    return result


def engine_model_as_registry(engine: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Serialize every live engine node into a deterministic formal YAML registry."""
    nodes: list[dict[str, Any]] = []
    for node_id in sorted(engine):
        spec = engine[node_id]
        node: dict[str, Any] = {
            "id": node_id,
            "kind": spec.get("kind"),
            "description": spec.get("description", ""),
            "ports": {
                "input": {k: _formal_port(v, direction="input") for k, v in (spec.get("inputs") or {}).items()},
                "output": {k: _formal_port(v, direction="output") for k, v in (spec.get("outputs") or {}).items()},
                "inout": {k: _formal_port(v, direction="inout") for k, v in (spec.get("inouts") or {}).items()},
            },
        }
        nodes.append(node)
    return {
        "version": "bt-engine-live-generated-v2",
        "source": "GET /nodes?builtin=0",
        "nodes": nodes,
    }


def _merge_port_semantics(formal: dict[str, Any], semantic: dict[str, Any] | None, *, direction: str) -> dict[str, Any]:
    out = _formal_port(formal, direction=direction)
    # Formal contract remains authoritative. Semantic overlay may add constraints
    # such as allowed_values/literal_only but cannot replace type/default/required.
    for key, value in (semantic or {}).items():
        if key in {"type", "default", "required"}:
            continue
        out[key] = value
    return out


def _semantic_overlay_issues(
    node_id: str,
    formal: dict[str, Any],
    semantic: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if not str(semantic.get("description") or formal.get("description") or "").strip():
        issues.append("needs a non-empty description")
    provides = [str(x).strip() for x in (semantic.get("provides") or []) if str(x).strip()]
    if not provides:
        issues.append("needs at least one 'provides' capability")
    valid_inputs = set(formal.get("inputs") or {}) | set(formal.get("inouts") or {})
    valid_outputs = set(formal.get("outputs") or {}) | set(formal.get("inouts") or {})
    unknown_inputs = sorted(set(semantic.get("inputs", {}) or {}) - valid_inputs)
    unknown_outputs = sorted(set(semantic.get("outputs", {}) or {}) - valid_outputs)
    unknown_inouts = sorted(set(semantic.get("inouts", {}) or {}) - set(formal.get("inouts") or {}))
    if unknown_inputs:
        issues.append("references unknown input ports: " + ", ".join(unknown_inputs))
    if unknown_outputs:
        issues.append("references unknown output ports: " + ", ".join(unknown_outputs))
    if unknown_inouts:
        issues.append("references unknown inout ports: " + ", ".join(unknown_inouts))
    return issues


def build_merged_skill_registry(
    engine: dict[str, dict[str, Any]],
    overlay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge live formal node contracts with local planner semantics.

    Every engine node is accounted for. Action/Condition nodes with a planner-enabled
    semantic overlay are promoted into the active Skill Registry. Leaves without an
    overlay are deliberately quarantined: they remain present in the generated formal
    engine registry and sync report, but are not exposed to the LLM/RAG with invented
    semantics.
    """
    overlay = overlay or load_bt_engine_semantic_overlay()
    semantic_nodes = overlay.get("nodes", {}) or {}
    policy = load_bt_skill_policy()
    policy_disabled = {str(x) for x in (policy.get("disabled_nodes") or [])}
    required_planner_nodes = {str(x) for x in (policy.get("required_planner_nodes") or [])}
    skills: list[dict[str, Any]] = []
    missing_semantics: list[str] = []
    invalid_semantics: dict[str, list[str]] = {}
    disabled_semantics: list[str] = []
    nonleaf_nodes: list[str] = []
    stale_overlay_nodes = sorted(set(semantic_nodes) - set(engine))

    for node_id in sorted(engine):
        formal = engine[node_id]
        kind = str(formal.get("kind", "")).upper()
        if kind not in LEAF_KINDS:
            nonleaf_nodes.append(node_id)
            continue

        if node_id in policy_disabled:
            disabled_semantics.append(node_id)
            continue

        semantic = semantic_nodes.get(node_id)
        if not isinstance(semantic, dict):
            missing_semantics.append(node_id)
            continue
        if semantic.get("planner_enabled", True) is False:
            disabled_semantics.append(node_id)
            continue
        issues = _semantic_overlay_issues(node_id, formal, semantic)
        if issues:
            invalid_semantics[node_id] = issues
            continue

        skill: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "description": str(semantic.get("description") or formal.get("description") or f"BT Engine {kind.title()} node {node_id}."),
        }
        for key, value in semantic.items():
            if key in {"planner_enabled", "description", "inputs", "outputs", "inouts", "id", "kind"}:
                continue
            skill[key] = value

        semantic_inputs = semantic.get("inputs", {}) or {}
        semantic_outputs = semantic.get("outputs", {}) or {}
        semantic_inouts = semantic.get("inouts", {}) or {}
        skill["inputs"] = {
            name: _merge_port_semantics(spec, semantic_inputs.get(name), direction="input")
            for name, spec in (formal.get("inputs") or {}).items()
        }
        skill["outputs"] = {
            name: _merge_port_semantics(spec, semantic_outputs.get(name), direction="output")
            for name, spec in (formal.get("outputs") or {}).items()
        }
        # The rest of the codebase currently models inout as both a readable input
        # and a writable output. Preserve an explicit marker so future nodes are not
        # silently dropped while keeping compatibility with existing validators.
        for name, spec in (formal.get("inouts") or {}).items():
            merged = _merge_port_semantics(spec, semantic_inouts.get(name), direction="inout")
            merged["inout"] = True
            skill["inputs"][name] = dict(merged)
            skill["outputs"][name] = dict(merged)

        skills.append(skill)

    # Verification dependencies are semantic, not part of the formal /nodes schema.
    # Do not expose an action to Planner/RAG if its required verification node is
    # unavailable/quarantined or is not a Condition. Iterate to a fixed point so a
    # dependency chain cannot leave a partially-valid planner registry behind.
    dependency_issues: dict[str, list[str]] = {}
    changed = True
    while changed:
        changed = False
        by_id = {str(skill.get("id")): skill for skill in skills}
        kept: list[dict[str, Any]] = []
        for skill in skills:
            node_id = str(skill.get("id"))
            issues: list[str] = []
            for verify_id in skill.get("recommended_verification", []) or []:
                verify_id = str(verify_id)
                target = by_id.get(verify_id)
                if target is None:
                    issues.append(f"recommended verification '{verify_id}' is unavailable/quarantined")
                elif str(target.get("kind", "")).upper() != "CONDITION":
                    issues.append(f"recommended verification '{verify_id}' is not a CONDITION")
            if issues:
                dependency_issues[node_id] = issues
                changed = True
                continue
            kept.append(skill)
        skills = kept

    if dependency_issues:
        invalid_semantics.update(dependency_issues)

    planner_ids = {str(skill.get("id")) for skill in skills}
    required_planner_node_issues: dict[str, str] = {}
    for node_id in sorted(required_planner_nodes):
        if node_id not in engine:
            required_planner_node_issues[node_id] = "required planner node is not present in live BT Engine /nodes"
        elif node_id not in planner_ids:
            required_planner_node_issues[node_id] = "required planner node is present formally but is not planner-enabled with a valid semantic overlay"

    registry = {
        "version": "bt-engine-live-merged-v2",
        "source_model": "bt_engine_nodes.xml",
        "semantic_overlay": "bt_engine_semantic_overlay.yaml",
        "blackboard": {"externally_provided": {}},
        "skills": skills,
        "contract_metadata": {
            "engine_ids_kinds_ports": "authoritative_from_live_bt_engine_nodes",
            "semantic_annotations": "local_overlay_only_no_auto_invention",
            "missing_semantic_overlay": missing_semantics,
            "invalid_semantic_overlay": invalid_semantics,
            "semantic_dependency_issues": dependency_issues,
            "planner_disabled_nodes": disabled_semantics,
            "policy_disabled_nodes": sorted(policy_disabled),
            "required_planner_node_issues": required_planner_node_issues,
            "stale_overlay_nodes": stale_overlay_nodes,
        },
    }
    report = {
        "engine_node_count": len(engine),
        "engine_leaf_count": sum(1 for x in engine.values() if str(x.get("kind", "")).upper() in LEAF_KINDS),
        "planner_skill_count": len(skills),
        "missing_semantic_overlay": missing_semantics,
        "invalid_semantic_overlay": invalid_semantics,
        "semantic_dependency_issues": dependency_issues,
        "planner_disabled_nodes": disabled_semantics,
        "policy_disabled_nodes": sorted(policy_disabled),
        "required_planner_node_issues": required_planner_node_issues,
        "stale_overlay_nodes": stale_overlay_nodes,
        "engine_nonleaf_nodes": nonleaf_nodes,
        "formal_ingested_node_ids": sorted(engine),
        "planner_skill_ids": sorted(str(skill.get("id")) for skill in skills),
        "quarantined_leaf_nodes": sorted(set(missing_semantics) | set(invalid_semantics) | set(disabled_semantics)),
        "all_engine_nodes_formally_ingested": True,
        "semantic_complete": not missing_semantics and not invalid_semantics and not required_planner_node_issues,
    }
    return registry, report


def _same_default(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b

    # XML attributes are strings while YAML preserves booleans.  Compare explicit
    # boolean spellings before numeric coercion so False matches "false" without
    # accidentally treating an unrelated numeric default as a boolean.
    def as_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
        return None

    bool_a = as_bool(a)
    bool_b = as_bool(b)
    if bool_a is not None or bool_b is not None:
        return bool_a is not None and bool_b is not None and bool_a == bool_b
    try:
        return float(a) == float(b)
    except Exception:
        return str(a) == str(b)


def validate_runtime_contract(
    skill_registry: dict[str, Any] | None = None,
    builtin_registry: dict[str, Any] | None = None,
    engine_model_path: Path | None = None,
    semantic_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the active planner registry is a safe semantic projection of /nodes.

    Unlike v6.0.1, a brand-new engine leaf is no longer treated as a hard structural
    mismatch that prevents formal synchronization. It is ingested formally and listed
    under ``missing_semantic_overlay`` until a local semantic overlay promotes it into
    the planner registry. Any planner-exposed skill, however, must match the live node
    ID, kind and ports exactly.
    """
    skill_registry = skill_registry or load_skill_registry()
    builtin_registry = builtin_registry or load_builtin_registry()
    semantic_overlay = semantic_overlay or load_bt_engine_semantic_overlay()
    engine = load_bt_engine_tree_nodes_model(engine_model_path)
    skills = skill_map(skill_registry)
    builtins = {str(n.get("id")): n for n in builtin_registry.get("nodes", []) if isinstance(n, dict) and n.get("id")}

    errors: list[str] = []
    warnings: list[str] = []
    engine_leaf_ids = {nid for nid, spec in engine.items() if spec["kind"] in LEAF_KINDS}
    registry_leaf_ids = set(skills)

    extra = sorted(registry_leaf_ids - engine_leaf_ids)
    if extra:
        errors.append("Skill registry contains leaves not registered by BT engine: " + ", ".join(extra))

    overlay_nodes = semantic_overlay.get("nodes", {}) or {}
    expected_registry, semantic_report = build_merged_skill_registry(engine, semantic_overlay)
    expected_planner = set(skill_map(expected_registry))
    invalid_semantics: dict[str, list[str]] = dict(semantic_report.get("invalid_semantic_overlay", {}) or {})

    # Backward-compatible formal-only validation: callers that explicitly provide a
    # legacy skill registry but no semantic overlay can still verify ID/kind/ports.
    # Runtime v6.1 always bootstraps an overlay before synchronization.
    if overlay_nodes:
        missing_from_registry = sorted(expected_planner - registry_leaf_ids)
        if missing_from_registry:
            errors.append("Planner-enabled semantic overlays are missing from skill registry: " + ", ".join(missing_from_registry))
        unexpected_planner = sorted((registry_leaf_ids & engine_leaf_ids) - expected_planner)
        if unexpected_planner:
            errors.append("Skill registry exposes leaves that are disabled/quarantined by semantic policy: " + ", ".join(unexpected_planner))

    missing_semantics = list(semantic_report.get("missing_semantic_overlay", []) or [])
    disabled_semantics = list(semantic_report.get("planner_disabled_nodes", []) or [])
    stale_overlay = list(semantic_report.get("stale_overlay_nodes", []) or [])
    semantic_dependency_issues = dict(semantic_report.get("semantic_dependency_issues", {}) or {})
    required_planner_node_issues = dict(semantic_report.get("required_planner_node_issues", {}) or {})
    policy_disabled_nodes = list(semantic_report.get("policy_disabled_nodes", []) or [])
    builtin_ids = set(builtins)
    effective_node_ids = set(engine) | builtin_ids

    for node_id in sorted(engine_leaf_ids & registry_leaf_ids):
        engine_spec = engine[node_id]
        reg = skills[node_id]
        reg_kind = str(reg.get("kind", "")).upper()
        if reg_kind != engine_spec["kind"]:
            errors.append(f"{node_id} kind mismatch: registry={reg_kind}, engine={engine_spec['kind']}.")

        reg_inputs = reg.get("inputs", {}) or {}
        engine_inputs = dict(engine_spec.get("inputs") or {})
        engine_inputs.update(engine_spec.get("inouts") or {})
        if set(reg_inputs) != set(engine_inputs):
            errors.append(f"{node_id} input-port mismatch: registry={sorted(reg_inputs)}, engine={sorted(engine_inputs)}.")
        else:
            for port, es in engine_inputs.items():
                rt = _normalized_type((reg_inputs.get(port) or {}).get("type"))
                et = _normalized_type(es.get("type"))
                if rt != et:
                    errors.append(f"{node_id}.{port} type mismatch: registry={rt}, engine={et}.")
                if es.get("default") is not None and not _same_default((reg_inputs.get(port) or {}).get("default"), es.get("default")):
                    errors.append(
                        f"{node_id}.{port} default mismatch: registry={(reg_inputs.get(port) or {}).get('default')}, engine={es.get('default')}."
                    )

        reg_outputs = reg.get("outputs", {}) or {}
        engine_outputs = dict(engine_spec.get("outputs") or {})
        engine_outputs.update(engine_spec.get("inouts") or {})
        if set(reg_outputs) != set(engine_outputs):
            errors.append(f"{node_id} output-port mismatch: registry={sorted(reg_outputs)}, engine={sorted(engine_outputs)}.")
        else:
            for port, es in engine_outputs.items():
                rt = _normalized_type((reg_outputs.get(port) or {}).get("type"))
                et = _normalized_type(es.get("type"))
                if rt != et:
                    errors.append(f"{node_id}.{port} output type mismatch: registry={rt}, engine={et}.")

    for node_id, reg in sorted(skills.items()):
        for verify_id in reg.get("recommended_verification", []) or []:
            verify_id = str(verify_id)
            target = skills.get(verify_id)
            if target is None:
                errors.append(f"{node_id} recommends unknown/unavailable verification skill '{verify_id}'.")
            elif str(target.get("kind", "")).upper() != "CONDITION":
                errors.append(f"{node_id} recommended verification '{verify_id}' is not a CONDITION.")

    # Known custom controls can carry stronger local constraints than /nodes can
    # describe (e.g. RecoveryNode exactly two children). Check formal ports whenever
    # the engine exports the control and the local builtin registry knows it.
    for node_id, engine_spec in sorted(engine.items()):
        if engine_spec["kind"] in LEAF_KINDS:
            continue
        local = builtins.get(node_id)
        if local is None:
            continue
        ports = ((local.get("ports") or {}).get("input") or {})
        engine_inputs = dict(engine_spec.get("inputs") or {})
        engine_inputs.update(engine_spec.get("inouts") or {})
        if set(ports) != set(engine_inputs):
            errors.append(f"{node_id} input-port mismatch: registry={sorted(ports)}, engine={sorted(engine_inputs)}.")
            continue
        for port, es in engine_inputs.items():
            rt = _normalized_type((ports.get(port) or {}).get("type"))
            et = _normalized_type(es.get("type"))
            if rt != et:
                errors.append(f"{node_id}.{port} type mismatch: registry={rt}, engine={et}.")
            if es.get("default") is not None and not _same_default((ports.get(port) or {}).get("default"), es.get("default")):
                errors.append(f"{node_id}.{port} default mismatch: registry={(ports.get(port) or {}).get('default')}, engine={es.get('default')}.")

    recovery = builtins.get("RecoveryNode")
    if engine.get("RecoveryNode") and recovery:
        children = recovery.get("children", {}) or {}
        if int(children.get("min", 0)) != 2 or int(children.get("max", 0)) != 2:
            errors.append("RecoveryNode must have exactly two children in builtin_bt_nodes.yaml.")

    if missing_semantics:
        warnings.append(
            "Live BT-engine leaves are formally synchronized but quarantined from Planner/RAG until semantic overlay is provided: "
            + ", ".join(missing_semantics)
        )
    if invalid_semantics:
        warnings.append(
            "BT-engine leaves have invalid semantic overlays and are quarantined from Planner/RAG: "
            + "; ".join(f"{node}: {', '.join(issues)}" for node, issues in sorted(invalid_semantics.items()))
        )
    if disabled_semantics:
        warnings.append("Planner-disabled BT-engine leaves: " + ", ".join(disabled_semantics))
    if stale_overlay:
        warnings.append("Semantic overlay entries no longer exported by BT engine: " + ", ".join(stale_overlay))
    if required_planner_node_issues:
        warnings.append("Required planner node issues: " + "; ".join(f"{k}: {v}" for k, v in sorted(required_planner_node_issues.items())))
    if not errors:
        warnings.append("Every planner-exposed skill matches the live BT-engine node ID/kind/port contract.")

    return {
        "valid": not errors,
        "semantic_complete": not missing_semantics and not invalid_semantics and not required_planner_node_issues,
        "errors": errors,
        "warnings": warnings,
        "missing_semantic_overlay": missing_semantics,
        "invalid_semantic_overlay": invalid_semantics,
        "semantic_dependency_issues": semantic_dependency_issues,
        "planner_disabled_nodes": disabled_semantics,
        "policy_disabled_nodes": policy_disabled_nodes,
        "required_planner_node_issues": required_planner_node_issues,
        "stale_overlay_nodes": stale_overlay,
        # engine_node_count is retained for API compatibility. At runtime the
        # synchronized model comes from /nodes?builtin=0, so this normally means
        # the seven live custom leaves rather than the complete executable set.
        "engine_node_count": len(engine),
        "engine_exported_node_count": len(engine),
        "engine_leaf_count": len(engine_leaf_ids),
        "builtin_node_count": len(builtin_ids),
        "effective_node_count": len(effective_node_ids),
        "planner_skill_count": len(registry_leaf_ids),
        "contract_source": "live /nodes?builtin=0 + builtin_bt_nodes.yaml + bt_engine_semantic_overlay.yaml",
    }
