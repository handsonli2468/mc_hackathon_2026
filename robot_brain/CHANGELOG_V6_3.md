# v6.3.0

## Typed environment knowledge

- Promoted Location to a first-class RAG type under `rag-knowledge/locations/`.
- Added direct data-driven location ID/alias resolution independent of Scene retrieval.
- Added semantic Location fallback and exact Scene->Location references.
- Changed Scene retrieval to per-query fan-out + result fusion + overfetch + anchor filtering before final top-K.
- Added Scene-derived executable search priors: high-confidence Scene knowledge can move the robot to a likely region before local visual grounding.
- Added CJK-aware retrieval terms/phrase matching for typed environment knowledge.
- Added generic `manage_rag_location.py`, `manage_rag_scene.py`, and `validate_typed_rag.py` operator tools.
- Removed precise spatial records from default Scene data; coordinates now live only in Location RAG.

## Mission semantics / goal closure

- Added `task_semantics` and generic mission-operation types.
- Added deterministic operation-level capability completion; relocation/delivery/fetch can no longer silently omit acquisition/release capability requirements.
- Added a generic goal-effect closure validator based on skill `provides`, not concrete BT node IDs.
- Preserved task semantics/retrieval provenance into TaskPlanIR.

## Environment integration mode

- Added `LOCATION_KNOWLEDGE_MODE=integration` as v6.3 default for full-pipeline testing.
- Integration mode treats configured positions as executable while retaining calibration/map mismatch warnings.
- `validated` mode restores strict real-map calibration/version gating for actual deployment.

## Gripper ABI migration

- Added `bt_skill_policy.yaml` as a hard Planner/RAG exposure layer on top of live `/nodes`.
- Disabled `GraspObject` and `CloseGripper` regardless of legacy engine export/overlay state.
- `OpenGripper` now provides `release_object`.
- Added required `SetGripper`, intentionally quarantined until its actual live ports and command semantics are supplied/reviewed.
- Added `inspect_gripper_contract.py` and clear required semantic setup information.
- Removed active examples/prompts/tests that relied on deprecated gripper actions.

## Experience compatibility

- New episodes store planner version and plan skill IDs.
- Experience retrieval filters out old episodes whose action skills are no longer present in the active Skill Registry, preventing obsolete ABI demonstrations from biasing the current planner.

## Prompt/data separation

- Planner prompts contain no site-specific place names, coordinates, object-storage relationships, or map facts.
- Environment facts are supplied only through typed RAG context or explicit world_state.

## Retrieval robustness follow-up

- Exact Location alias matching now supports registered CJK aliases embedded inside a full CJK user sentence.
- Scene RAG now has a deterministic registered `target_terms` / `object_categories` channel in addition to FTS fan-out. A configured object/category -> search-region relation therefore does not depend solely on FTS ranking/tokenization.
- Default Scene documents explicitly carry `enabled: true`; high-value object-location priors include registered target terms in RAG data rather than planner prompts.
