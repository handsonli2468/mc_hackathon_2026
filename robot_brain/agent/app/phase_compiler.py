from __future__ import annotations

from copy import deepcopy
from typing import Any

from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET

from .compiler_adapter import (
    continuous_search_recovery_steps,
    ensure_state_verifications,
    phase_retry_attempts,
    phase_timeout_msec,
    search_activity_recovery_steps,
)
from .registry import load_builtin_registry, load_skill_registry, skill_map
from .schemas import Phase, TaskPlanIR

_WRAPPERS = {"Action", "Condition", "Control", "Decorator"}
_FORBIDDEN_MODEL_POLICY = {
    "RecoveryNode", "RetryUntilSuccessful", "Repeat", "Timeout", "Delay",
    "ForceFailure", "ForceSuccess", "Inverter", "KeepRunningUntilFailure",
}
_SEQUENCE_TAGS = {"Sequence", "ReactiveSequence"}


def _canonical_expected(name: str, expected: list[str]) -> str | None:
    if name in expected:
        return name
    matches = [x for x in expected if x.casefold() == name.casefold()]
    return matches[0] if len(matches) == 1 else None


def _tree_and_child(root: Any) -> tuple[Any | None, Any | None, list[str]]:
    errors: list[str] = []
    root_children = list(root)
    trees = [x for x in root_children if x.tag == "BehaviorTree"]
    if trees:
        main = root.attrib.get("main_tree_to_execute")
        tree = next((t for t in trees if t.attrib.get("ID") == main), None)
        if tree is None and len(trees) == 1:
            tree = trees[0]
        if tree is None:
            return None, None, ["BTGenBot output does not identify one unambiguous main BehaviorTree."]
        # Extra executable siblings outside BehaviorTree are not silently ignored.
        extras = [x for x in root_children if x is not tree]
        if extras:
            return tree, None, [f"BTGenBot output contains {len(extras)} extra top-level node(s) outside <BehaviorTree>."]
        children = list(tree)
        if len(children) != 1:
            return tree, None, [f"BehaviorTree must contain exactly one root BT node; got {len(children)}."]
        return tree, children[0], errors

    # BTGenBot-2 sometimes omits the BehaviorTree wrapper while still returning a
    # usable phase-local BT node directly under <root>. For phase compilation only,
    # accept exactly one such child. The final assembler always restores the full
    # BehaviorTree.CPP document wrapper.
    if len(root_children) == 1:
        child = root_children[0]
        if child.tag not in {"TreeNodesModel", "include"}:
            return None, child, ["BTGenBot omitted <BehaviorTree>; accepted the single phase subtree directly."]
    return None, None, ["BTGenBot output has no <BehaviorTree> and does not contain exactly one phase subtree under <root>."]


def _ancestors(el: Any, parent: dict[Any, Any]) -> list[Any]:
    out = []
    cur = el
    while cur in parent:
        cur = parent[cur]
        out.append(cur)
    return out


def _child_under(ancestor: Any, node: Any, parent: dict[Any, Any]) -> Any | None:
    cur = node
    prev = node
    while cur in parent:
        prev = cur
        cur = parent[cur]
        if cur is ancestor:
            return prev
    return None


def _ordered_by_sequence(first: Any, second: Any, parent: dict[Any, Any]) -> bool:
    second_anc = set(_ancestors(second, parent))
    for anc in _ancestors(first, parent):
        if anc.tag not in _SEQUENCE_TAGS or anc not in second_anc:
            continue
        a = _child_under(anc, first, parent)
        b = _child_under(anc, second, parent)
        if a is None or b is None or a is b:
            continue
        children = list(anc)
        if children.index(a) < children.index(b):
            return True
    return False



def _unwrap_policy_nodes(parent: Any, phase: Phase, changes: list[str]) -> None:
    expected = {phase.nominal_action.skill, *(v.condition for v in phase.verification)}
    for child in list(parent):
        _unwrap_policy_nodes(child, phase, changes)

    strippable = {
        "RetryUntilSuccessful", "Repeat", "Timeout", "Delay",
        "ForceFailure", "ForceSuccess", "Inverter", "KeepRunningUntilFailure",
    }
    children = list(parent)
    for idx, child in enumerate(children):
        tag = str(child.tag)
        replacement = None
        if tag in strippable and len(list(child)) == 1:
            replacement = list(child)[0]
        elif tag == "RecoveryNode" and len(list(child)) >= 1:
            primary = list(child)[0]
            primary_ids = set()
            recovery_ids = set()
            for el in primary.iter():
                candidate = str(el.attrib.get("ID", "")) if str(el.tag) in _WRAPPERS and el.attrib.get("ID") else str(el.tag)
                if candidate in expected:
                    primary_ids.add(candidate)
            for extra in list(child)[1:]:
                for el in extra.iter():
                    candidate = str(el.attrib.get("ID", "")) if str(el.tag) in _WRAPPERS and el.attrib.get("ID") else str(el.tag)
                    if candidate in expected:
                        recovery_ids.add(candidate)
            if expected.issubset(primary_ids) and not recovery_ids:
                replacement = primary
        if replacement is not None:
            parent.remove(child)
            parent.insert(idx, deepcopy(replacement))
            changes.append(f"stripped model-owned policy wrapper <{tag}>")


def prepare_phase_candidate_xml(text: str, phase: Phase) -> dict[str, Any]:
    """Recover/sanitize BTGenBot phase XML before strict topology validation.

    The model is only responsible for nominal action/verification topology.  This
    helper removes non-executable TreeNodesModel/include metadata and strips policy
    wrappers that the deterministic server owns anyway. It may also wrap a bare
    BehaviorTree document in the required <root>. No required leaf is invented.
    """
    raw = str(text or "").strip()
    changes: list[str] = []
    warnings: list[str] = []

    # Strip common Markdown fences without trying to repair arbitrary malformed XML.
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
        changes.append("removed Markdown code fence")

    # Prefer the last complete <root> block if prose surrounded it.
    import re
    roots = re.findall(r"<root\b[\s\S]*?</root>", raw, flags=re.IGNORECASE)
    candidate = roots[-1].strip() if roots else raw

    # If the model returned a complete BehaviorTree without <root>, add only the
    # document wrapper. This is a syntax recovery, not a control-flow rewrite.
    if not candidate.lstrip().startswith("<root"):
        trees = re.findall(r"<BehaviorTree\b[\s\S]*?</BehaviorTree>", candidate, flags=re.IGNORECASE)
        if len(trees) == 1:
            try:
                tree = SafeET.fromstring(trees[0])
                tree_id = str(tree.attrib.get("ID") or "MainTree")
                candidate = (
                    f'<root BTCPP_format="4" main_tree_to_execute="{tree_id}">'
                    + trees[0]
                    + "</root>"
                )
                changes.append("wrapped bare <BehaviorTree> in <root>")
            except Exception:
                pass

    try:
        root = SafeET.fromstring(candidate)
    except Exception as exc:
        return {"xml": None, "changes": changes, "warnings": warnings, "error": f"XML parse error: {exc}"}
    if root.tag != "root":
        return {"xml": None, "changes": changes, "warnings": warnings, "error": "Top-level element must be <root>."}

    # TreeNodesModel/include are metadata, not phase executable siblings. Remove them
    # before _tree_and_child enforces one unambiguous executable subtree.
    for child in list(root):
        if str(child.tag) in {"TreeNodesModel", "include"}:
            root.remove(child)
            changes.append(f"removed non-executable <{child.tag}> metadata")

    tree, subtree, notes = _tree_and_child(root)
    warnings.extend(notes)
    if subtree is not None:
        owner = tree if tree is not None else root
        _unwrap_policy_nodes(owner, phase, changes)

    return {
        "xml": ET.tostring(root, encoding="unicode"),
        "changes": changes,
        "warnings": warnings,
        "error": None,
    }

def validate_phase_candidate(xml: str, phase: Phase, builtin_registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate only the topology BTGenBot is responsible for in one phase.

    The compiler model is allowed to choose nominal control flow, but it is not
    allowed to invent leaves or policy wrappers. Retry/timeout policy is applied
    deterministically after this check.
    """
    builtin_registry = builtin_registry or load_builtin_registry()
    builtin_ids = {str(x.get("id")) for x in builtin_registry.get("nodes", []) if x.get("id")}
    expected = [phase.nominal_action.skill, *(v.condition for v in phase.verification)]
    errors: list[str] = []

    try:
        root = SafeET.fromstring(xml)
    except Exception as exc:
        return {"valid": False, "errors": [f"XML parse error: {exc}"], "warnings": [], "subtree_xml": None}
    if root.tag != "root":
        return {"valid": False, "errors": ["Top-level element must be <root>."], "warnings": [], "subtree_xml": None}

    _, subtree, tree_notes = _tree_and_child(root)
    if subtree is None:
        errors.extend(tree_notes)
        return {"valid": False, "errors": errors, "warnings": [], "subtree_xml": None}
    warnings = list(tree_notes)

    parent = {child: p for p in subtree.iter() for child in list(p)}
    found: list[tuple[str, Any]] = []
    for el in subtree.iter():
        tag = str(el.tag)
        candidate = str(el.attrib.get("ID", "")) if tag in _WRAPPERS and el.attrib.get("ID") else tag
        canonical = _canonical_expected(candidate, expected)
        if canonical:
            found.append((canonical, el))
            continue
        if candidate in builtin_ids or tag in builtin_ids:
            if candidate in _FORBIDDEN_MODEL_POLICY or tag in _FORBIDDEN_MODEL_POLICY:
                errors.append(
                    f"BTGenBot must not generate policy wrapper <{candidate}> for a phase; retry/timeout policy is added deterministically."
                )
            continue
        errors.append(f"BTGenBot invented leaf node '{candidate}' outside this phase's closed Actions list: {expected}.")

    counts = {sid: sum(1 for name, _ in found if name == sid) for sid in expected}
    for sid, count in counts.items():
        if count == 0:
            errors.append(f"Required phase leaf '{sid}' is missing.")
        elif count > 1:
            errors.append(f"Required phase leaf '{sid}' appears {count} times; expected exactly once.")

    action_el = next((el for name, el in found if name == phase.nominal_action.skill), None)
    for verification in phase.verification:
        verify_el = next((el for name, el in found if name == verification.condition), None)
        if action_el is not None and verify_el is not None and not _ordered_by_sequence(action_el, verify_el, parent):
            errors.append(
                f"{phase.nominal_action.skill} must execute before {verification.condition} under sequential control flow."
            )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "subtree_xml": ET.tostring(subtree, encoding="unicode") if not errors else None,
    }


def deterministic_phase_subtree(phase: Phase) -> str:
    """Minimal safe fallback topology for research/debug deployments.

    Ports/names are deliberately omitted here; the normalizer fills them from the
    TaskPlanIR + SkillManifest contract after all phases are assembled.
    """
    if phase.verification:
        seq = ET.Element("Sequence", {"name": f"{phase.id}_nominal_sequence"})
        ET.SubElement(seq, phase.nominal_action.skill)
        for verification in phase.verification:
            ET.SubElement(seq, verification.condition)
        return ET.tostring(seq, encoding="unicode")
    return f"<{phase.nominal_action.skill}/>"


def _wrap_policy(
    ir: TaskPlanIR,
    phase_index: int,
    phase: Phase,
    subtree: Any,
    registry: dict[str, Any] | None = None,
    builtin_registry: dict[str, Any] | None = None,
) -> Any:
    current = subtree

    # Current live-engine search contract. VisualizeObject selects and starts the
    # continuous camera query once. The condition-first ReactiveFallback polls that
    # query on every tick while Patrol remains RUNNING. When the condition succeeds,
    # ReactiveFallback halts Patrol. Timeout FAILURE propagates through the mission
    # Sequence, so no downstream action is executed after search exhaustion.
    active_registry = registry or load_skill_registry()
    continuous_search = continuous_search_recovery_steps(phase, active_registry)
    if continuous_search:
        skills = skill_map(active_registry)
        action_spec = skills.get(phase.nominal_action.skill, {})
        policy = action_spec.get("search_policy", {}) or {}
        timeout_msec = phase_timeout_msec(phase)
        if not timeout_msec:
            timeout_msec = int(float(policy.get("timeout_sec", 60)) * 1000)

        search_sequence = ET.Element("Sequence", {"name": f"{phase.id}_continuous_search"})
        ET.SubElement(search_sequence, phase.nominal_action.skill)
        timeout = ET.SubElement(
            search_sequence,
            "Timeout",
            {"name": f"{phase.id}_search_timeout", "msec": str(timeout_msec)},
        )
        monitor = ET.SubElement(timeout, "ReactiveFallback", {"name": f"{phase.id}_search_monitor"})
        if len(phase.verification) == 1:
            ET.SubElement(monitor, phase.verification[0].condition)
        else:
            conditions = ET.SubElement(monitor, "Sequence", {"name": f"{phase.id}_search_conditions"})
            for verification in phase.verification:
                ET.SubElement(conditions, verification.condition)
        if len(continuous_search) == 1:
            ET.SubElement(monitor, continuous_search[0].skill)
        else:
            activities = ET.SubElement(monitor, "Sequence", {"name": f"{phase.id}_search_activity"})
            for step in continuous_search:
                ET.SubElement(activities, step.skill)
        return search_sequence

    # Observable ensure-state guard:
    # IF the desired condition is already true, skip the mutating action.
    # ELSE execute action -> verification. With the team's formal string-based
    # object_name contract, even the first perception phase can safely pre-check
    # IsObjectFound before invoking VisualizeObject when inputs are already safe.
    prechecks = ensure_state_verifications(ir, phase_index, registry)
    if prechecks:
        ensure = ET.Element("Fallback", {"name": f"{phase.id}_ensure_state"})
        if len(prechecks) == 1:
            ET.SubElement(ensure, prechecks[0].condition)
        else:
            already = ET.Element("Sequence", {"name": f"{phase.id}_already_satisfied"})
            for verification in prechecks:
                ET.SubElement(already, verification.condition)
            ensure.append(already)
        ensure.append(current)
        current = ensure

    # Prefer a dedicated RecoveryNode only when the active engine exports it. The
    # current ABI uses standard BT.CPP Fallback + ForceFailure under a bounded retry.
    search_recovery = search_activity_recovery_steps(phase, registry)
    active_builtins = builtin_registry or load_builtin_registry()
    builtin_ids = {
        str(x.get("id")) for x in active_builtins.get("nodes", [])
        if isinstance(x, dict) and x.get("id")
    }
    use_team_recovery = "RecoveryNode" in builtin_ids
    recovery_handles_retry = bool(search_recovery and use_team_recovery)
    attempts = phase_retry_attempts(phase)
    if search_recovery and use_team_recovery:
        recovery_node = ET.Element(
            "RecoveryNode",
            {
                "name": f"{phase.id}_recovery",
                # TaskPlanIR max_attempts counts the initial primary attempt; the
                # runtime RecoveryNode port counts recovery+retry cycles.
                "number_of_retries": str(max(0, attempts - 1)),
            },
        )
        recovery_node.append(current)
        if len(search_recovery) == 1:
            ET.SubElement(recovery_node, search_recovery[0].skill)
        else:
            recovery_seq = ET.Element("Sequence", {"name": f"{phase.id}_recovery_sequence"})
            for step in search_recovery:
                ET.SubElement(recovery_seq, step.skill)
            recovery_node.append(recovery_seq)
        current = recovery_node
    elif search_recovery:
        fallback = ET.Element("Fallback", {"name": f"{phase.id}_observation_fallback"})
        fallback.append(current)
        if len(search_recovery) == 1:
            recovery_behavior = ET.Element(search_recovery[0].skill)
        else:
            recovery_behavior = ET.Element("Sequence", {"name": f"{phase.id}_recovery_sequence"})
            for step in search_recovery:
                ET.SubElement(recovery_behavior, step.skill)
        if "ForceFailure" in builtin_ids:
            force_failure = ET.Element("ForceFailure", {"name": f"{phase.id}_retry_after_recovery"})
            force_failure.append(recovery_behavior)
            fallback.append(force_failure)
        else:
            recovery_seq = ET.Element("Sequence", {"name": f"{phase.id}_observation_recovery"})
            recovery_seq.append(recovery_behavior)
            ET.SubElement(recovery_seq, "AlwaysFailure", {"name": f"{phase.id}_retry_after_recovery"})
            fallback.append(recovery_seq)
        current = fallback

    timeout = phase_timeout_msec(phase)
    if timeout:
        wrapper = ET.Element("Timeout", {"name": f"{phase.id}_timeout", "msec": str(timeout)})
        wrapper.append(current)
        current = wrapper

    # When RecoveryNode owns the retry loop, do not add a second retry decorator.
    if attempts > 1 and not recovery_handles_retry:
        wrapper = ET.Element(
            "RetryUntilSuccessful",
            {"name": f"{phase.id}_retry", "num_attempts": str(attempts)},
        )
        wrapper.append(current)
        current = wrapper
    return current

def assemble_phase_subtrees(
    ir: TaskPlanIR,
    phase_subtrees: dict[str, str],
    registry: dict[str, Any] | None = None,
    builtin_registry: dict[str, Any] | None = None,
) -> str:
    root = ET.Element("root", {"BTCPP_format": "4", "main_tree_to_execute": "MainTree"})
    tree = ET.SubElement(root, "BehaviorTree", {"ID": "MainTree"})
    mission = ET.SubElement(tree, "Sequence", {"name": "mission_sequence"})

    for phase_index, phase in enumerate(ir.phases):
        xml = phase_subtrees[phase.id]
        subtree = SafeET.fromstring(xml)
        mission.append(
            _wrap_policy(
                ir,
                phase_index,
                phase,
                deepcopy(subtree),
                registry,
                builtin_registry,
            )
        )

    return ET.tostring(root, encoding="unicode")


def recovery_compilation_warnings(ir: TaskPlanIR) -> list[str]:
    warnings: list[str] = []
    builtin_ids = {
        str(x.get("id")) for x in load_builtin_registry().get("nodes", [])
        if isinstance(x, dict) and x.get("id")
    }
    recovery_structure = (
        "the engine-provided RecoveryNode"
        if "RecoveryNode" in builtin_ids
        else "a bounded RetryUntilSuccessful + Fallback + ForceFailure structure"
    )
    for phase in ir.phases:
        all_recoveries = [r for f in phase.failure_modes for r in f.recovery if r.skill]
        if not all_recoveries:
            continue
        continuous = continuous_search_recovery_steps(phase)
        compiled = search_activity_recovery_steps(phase)
        if continuous:
            warnings.append(
                f"Phase '{phase.id}' uses continuous camera search: VisualizeObject runs once, then "
                "Timeout(ReactiveFallback(IsObjectFound, Patrol)) monitors until found or mission failure: "
                + ", ".join(str(x.skill) for x in continuous)
                + "."
            )
        elif compiled:
            warnings.append(
                f"Phase '{phase.id}' uses an observable search activity through {recovery_structure}: "
                + ", ".join(str(x.skill) for x in compiled)
                + ". Other failure-specific recovery remains metadata-only until runtime failure-discriminator conditions exist."
            )
        else:
            warnings.append(
                f"Phase '{phase.id}' contains failure-specific recovery steps in TaskPlanIR. "
                "The compiler preserves them in IR but does not emit failure-specific branches when the runtime cannot safely discriminate the failure cause. "
                "Bounded retry/timeout policy is still compiled deterministically."
            )
    return warnings
