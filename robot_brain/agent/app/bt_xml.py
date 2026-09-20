from __future__ import annotations

import re
from typing import Any

from defusedxml import ElementTree as SafeET

ROOT_RE = re.compile(r"<root\b[\s\S]*?</root>", re.IGNORECASE)
BLACKBOARD_RE = re.compile(r"^\{([^{}]+)\}$")
PRIMITIVE_TYPES = {"string", "std::string", "str", "int", "integer", "unsigned int", "uint", "uint32_t", "float", "double", "number", "bool", "boolean", "any"}


def extract_bt_xml(text: str) -> str | None:
    """Return the last complete <root>...</root> block, matching BTGenBot-2's official inference notebook behavior."""
    matches = ROOT_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def normalize_bt_xml(xml: str) -> str:
    """Apply only safe, deterministic syntax normalization before validation."""
    try:
        root = SafeET.fromstring(xml)
    except Exception:
        return xml
    if root.tag == "root" and not root.attrib.get("main_tree_to_execute"):
        trees = [x for x in list(root) if x.tag == "BehaviorTree" and x.attrib.get("ID")]
        if len(trees) == 1:
            root.set("main_tree_to_execute", trees[0].attrib["ID"])
    return SafeET.tostring(root, encoding="unicode")


def _node_map(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in registry.get("nodes", []) if isinstance(n, dict) and n.get("id")}


def _port_specs(spec: dict[str, Any], direction: str) -> dict[str, dict[str, Any]]:
    ports = spec.get("ports", {}) or {}
    data = ports.get(direction, {}) or {}
    return data if isinstance(data, dict) else {}


def _is_primitive(type_name: str) -> bool:
    return str(type_name or "string").lower() in PRIMITIVE_TYPES


def _literal_matches(value: str, type_name: str) -> bool:
    t = (type_name or "string").lower()
    if BLACKBOARD_RE.match(value):
        return True
    if t in {"string", "std::string", "str", "any"}:
        return True
    if t in {"int", "integer", "unsigned int", "uint", "uint32_t"}:
        try:
            parsed = int(value)
            return parsed >= 0 if t in {"unsigned int", "uint", "uint32_t"} else True
        except ValueError:
            return False
    if t in {"float", "double", "number"}:
        try:
            float(value)
            return True
        except ValueError:
            return False
    if t in {"bool", "boolean"}:
        return value.lower() in {"true", "false", "0", "1"}
    # Opaque robotics types must flow through the BT blackboard.
    return False


def _check_numeric_bounds(tag: str, name: str, value: str, spec: dict[str, Any], errors: list[str]) -> None:
    if BLACKBOARD_RE.match(value):
        return
    if "min" not in spec and "max" not in spec:
        return
    try:
        number = float(value)
    except ValueError:
        return
    if spec.get("min") is not None and number < float(spec["min"]):
        errors.append(f"<{tag}> port '{name}' value {value} is below minimum {spec['min']}.")
    if spec.get("max") is not None and number > float(spec["max"]):
        errors.append(f"<{tag}> port '{name}' value {value} exceeds maximum {spec['max']}.")




def _check_allowed_values(tag: str, name: str, value: str, spec: dict[str, Any], errors: list[str]) -> None:
    if BLACKBOARD_RE.match(value):
        return
    allowed = spec.get("allowed_values") or spec.get("enum") or []
    if allowed and value not in {str(x) for x in allowed}:
        errors.append(
            f"<{tag}> port '{name}' value '{value}' is not one of the allowed values: {', '.join(map(str, allowed))}."
        )

def validate_bt_xml(xml: str, leaf_registry: dict[str, Any], builtin_registry: dict[str, Any]) -> dict[str, Any]:
    """Validate BehaviorTree.CPP XML with SUCCESS/FAILURE-aware blackboard dataflow.

    v5.3 separates two concerns:
    1) node/port/schema validation, performed for every node regardless of reachability;
    2) control-flow dataflow, where blackboard guarantees are tracked separately for
       SUCCESS and FAILURE exits.

    The distinction matters for recovery trees such as::

        Fallback
        |- Sequence(FindObject -> IsObjectLocated)   # may succeed and produces object
        `- Sequence(RotateInPlace -> AlwaysFailure)  # can never make Fallback succeed

    Downstream nodes may safely consume the object after the Fallback succeeds because
    the recovery-only branch cannot be the source of that SUCCESS result.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        root = SafeET.fromstring(xml)
    except Exception as exc:
        return {"valid": False, "errors": [f"XML parse error: {exc}"], "warnings": []}
    if root.tag != "root":
        return {"valid": False, "errors": ["Top-level XML element must be <root>."], "warnings": []}

    main_id = root.attrib.get("main_tree_to_execute")
    if not main_id:
        errors.append("<root> is missing main_tree_to_execute.")
    trees = [x for x in list(root) if x.tag == "BehaviorTree"]
    if not trees:
        return {"valid": False, "errors": errors + ["At least one <BehaviorTree ID=...> is required."], "warnings": warnings}

    tree_map: dict[str, Any] = {}
    for tree in trees:
        tid = tree.attrib.get("ID")
        if not tid:
            errors.append("BehaviorTree is missing ID.")
            continue
        if tid in tree_map:
            errors.append(f"Duplicate BehaviorTree ID '{tid}'.")
        tree_map[tid] = tree
        if len(list(tree)) != 1:
            errors.append(f"BehaviorTree '{tid}' must contain exactly one root BT node.")
    if main_id and main_id not in tree_map:
        errors.append(f"main_tree_to_execute='{main_id}' does not reference a declared tree.")

    leaf_map, builtin_map = _node_map(leaf_registry), _node_map(builtin_registry)
    all_nodes = {**builtin_map, **leaf_map}
    ext_raw = (leaf_registry.get("blackboard", {}) or {}).get("externally_provided", {}) or {}
    if isinstance(ext_raw, dict):
        known0 = {str(k): str(v) for k, v in ext_raw.items()}
    else:
        known0 = {str(k): "any" for k in ext_raw}

    def check_input(tag: str, name: str, value: str, expected: str, known: dict[str, str]) -> None:
        m = BLACKBOARD_RE.match(value)
        if m:
            key = m.group(1)
            if key not in known:
                errors.append(f"<{tag}> input '{name}' reads blackboard '{{{key}}}' before a guaranteed producer exists.")
            else:
                actual = known[key]
                if expected not in {"", "any"} and actual not in {"any", expected}:
                    errors.append(f"<{tag}> input '{name}' expects '{expected}' but '{{{key}}}' is '{actual}'.")
            return

        if not _literal_matches(value, expected):
            if not _is_primitive(expected):
                errors.append(
                    f"<{tag}> input '{name}' has opaque type '{expected}' and must use a blackboard reference like '{{key}}', got '{value}'."
                )
            else:
                errors.append(f"<{tag}> input '{name}' value '{value}' is incompatible with '{expected}'.")

    def validate_shape(el: Any) -> None:
        """Validate node registration, cardinality and declared ports independent of flow."""
        tag = el.tag
        spec = all_nodes.get(tag)
        if spec is None:
            errors.append(f"Unregistered BT node <{tag}>.")
            return

        children = list(el)
        child_rule = spec.get("children", {}) or {}
        if child_rule.get("min") is not None and len(children) < int(child_rule["min"]):
            errors.append(f"<{tag}> requires at least {child_rule['min']} child node(s); got {len(children)}.")
        if child_rule.get("max") is not None and len(children) > int(child_rule["max"]):
            errors.append(f"<{tag}> allows at most {child_rule['max']} child node(s); got {len(children)}.")
        if tag in leaf_map and children:
            errors.append(f"Leaf node <{tag}> must not have children.")

        inputs = _port_specs(spec, "input")
        outputs = _port_specs(spec, "output")
        attrs = spec.get("attributes", {}) or {}
        for name, ps in inputs.items():
            if ps.get("required") and name not in el.attrib:
                errors.append(f"<{tag}> is missing required input '{name}'.")
        for name, ps in outputs.items():
            if ps.get("required") and name not in el.attrib:
                errors.append(f"<{tag}> is missing required output '{name}'.")
        for name, ps in attrs.items():
            if ps.get("required") and name not in el.attrib:
                errors.append(f"<{tag}> is missing required attribute '{name}'.")

        allowed = {"name"} | set(inputs) | set(outputs) | set(attrs)
        if not spec.get("allow_extra_attributes", False):
            for name in el.attrib:
                if name not in allowed:
                    warnings.append(f"<{tag}> uses undeclared attribute '{name}'.")
        if tag != "SubTree" and "name" not in el.attrib:
            warnings.append(f"<{tag}> has no unique name attribute.")

        for name, ps in inputs.items():
            if name in el.attrib:
                _check_numeric_bounds(tag, name, el.attrib[name], ps, errors)
                _check_allowed_values(tag, name, el.attrib[name], ps, errors)
        for name, ps in outputs.items():
            if name in el.attrib and not BLACKBOARD_RE.match(el.attrib[name]):
                errors.append(
                    f"<{tag}> output '{name}' must bind to a blackboard key using '{{key}}' syntax, got '{el.attrib[name]}'."
                )

        if tag == "SubTree":
            target = el.attrib.get("ID")
            if target and target not in tree_map:
                errors.append(f"<SubTree ID='{target}'> references unknown BehaviorTree ID.")

        for child in children:
            validate_shape(child)

    def merge_guarantees(states: list[dict[str, str]]) -> dict[str, str]:
        """Intersection of blackboard keys guaranteed across all listed terminal paths."""
        if not states:
            return {}
        common = set(states[0])
        for state in states[1:]:
            common &= set(state)
        merged: dict[str, str] = {}
        for key in common:
            types = {state[key] for state in states}
            merged[key] = types.pop() if len(types) == 1 else "any"
        return merged

    def add_outputs(el: Any, known: dict[str, str]) -> dict[str, str]:
        spec = all_nodes.get(el.tag) or {}
        result = dict(known)
        for name, ps in _port_specs(spec, "output").items():
            if name in el.attrib:
                m = BLACKBOARD_RE.match(el.attrib[name])
                if m:
                    result[m.group(1)] = str(ps.get("type", "any"))
        return result

    def flow(el: Any, known_before: dict[str, str]) -> dict[str, Any]:
        """Return terminal SUCCESS/FAILURE possibilities and per-status guarantees."""
        tag = el.tag
        spec = all_nodes.get(tag)
        if spec is None:
            return {
                "success_possible": True,
                "success_known": dict(known_before),
                "failure_possible": True,
                "failure_known": dict(known_before),
            }

        inputs = _port_specs(spec, "input")
        for name, ps in inputs.items():
            if name in el.attrib:
                check_input(tag, name, el.attrib[name], str(ps.get("type", "any")), known_before)

        children = list(el)
        if tag == "SubTree":
            # SubTree blackboard remapping is intentionally conservative in this starter.
            return {
                "success_possible": True,
                "success_known": dict(known_before),
                "failure_possible": True,
                "failure_known": dict(known_before),
            }

        if not children:
            success_known = add_outputs(el, known_before)
            if tag in {"AlwaysSuccess", "Success"}:
                return {
                    "success_possible": True,
                    "success_known": success_known,
                    "failure_possible": False,
                    "failure_known": dict(known_before),
                }
            if tag in {"AlwaysFailure", "Failure"}:
                return {
                    "success_possible": False,
                    "success_known": dict(known_before),
                    "failure_possible": True,
                    "failure_known": dict(known_before),
                }
            # Generic actions/conditions may terminally succeed or fail. Output ports
            # are guaranteed only on SUCCESS unless a future SkillManifest says more.
            return {
                "success_possible": True,
                "success_known": success_known,
                "failure_possible": True,
                "failure_known": dict(known_before),
            }

        if tag in {"Sequence", "ReactiveSequence"}:
            current = dict(known_before)
            prefix_can_succeed = True
            failure_states: list[dict[str, str]] = []
            for child in children:
                if not prefix_can_succeed:
                    break
                child_flow = flow(child, current)
                if child_flow["failure_possible"]:
                    failure_states.append(dict(child_flow["failure_known"]))
                if child_flow["success_possible"]:
                    current = dict(child_flow["success_known"])
                else:
                    prefix_can_succeed = False
            return {
                "success_possible": prefix_can_succeed,
                "success_known": current if prefix_can_succeed else dict(known_before),
                "failure_possible": bool(failure_states),
                "failure_known": merge_guarantees(failure_states) if failure_states else dict(known_before),
            }

        if tag in {"Fallback", "ReactiveFallback"}:
            next_known = dict(known_before)
            prefix_can_fail = True
            success_states: list[dict[str, str]] = []
            for child in children:
                if not prefix_can_fail:
                    break
                child_flow = flow(child, next_known)
                if child_flow["success_possible"]:
                    success_states.append(dict(child_flow["success_known"]))
                if child_flow["failure_possible"]:
                    next_known = dict(child_flow["failure_known"])
                else:
                    prefix_can_fail = False
            return {
                "success_possible": bool(success_states),
                "success_known": merge_guarantees(success_states) if success_states else dict(known_before),
                "failure_possible": prefix_can_fail,
                "failure_known": next_known if prefix_can_fail else dict(known_before),
            }

        # Team RecoveryNode: child 1 is the primary behavior; on FAILURE child 2
        # runs as recovery and the primary is retried up to number_of_retries.
        # SUCCESS can only come from the primary child, so downstream SUCCESS
        # guarantees are exactly the primary's success guarantees.
        if tag == "RecoveryNode" and len(children) == 2:
            primary_flow = flow(children[0], dict(known_before))
            recovery_known = (
                dict(primary_flow["failure_known"])
                if primary_flow["failure_possible"]
                else dict(known_before)
            )
            recovery_flow = flow(children[1], recovery_known)
            failure_states = [dict(known_before)]
            if primary_flow["failure_possible"]:
                failure_states.append(dict(primary_flow["failure_known"]))
            if recovery_flow["failure_possible"]:
                failure_states.append(dict(recovery_flow["failure_known"]))
            return {
                "success_possible": primary_flow["success_possible"],
                "success_known": dict(primary_flow["success_known"]),
                "failure_possible": primary_flow["failure_possible"],
                "failure_known": merge_guarantees(failure_states),
            }

        # Decorators: model terminal status transformations explicitly enough for
        # safe downstream blackboard reasoning.
        child_flow = flow(children[0], dict(known_before))
        if tag == "Inverter":
            return {
                "success_possible": child_flow["failure_possible"],
                "success_known": dict(child_flow["failure_known"]),
                "failure_possible": child_flow["success_possible"],
                "failure_known": dict(child_flow["success_known"]),
            }
        if tag == "ForceSuccess":
            terminal_states = []
            if child_flow["success_possible"]:
                terminal_states.append(dict(child_flow["success_known"]))
            if child_flow["failure_possible"]:
                terminal_states.append(dict(child_flow["failure_known"]))
            return {
                "success_possible": bool(terminal_states),
                "success_known": merge_guarantees(terminal_states) if terminal_states else dict(known_before),
                "failure_possible": False,
                "failure_known": dict(known_before),
            }
        if tag == "ForceFailure":
            terminal_states = []
            if child_flow["success_possible"]:
                terminal_states.append(dict(child_flow["success_known"]))
            if child_flow["failure_possible"]:
                terminal_states.append(dict(child_flow["failure_known"]))
            return {
                "success_possible": False,
                "success_known": dict(known_before),
                "failure_possible": bool(terminal_states),
                "failure_known": merge_guarantees(terminal_states) if terminal_states else dict(known_before),
            }
        if tag == "Timeout":
            # A timeout can happen before the child reaches a terminal state, so no
            # child-produced output is guaranteed on the timeout/failure path.
            failure_states = [dict(known_before)]
            if child_flow["failure_possible"]:
                failure_states.append(dict(child_flow["failure_known"]))
            return {
                "success_possible": child_flow["success_possible"],
                "success_known": dict(child_flow["success_known"]),
                "failure_possible": True,
                "failure_known": merge_guarantees(failure_states),
            }
        if tag in {"RetryUntilSuccessful", "Repeat", "Delay"}:
            # Bounded retry/repeat/delay do not change the child's SUCCESS guarantee.
            # Failure remains possible whenever the child can fail (e.g. retry bound
            # exhausted); do not assume outputs from failed attempts exist.
            return {
                "success_possible": child_flow["success_possible"],
                "success_known": dict(child_flow["success_known"]),
                "failure_possible": child_flow["failure_possible"],
                "failure_known": dict(child_flow["failure_known"]),
            }

        if tag == "KeepRunningUntilFailure":
            # It never succeeds: child SUCCESS/RUNNING becomes RUNNING, and child
            # FAILURE is propagated as FAILURE.
            return {
                "success_possible": False,
                "success_known": dict(known_before),
                "failure_possible": child_flow["failure_possible"],
                "failure_known": dict(child_flow["failure_known"]),
            }

        # Unknown one-child control/decorator semantics are conservative.
        return child_flow

    for tree in tree_map.values():
        if len(list(tree)) != 1:
            continue
        bt_root = list(tree)[0]
        validate_shape(bt_root)
        flow(bt_root, dict(known0))

        # Registry-driven continuous search invariant. This also protects the
        # direct-to-BT comparison path, which has no TaskPlanIR semantic validator.
        parents = {child: parent for parent in bt_root.iter() for child in list(parent)}
        for trigger in [el for el in bt_root.iter() if (leaf_map.get(el.tag) or {}).get("continuous_search_trigger")]:
            spec = leaf_map.get(trigger.tag) or {}
            policy = spec.get("search_policy", {}) or {}
            condition_id = str(policy.get("condition") or "IsObjectFound")
            activity_id = str(policy.get("activity") or "Patrol")
            sequence = parents.get(trigger)
            if sequence is None or sequence.tag != "Sequence":
                errors.append(
                    f"<{trigger.tag}> continuous-search trigger must be a direct child of Sequence."
                )
                continue
            siblings = list(sequence)
            trigger_index = siblings.index(trigger)
            if trigger_index + 1 >= len(siblings) or siblings[trigger_index + 1].tag != "Timeout":
                errors.append(
                    f"<{trigger.tag}> must be followed immediately by Timeout(ReactiveFallback({condition_id}, {activity_id}))."
                )
                continue
            timeout = siblings[trigger_index + 1]
            timeout_children = list(timeout)
            if len(timeout_children) != 1 or timeout_children[0].tag != "ReactiveFallback":
                errors.append(
                    f"Continuous search <Timeout> must contain exactly one <ReactiveFallback>."
                )
                continue
            monitor_children = list(timeout_children[0])
            if len(monitor_children) != 2 or [x.tag for x in monitor_children] != [condition_id, activity_id]:
                errors.append(
                    f"Continuous search <ReactiveFallback> children must be exactly <{condition_id}> then <{activity_id}>."
                )
                continue
            trigger_target = trigger.attrib.get("object_name")
            condition_target = monitor_children[0].attrib.get("object_name")
            if trigger_target != condition_target:
                errors.append(
                    f"<{trigger.tag}> and <{condition_id}> must use the same literal object_name."
                )
            current = sequence
            while current in parents and current is not bt_root:
                current = parents[current]
                if current.tag in {"ForceSuccess", "Inverter", "Fallback", "ReactiveFallback"}:
                    errors.append(
                        "Continuous search must remain on a fail-stop Sequence path; do not mask Timeout FAILURE."
                    )
                    break

    return {"valid": not errors, "errors": errors, "warnings": warnings}
