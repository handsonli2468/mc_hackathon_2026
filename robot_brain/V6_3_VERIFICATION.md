# v6.3.0 Verification

## Scope

This verification covers the typed Scene/Location RAG redesign, mission-semantic capability completion and goal closure, environment trust modes, live BT-node exposure policy, and the current gripper ABI migration.

## Automated regression

Executed from the project root:

```bash
PYTHONPATH=. pytest -q
```

Result:

```text
105 passed
```

The v6.3 tests include:

- named Location RAG retrieval independent of Scene RAG;
- exact ID/alias retrieval;
- CJK location alias matching inside a full user sentence;
- Scene target/category registered-term retrieval independent of FTS ranking;
- Scene -> Location search-prior insertion before local visual search;
- integration-mode execution of configured-but-uncalibrated locations;
- validated-mode rejection of uncalibrated locations;
- generic relocation capability completion (`acquire_object` + `release_object`);
- goal-effect closure rejection when a transport plan omits release;
- gripper exposure policy that disables legacy `GraspObject`/`CloseGripper`;
- prompt scan preventing default site/place knowledge from appearing in planner prompts.

## Static validation

```bash
python3 -m compileall -q agent docker
for f in docker/*.sh; do bash -n "$f"; done
python3 docker/validate_typed_rag.py --root agent/defaults/rag
```

Result:

```text
Python compile: PASS
Shell syntax: PASS
Typed RAG validation: PASS
locations=4 aliases=9
```

## Prompt/data separation check

Planner/default prompt files were scanned for the bundled site-specific location/object knowledge. No bundled place names or map facts are embedded in the planner prompts. Environment knowledge is loaded from typed RAG files or explicit `world_state`.

## Gripper limitation intentionally retained

`GraspObject` and `CloseGripper` are hard-disabled by `bt_skill_policy.yaml`. `OpenGripper` is active and provides `release_object`.

`SetGripper` is intentionally **not** planner-enabled in the shipped semantic overlay because its current live port meanings/command values/SUCCESS semantics were not supplied. Startup `/nodes` sync still formally ingests the node. Run:

```bash
python3 docker/inspect_gripper_contract.py \
  --template-out /mlsteam/workspace/agent-runtime/state/setgripper_overlay_scaffold.yaml
```

Then review the real contract and update `config/bt_engine_semantic_overlay.yaml`. This avoids silently guessing a manipulation ABI.

## Environment mode

Default:

```env
LOCATION_KNOWLEDGE_MODE=integration
```

This intentionally treats enabled configured Location RAG poses as executable for pipeline testing, while keeping calibration/map warnings visible. Switch to `validated` after replacing demo data with measured real-map data.
