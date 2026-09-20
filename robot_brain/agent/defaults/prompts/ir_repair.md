Repair the semantic TaskPlanDraft using the deterministic IR validation errors supplied below.
Do not generate XML and do not provide prose reasoning.
Your response is constrained to TaskPlanDraft.
Do not invent skills. Preserve the user's mission and every valid part of the previous draft.
Do not add mission_status, missing_capabilities, or required_user_information; deterministic orchestration owns those fields.

Cleanup repair rule:
- cleanup is reserved for true teardown/resource-release actions and must use only skills explicitly marked cleanup_eligible in SkillManifest;
- move search/navigation/viewpoint/manipulation retry behavior into failure_modes.recovery;
- when no cleanup-eligible teardown is needed, set cleanup to [].

Continuous-search repair rule:
- VisualizeObject triggers a continuous camera query; IsObjectFound is the required observable verification.
- When the registry declares a continuous Patrol role, repair search failure policy to use Patrol, finite timeout, max_attempts=1, and MISSION_FAILURE. Do not use RotateInPlace for search.
- Native control/decorator topology is server-owned and will be compiled as Timeout(ReactiveFallback(IsObjectFound, Patrol)).

V5.3 FORMAL BT-ENGINE CONTRACT REPAIR:
- Repair legacy or invented node IDs to exact registered runtime IDs only.
- Use exact input ports from SkillManifest.
- Named-object nodes consume literal `object_name` strings. Do not use phase output references or synthetic TrackedObject handles.
