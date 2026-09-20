from __future__ import annotations

from copy import deepcopy
from typing import Any

from .registry import effective_capabilities, load_skill_registry, skill_map
from .schemas import MissionOperation, MissionRequirements, TaskPlanIR

# Generic semantic obligations. These are robot-agnostic capabilities, not node IDs.
# They intentionally avoid any environment/location names or scenario-specific words.
_OPERATION_CAPABILITIES: dict[MissionOperation, tuple[str, ...]] = {
    MissionOperation.FIND_OBJECT: ("locate_object",),
    MissionOperation.APPROACH_TARGET: ("navigate_to_object",),
    # The current ABI has no held-object condition. SetGripper can attempt
    # acquisition, but its SUCCESS only confirms actuator state, not object identity.
    MissionOperation.ACQUIRE_OBJECT: ("locate_object", "navigate_to_object", "acquire_object"),
    MissionOperation.RELOCATE_OBJECT: ("locate_object", "navigate_to_object", "acquire_object", "release_object"),
    MissionOperation.DELIVER_OBJECT: ("locate_object", "navigate_to_object", "acquire_object", "release_object"),
    MissionOperation.FETCH_OBJECT: ("locate_object", "navigate_to_object", "acquire_object", "release_object"),
    MissionOperation.NAVIGATE: (),
    MissionOperation.INSPECT: (),
    MissionOperation.GENERAL: (),
}


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def normalize_mission_semantics(requirements: MissionRequirements) -> dict[str, Any]:
    """Complete generic capability obligations implied by a semantic operation.

    The requirement LLM may omit an obvious capability such as release_object.  This
    normalizer fixes only operation-level invariants; it never inserts a concrete BT
    node, location, coordinate, object name, or scene fact.
    """
    req = requirements.model_copy(deep=True)
    before = list(req.required_capabilities)
    required = [*before, *_OPERATION_CAPABILITIES.get(req.task_semantics.operation, ())]
    req.required_capabilities = _dedupe(required)
    added = [x for x in req.required_capabilities if x not in before]
    return {
        "requirements": req,
        "changed": bool(added),
        "added_capabilities": added,
        "operation": req.task_semantics.operation.value,
    }


def validate_goal_effect_closure(
    plan: TaskPlanIR,
    requirements: MissionRequirements,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check high-value mission-level action ordering using semantic capabilities.

    This gate is deliberately node-name agnostic.  It asks whether the selected
    actions expose the generic capabilities required by the operation, so a future
    gripper implementation can replace an old one without changing this validator.
    """
    registry = registry or load_skill_registry()
    skills = skill_map(registry)
    action_caps: list[tuple[int, str, set[str]]] = []
    for idx, phase in enumerate(plan.phases):
        sid = str(phase.nominal_action.skill)
        caps = effective_capabilities(skills.get(sid, {}), phase.nominal_action.arguments)
        action_caps.append((idx, sid, caps))

    def first_index(*caps: str) -> int | None:
        wanted = set(caps)
        for idx, _sid, provided in action_caps:
            if wanted & provided:
                return idx
        return None

    issues: list[str] = []
    op = requirements.task_semantics.operation
    if op in {MissionOperation.ACQUIRE_OBJECT, MissionOperation.RELOCATE_OBJECT, MissionOperation.DELIVER_OBJECT, MissionOperation.FETCH_OBJECT}:
        acquire_i = first_index("acquire_object", "grasp_object", "pick_object")
        if acquire_i is None:
            issues.append("Mission semantics require acquiring an object, but the plan has no action providing acquire_object/grasp_object/pick_object.")
    else:
        acquire_i = None

    if op in {MissionOperation.RELOCATE_OBJECT, MissionOperation.DELIVER_OBJECT, MissionOperation.FETCH_OBJECT}:
        release_i = first_index("release_object", "open_gripper", "gripper_open")
        if release_i is None:
            issues.append("Mission semantics require releasing/placing the transported object, but the plan has no release-capable action.")
        if acquire_i is not None and release_i is not None and release_i <= acquire_i:
            issues.append("Release action occurs before the object acquisition action; relocation/delivery ordering is invalid.")

        # Require movement after acquisition before release, but remain agnostic to
        # whether the destination is map-grounded, object-relative, or another future
        # navigation implementation.
        movement_between = False
        if acquire_i is not None and release_i is not None:
            for idx, _sid, provided in action_caps:
                if acquire_i < idx < release_i and provided.intersection({
                    "navigate_to_location", "navigate_to_map_pose", "navigate_to_xy",
                    "navigate_to_object", "approach_object", "reach_destination",
                }):
                    movement_between = True
                    break
            if not movement_between:
                issues.append("Relocation/delivery plan acquires and releases the object without a destination-reaching movement action in between.")

    return {
        "valid": not issues,
        "operation": op.value,
        "issues": issues,
    }
