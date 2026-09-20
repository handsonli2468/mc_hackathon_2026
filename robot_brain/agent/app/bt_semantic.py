from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as SafeET

from .compiler_adapter import (
    build_invocation_plan,
    continuous_search_recovery_steps,
    ensure_state_verifications,
    phase_retry_attempts,
    phase_timeout_msec,
    search_viewpoint_recovery_steps,
)
from .registry import load_builtin_registry, load_skill_registry, skill_map
from .schemas import TaskPlanIR

_SEQUENCE_TAGS = {"Sequence", "ReactiveSequence"}


def _ancestors(el: Any, parent: dict[Any, Any]) -> list[Any]:
    out: list[Any] = []
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
    second_ancestors = set(_ancestors(second, parent))
    for anc in _ancestors(first, parent):
        if anc.tag not in _SEQUENCE_TAGS or anc not in second_ancestors:
            continue
        a = _child_under(anc, first, parent)
        b = _child_under(anc, second, parent)
        if a is None or b is None or a is b:
            continue
        children = list(anc)
        if children.index(a) < children.index(b):
            return True
    return False


def _shared_ancestor(nodes: list[Any], tag: str, parent: dict[Any, Any]) -> Any | None:
    if not nodes:
        return None
    common = set(_ancestors(nodes[0], parent))
    for node in nodes[1:]:
        common &= set(_ancestors(node, parent))
    # Prefer the nearest common ancestor to the first node.
    for anc in _ancestors(nodes[0], parent):
        if anc in common and anc.tag == tag:
            return anc
    return None




def _validate_mission_fail_stop(
    root: Any,
    ir: TaskPlanIR,
    named: dict[str, Any],
    invocations: list[dict[str, Any]],
    parent: dict[Any, Any],
) -> dict[str, Any]:
    """Prove that phase failure prevents later phases from being ticked.

    The generated mission contract is deliberately strict: one selected BehaviorTree,
    whose root is a Sequence, with one direct child per TaskPlanIR phase in the same
    order. BehaviorTree.CPP Sequence only advances after the current child returns
    SUCCESS, so exhaustion/failure of an early RecoveryNode/Retry/Timeout propagates
    to the mission Sequence and later phases are not executed.
    """
    errors: list[str] = []
    warnings: list[str] = []

    trees = [el for el in list(root) if el.tag == "BehaviorTree"]
    main_id = root.attrib.get("main_tree_to_execute")
    tree = next((el for el in trees if el.attrib.get("ID") == main_id), None)
    if tree is None and len(trees) == 1:
        tree = trees[0]
    if tree is None:
        return {"valid": False, "errors": ["Mission fail-stop check could not identify one main BehaviorTree."], "warnings": []}

    roots = list(tree)
    if len(roots) != 1:
        return {"valid": False, "errors": [f"Mission BehaviorTree must have exactly one root control node; got {len(roots)}."], "warnings": []}
    mission_root = roots[0]
    if mission_root.tag != "Sequence":
        errors.append(
            f"Mission root must be <Sequence> for fail-stop semantics; got <{mission_root.tag}>. "
            "A later phase must never run after an earlier phase returns FAILURE."
        )
        return {"valid": False, "errors": errors, "warnings": warnings, "root": mission_root.tag}

    children = list(mission_root)
    phase_roots: list[Any] = []
    for phase in ir.phases:
        action_inv = next((x for x in invocations if x.get("phase_id") == phase.id and x.get("role") == "action"), None)
        action_el = named.get(action_inv["node_name"]) if action_inv else None
        if action_el is None:
            errors.append(f"Mission fail-stop check cannot locate the nominal action for phase '{phase.id}'.")
            continue
        top = _child_under(mission_root, action_el, parent)
        if top is None:
            errors.append(f"Phase '{phase.id}' nominal action is not contained under the mission Sequence.")
            continue
        phase_roots.append(top)

    if len(children) != len(ir.phases):
        errors.append(
            f"Mission Sequence must contain exactly one direct phase subtree per TaskPlanIR phase; "
            f"got {len(children)} children for {len(ir.phases)} phases."
        )

    if len(phase_roots) == len(ir.phases):
        if len(set(phase_roots)) != len(phase_roots):
            errors.append("Two TaskPlanIR phases share the same direct mission Sequence child; phase failure boundaries are ambiguous.")
        for idx, (phase, top) in enumerate(zip(ir.phases, phase_roots)):
            if idx >= len(children) or children[idx] is not top:
                errors.append(
                    f"Phase '{phase.id}' is not direct child {idx + 1} of mission_sequence in TaskPlanIR order."
                )

    if not errors:
        warnings.append(
            "Mission fail-stop semantics verified: the top-level Sequence advances only after each phase subtree succeeds; "
            "if search/recovery, navigation, or grasp exhausts and returns FAILURE, all downstream phases remain unticked."
        )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "root": mission_root.tag,
        "phase_count": len(ir.phases),
        "direct_children": len(children),
        "guarantee": "stop_downstream_on_phase_failure" if not errors else None,
    }

def validate_bt_semantics(
    xml: str,
    ir: TaskPlanIR,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check that the normalized BT faithfully represents the validated TaskPlanIR.

    This validator focuses on properties that XML/port validation cannot prove:
    required leaf presence, action-before-verification ordering, retry coverage and
    retry/timeout coverage and executable compiler-contract preservation. It intentionally
    does not rewrite topology. Cleanup and failure-specific recovery are metadata-only
    until the compiler capability contract says otherwise.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    builtin_ids = {
        str(x.get("id")) for x in load_builtin_registry().get("nodes", [])
        if isinstance(x, dict) and x.get("id")
    }
    use_team_recovery = "RecoveryNode" in builtin_ids
    errors: list[str] = []
    warnings: list[str] = []

    try:
        root = SafeET.fromstring(xml)
    except Exception as exc:
        return {"valid": False, "errors": [f"Semantic validation could not parse XML: {exc}"], "warnings": []}

    parent = {child: p for p in root.iter() for child in list(p)}
    named: dict[str, Any] = {}
    duplicates: set[str] = set()
    for el in root.iter():
        name = el.attrib.get("name")
        if not name:
            continue
        if name in named:
            duplicates.add(name)
        named[name] = el
    if duplicates:
        errors.append("Duplicate BT node names prevent semantic matching: " + ", ".join(sorted(duplicates)))

    invocations = build_invocation_plan(
        ir,
        registry,
        include_recovery=False,
        include_compiled_search_recovery=True,
        include_compiled_ensure_state=True,
        include_cleanup=False,
    )
    expected_names = {x["node_name"] for x in invocations}
    invocation_by_name = {x["node_name"]: x for x in invocations}

    mission_fail_stop = _validate_mission_fail_stop(root, ir, named, invocations, parent)
    errors.extend(mission_fail_stop.get("errors", []))
    warnings.extend(mission_fail_stop.get("warnings", []))

    for inv in invocations:
        el = named.get(inv["node_name"])
        if el is None:
            errors.append(
                f"TaskPlanIR requires {inv['role']} skill '{inv['skill']}' in phase '{inv['phase_id']}', "
                f"but the BT does not contain it."
            )
        elif el.tag != inv["skill"]:
            errors.append(
                f"BT node '{inv['node_name']}' should be <{inv['skill']}> according to TaskPlanIR, got <{el.tag}>."
            )

    # Registered extra leaves are semantic plan drift. A retry must be represented by
    # a decorator around the planned node, not by silently duplicating new leaf calls.
    for el in root.iter():
        if el.tag not in skills:
            continue
        name = el.attrib.get("name")
        if name and name not in expected_names:
            errors.append(f"BT contains extra registered leaf <{el.tag} name='{name}'> not present in TaskPlanIR.")

    for phase_index, phase in enumerate(ir.phases):
        action_inv = next(
            (x for x in invocations if x["phase_id"] == phase.id and x["role"] == "action"),
            None,
        )
        if not action_inv:
            continue
        action_el = named.get(action_inv["node_name"])
        verification_invs = [
            x for x in invocations if x["phase_id"] == phase.id and x["role"].startswith("verify_")
        ]
        verification_els = [named.get(x["node_name"]) for x in verification_invs]

        if action_el is not None:
            for inv, verification_el in zip(verification_invs, verification_els):
                if verification_el is None:
                    continue
                if not _ordered_by_sequence(action_el, verification_el, parent):
                    errors.append(
                        f"Phase '{phase.id}' requires <{phase.nominal_action.skill}> to execute before "
                        f"verification <{inv['skill']}> in sequential control flow. "
                        "Do not place the action and its verification as alternative Fallback children."
                    )

        # v5.3 observable ensure-state guard: when a verification condition can be
        # evaluated from state that existed before the action, the compiler may add
        # an IF/ELSE Fallback that skips the mutating action if the condition is
        # already true. Validate that the precheck and action are alternative branches.
        ensure_specs = ensure_state_verifications(ir, phase_index, registry)
        ensure_invs = [
            x for x in invocations
            if x["phase_id"] == phase.id and x["role"].startswith("ensure_precheck_")
        ]
        ensure_els = [named.get(x["node_name"]) for x in ensure_invs]
        if ensure_specs and action_el is not None:
            for inv, precheck_el in zip(ensure_invs, ensure_els):
                if precheck_el is None:
                    continue
                fallback = _shared_ancestor([precheck_el, action_el], "Fallback", parent)
                if fallback is None:
                    errors.append(
                        f"Phase '{phase.id}' observable precheck <{inv['skill']}> must be in an IF/ELSE Fallback "
                        f"with the nominal <{phase.nominal_action.skill}> branch."
                    )
                    continue
                pre_branch = _child_under(fallback, precheck_el, parent)
                action_branch = _child_under(fallback, action_el, parent)
                if pre_branch is None or action_branch is None or pre_branch is action_branch:
                    errors.append(
                        f"Phase '{phase.id}' ensure-state precheck <{inv['skill']}> must be an alternative branch "
                        "to the nominal action, not inside the same branch."
                    )
                elif list(fallback).index(pre_branch) > list(fallback).index(action_branch):
                    errors.append(
                        f"Phase '{phase.id}' ensure-state precheck <{inv['skill']}> must be evaluated before "
                        "the nominal action branch."
                    )
            warnings.append(
                f"Phase '{phase.id}' uses an observable ensure-state IF/ELSE guard so an already-satisfied "
                "condition can skip the mutating action safely."
            )

        attempts = phase_retry_attempts(phase)
        phase_nodes = [x for x in [action_el, *verification_els] if x is not None]
        continuous_search = continuous_search_recovery_steps(phase, registry)
        compiled_search_recovery = search_viewpoint_recovery_steps(phase, registry)

        # Formal runtime search recovery uses the team RecoveryNode rather than a
        # generic RetryUntilSuccessful+Fallback workaround. For all other phases,
        # RetryUntilSuccessful remains the bounded retry representation.
        if attempts > 1 and phase_nodes and not continuous_search:
            if compiled_search_recovery and use_team_recovery:
                recovery_control = _shared_ancestor(phase_nodes, "RecoveryNode", parent)
                if recovery_control is None:
                    errors.append(
                        f"Phase '{phase.id}' requires team <RecoveryNode> search recovery with "
                        f"number_of_retries={attempts - 1}, but no shared RecoveryNode was found."
                    )
                else:
                    try:
                        actual_retries = int(recovery_control.attrib.get("number_of_retries", "1"))
                    except ValueError:
                        actual_retries = -1
                    expected_retries = max(0, attempts - 1)
                    if actual_retries != expected_retries:
                        errors.append(
                            f"Phase '{phase.id}' RecoveryNode must use number_of_retries={expected_retries}; "
                            f"generated BT uses {recovery_control.attrib.get('number_of_retries')!r}."
                        )
            else:
                retry = _shared_ancestor(phase_nodes, "RetryUntilSuccessful", parent)
                if retry is None:
                    errors.append(
                        f"Phase '{phase.id}' requires bounded retry of the complete action+verification block "
                        f"with num_attempts={attempts}, but no shared RetryUntilSuccessful decorator was found."
                    )
                else:
                    try:
                        actual = int(retry.attrib.get("num_attempts", ""))
                    except ValueError:
                        actual = -1
                    if actual != attempts:
                        errors.append(
                            f"Phase '{phase.id}' retry bound must be num_attempts={attempts}; "
                            f"generated BT uses {retry.attrib.get('num_attempts')!r}."
                        )

        if continuous_search and action_el is not None:
            recovery_invs = [
                x for x in invocations
                if x["phase_id"] == phase.id and x["role"].startswith("search_recovery_")
            ]
            recovery_els = [named.get(x["node_name"]) for x in recovery_invs]
            for inv, patrol_el in zip(recovery_invs, recovery_els):
                if patrol_el is None:
                    errors.append(
                        f"Phase '{phase.id}' requires continuous search activity <{inv['skill']}> but the BT does not contain it."
                    )
                    continue
                for verify_inv, condition_el in zip(verification_invs, verification_els):
                    if condition_el is None:
                        continue
                    monitor = _shared_ancestor([condition_el, patrol_el], "ReactiveFallback", parent)
                    if monitor is None:
                        errors.append(
                            f"Phase '{phase.id}' must place <{verify_inv['skill']}> and <{inv['skill']}> in one ReactiveFallback."
                        )
                        continue
                    condition_branch = _child_under(monitor, condition_el, parent)
                    patrol_branch = _child_under(monitor, patrol_el, parent)
                    children = list(monitor)
                    if len(children) != 2:
                        errors.append(
                            f"Phase '{phase.id}' continuous-search ReactiveFallback must have exactly two children."
                        )
                    if condition_branch is None or patrol_branch is None or condition_branch is patrol_branch:
                        errors.append(
                            f"Phase '{phase.id}' condition and Patrol must be separate ReactiveFallback branches."
                        )
                    elif condition_branch is not children[0] or patrol_branch is not children[1]:
                        errors.append(
                            f"Phase '{phase.id}' <{verify_inv['skill']}> must be checked before <{inv['skill']}>."
                        )
                    timeout = _shared_ancestor([condition_el, patrol_el], "Timeout", parent)
                    if timeout is None:
                        errors.append(
                            f"Phase '{phase.id}' ReactiveFallback search monitor must be bounded by Timeout."
                        )
                    elif timeout in _ancestors(action_el, parent):
                        errors.append(
                            f"Phase '{phase.id}' VisualizeObject trigger must execute before, not inside, the search Timeout."
                        )
                    else:
                        expected_timeout = phase_timeout_msec(phase)
                        try:
                            actual_timeout = int(timeout.attrib.get("msec", ""))
                        except ValueError:
                            actual_timeout = -1
                        if expected_timeout and actual_timeout != expected_timeout:
                            errors.append(
                                f"Phase '{phase.id}' search Timeout must use msec={expected_timeout}; "
                                f"generated BT uses {timeout.attrib.get('msec')!r}."
                            )
                if not _ordered_by_sequence(action_el, patrol_el, parent):
                    errors.append(
                        f"Phase '{phase.id}' camera-search trigger must execute before the Patrol monitoring loop."
                    )
            warnings.append(
                f"Phase '{phase.id}' continuous search policy verified: trigger once, then bounded "
                "ReactiveFallback(condition first, Patrol second); timeout FAILURE stops downstream mission phases."
            )
        elif compiled_search_recovery and action_el is not None:
            recovery_invs = [
                x for x in invocations
                if x["phase_id"] == phase.id and x["role"].startswith("search_recovery_")
            ]
            recovery_els = [named.get(x["node_name"]) for x in recovery_invs]
            if use_team_recovery:
                for inv, recovery_el in zip(recovery_invs, recovery_els):
                    if recovery_el is None:
                        errors.append(
                            f"Phase '{phase.id}' requires observation recovery <{inv['skill']}> but the BT does not contain it."
                        )
                        continue
                    recovery_control = _shared_ancestor([action_el, recovery_el], "RecoveryNode", parent)
                    if recovery_control is None:
                        errors.append(
                            f"Phase '{phase.id}' search recovery must use the team <RecoveryNode> with the nominal "
                            "behavior as child 1 and recovery behavior as child 2."
                        )
                        continue
                    children = list(recovery_control)
                    if len(children) != 2:
                        errors.append(f"Phase '{phase.id}' RecoveryNode must contain exactly two children.")
                        continue
                    nominal_branch = _child_under(recovery_control, action_el, parent)
                    recovery_branch = _child_under(recovery_control, recovery_el, parent)
                    if nominal_branch is not children[0]:
                        errors.append(
                            f"Phase '{phase.id}' nominal search behavior must be child 1 of RecoveryNode."
                        )
                    if recovery_branch is not children[1]:
                        errors.append(
                            f"Phase '{phase.id}' recovery <{inv['skill']}> must be child 2 of RecoveryNode."
                        )
                warnings.append(
                    f"Phase '{phase.id}' compiles observable search/viewpoint recovery with the formal team RecoveryNode. "
                    "Failure-specific branches requiring hidden runtime error-code discrimination remain metadata-only."
                )
            else:
                # Standard BT.CPP recovery uses Fallback plus ForceFailure. Older
                # demo registries may still use a terminal AlwaysFailure leaf.
                for inv, recovery_el in zip(recovery_invs, recovery_els):
                    if recovery_el is None:
                        errors.append(
                            f"Phase '{phase.id}' requires observation recovery <{inv['skill']}> but the BT does not contain it."
                        )
                        continue
                    fallback = _shared_ancestor([action_el, recovery_el], "Fallback", parent)
                    if fallback is None:
                        errors.append(
                            f"Phase '{phase.id}' search recovery must be represented as an IF/ELSE Fallback between "
                            "the nominal action+verification path and the recovery path."
                        )
                        continue
                    nominal_branch = _child_under(fallback, action_el, parent)
                    recovery_branch = _child_under(fallback, recovery_el, parent)
                    if nominal_branch is None or recovery_branch is None or nominal_branch is recovery_branch:
                        errors.append(
                            f"Phase '{phase.id}' recovery <{inv['skill']}> must be in the alternative branch of the search Fallback."
                        )
                        continue
                    failures = [el for el in recovery_branch.iter() if el.tag in {"ForceFailure", "AlwaysFailure"}]
                    if not failures:
                        errors.append(
                            f"Phase '{phase.id}' recovery branch must force FAILURE so the outer bounded retry "
                            "re-runs observation after recovery instead of treating recovery itself as mission success."
                        )
                    elif not any(
                        (fail.tag == "ForceFailure" and fail in _ancestors(recovery_el, parent))
                        or _ordered_by_sequence(recovery_el, fail, parent)
                        for fail in failures
                    ):
                        errors.append(
                            f"Phase '{phase.id}' recovery <{inv['skill']}> must execute inside <ForceFailure> or before <AlwaysFailure>."
                        )
                warnings.append(
                    f"Phase '{phase.id}' compiles observable search/viewpoint recovery into an IF/ELSE Fallback. "
                    "Failure-specific branches that require runtime error-code discrimination remain metadata-only."
                )
        elif any(r.skill for f in phase.failure_modes for r in f.recovery):
            warnings.append(
                f"Phase '{phase.id}' has failure-specific recovery steps in TaskPlanIR. "
                "They remain semantic metadata unless they can be compiled safely from observable BT success/failure without hidden failure-code discrimination."
            )

        timeout_msec = phase_timeout_msec(phase)
        if timeout_msec and phase_nodes and not continuous_search:
            timeout = _shared_ancestor(phase_nodes, "Timeout", parent)
            if timeout is None:
                warnings.append(
                    f"Phase '{phase.id}' declares bounded failure timeouts (up to {timeout_msec} msec) but the BT has no shared Timeout decorator."
                )
            else:
                try:
                    actual_timeout = int(timeout.attrib.get("msec", ""))
                    if actual_timeout > timeout_msec:
                        warnings.append(
                            f"Phase '{phase.id}' Timeout msec={actual_timeout} exceeds the largest IR timeout {timeout_msec}."
                        )
                except ValueError:
                    pass

    return {"valid": not errors, "errors": errors, "warnings": warnings, "mission_fail_stop": mission_fail_stop}
