# Manta deployment — Robot Brain v6

Project mount remains:

```text
/mlsteam/workspace
```

Persistent runtime remains:

```text
/mlsteam/workspace/agent-runtime
```

v6 adds persistent RAG knowledge and index state:

```text
/mlsteam/workspace/agent-runtime/rag-knowledge
/mlsteam/workspace/agent-runtime/state/rag.db
```

## Upgrade from v5.6

Replace the project files under `/mlsteam/workspace` but preserve `agent-runtime`, then:

```bash
cd /mlsteam/workspace
chmod +x docker/*.sh
bash docker/upgrade_v6_rag.sh
bash docker/restart_agent.sh
```

No Docker image rebuild is required. Do not restart Qwen or BTGenBot unless their existing model server processes are unhealthy.

## Verify RAG

```bash
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
curl -s http://127.0.0.1:8000/api/rag/status | jq .
```

Expected initial knowledge is approximately:

```text
skills:     7
patterns:   8
scene:      6
locations:  4
```

Experience count begins at zero and grows after terminal BT-engine executions.

## Verify service health

```bash
curl -s http://127.0.0.1:8000/health | jq .
```

Look for:

```json
{
  "version": "6.4.0",
  "rag": {
    "enabled": true,
    "required": true,
    "ok": true
  },
  "bt_engine_contract": {
    "valid": true,
    "semantic_complete": true,
    "engine_node_count": 7,
    "engine_leaf_count": 7,
    "builtin_node_count": 13,
    "effective_node_count": 20,
    "planner_skill_count": 7
  }
}
```

`engine_node_count` is the count synchronized from `GET /nodes?builtin=0`, so the current live value is 7 custom leaves. The 13 standard BehaviorTree.CPP nodes come from `builtin_bt_nodes.yaml`; `effective_node_count` is the union used to validate generated trees and should be 20.

BT Engine may still be offline without failing overall health unless `BT_ENGINE_REQUIRED_FOR_HEALTH=1`.

## BT Engine

Public endpoint default:

```env
BT_ENGINE_URL=https://mcpc.taile84e23.ts.net
BT_ENGINE_TOKEN=<token>
```

Lab-network alternative:

```env
BT_ENGINE_URL=http://192.168.50.125:8090
BT_ENGINE_TOKEN=<token-if-required>
```

Test:

```bash
bash docker/test_bt_engine_connection.sh
```

## RAG knowledge editing

Patterns:

```text
agent-runtime/rag-knowledge/patterns/*.md
```

Scene memory / location registry:

```text
agent-runtime/rag-knowledge/scene/*.yaml
```

After edits:

```bash
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
```

Do not put real unverified robot coordinates into the demo defaults and assume they are safe. Calibrate locations and set their metadata appropriately before using them for future location-navigation skills.

## Latency profile

v6 does not add another LLM call. The first requirement call also emits retrieval queries; RAG itself is local SQLite FTS5.

```bash
bash docker/run_timing_profile.sh 5 \
  --pipeline hybrid \
  --message "find the blue box and grab it"
```

Normal-path target:

```text
~30 s
```

Watch for:

```text
rag missing=[]
compact_planner_used=True
planner_escalated=False
critic_attempted=False
ir_repairs=0
bt_repairs=0
```

If runs jump to ~90 s, first inspect whether `planner_escalated=True`; do not immediately lower Qwen thinking budgets.

## Unit tests

```bash
python3 -m pytest -q agent/tests
```

## Runtime settings

Important v6 defaults:

```env
PLANNER_REQUIREMENTS_THINKING_BUDGET=640
COMPACT_PLANNER_THINKING_BUDGET=1280
RAG_ENABLED=1
RAG_REQUIRED=1
RAG_SKILL_MAX_DOCS=8
RAG_PATTERN_MAX_DOCS=2
RAG_SCENE_MAX_DOCS=2
RAG_EXPERIENCE_MAX_DOCS=2
RAG_PROMPT_MAX_CHARS=10000
```

## v6.2 deterministic location grounding upgrade

When upgrading a persistent v6.1 runtime, keep `/mlsteam/workspace/agent-runtime` and replace only the project code, then run:

```bash
cd /mlsteam/workspace
bash docker/upgrade_v6_2_location_grounding.sh
```

The upgrade script backs up the affected runtime configuration, preserves existing scene coordinates and `calibrated` flags, fills the `NavigateToPoint` semantic/location-binding contract, and updates the planner prompts.

If `marker_station` has been physically calibrated in the currently loaded map, record the measured pose explicitly:

```bash
python3 docker/set_scene_location.py marker_station \
  --x <MEASURED_X> \
  --y <MEASURED_Y> \
  --yaw-deg <MEASURED_YAW_DEG> \
  --map-version <ACTIVE_MAP_VERSION> \
  --description "Measured marker station pose" \
  --calibrated
```

Then restart the Agent. Startup automatically re-syncs `/nodes`, rebuilds the merged Skill Registry, and rebuilds Skill RAG:

```bash
bash docker/restart_agent.sh
curl -s http://127.0.0.1:8000/api/bt-engine/sync-status | jq .
```

For deployments that always know the active map version, set `LOCATION_REQUIRE_ACTIVE_MAP_VERSION=1` and `ROBOT_MAP_VERSION=<ACTIVE_MAP_VERSION>` in the runtime settings/environment. An uncalibrated pose or a known map-version mismatch remains advisory and cannot be converted to a `NavigateToPoint` action.

## v6.3 typed Scene/Location RAG + gripper ABI upgrade

Keep the persistent `/mlsteam/workspace/agent-runtime` directory, replace the project code with v6.3, then run:

```bash
cd /mlsteam/workspace
chmod +x docker/*.sh
bash docker/upgrade_v6_3_typed_rag.sh
bash docker/restart_agent.sh
python3 docker/validate_typed_rag.py
python3 docker/inspect_gripper_contract.py
curl -s http://127.0.0.1:8000/api/bt-engine/sync-status | jq .
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
```

v6.3 makes Location RAG a first-class knowledge type. Scene RAG keeps environment relationships/search priors; precise poses/regions live under `agent-runtime/rag-knowledge/locations/`. Named locations can be retrieved directly by ID/alias without depending on a Scene document hit, while Scene documents may reference `location_ids` to steer object/category search toward a likely area.

For integration testing the upgrade sets:

```env
LOCATION_KNOWLEDGE_MODE=integration
```

This intentionally treats configured location poses as executable even when they still say `calibrated: false` or the active map version cannot yet be verified. Warnings remain visible in telemetry. Before real deployment, replace placeholder geometry with measured data and switch to:

```env
LOCATION_KNOWLEDGE_MODE=validated
ROBOT_MAP_VERSION=<actual-map-version>
LOCATION_REQUIRE_ACTIVE_MAP_VERSION=1
```

Then restart the Agent and resync RAG.

### Update real location knowledge

```bash
python3 docker/manage_rag_location.py <location_id> \
  --x <x_m> --y <y_m> --yaw-deg <yaw_deg> \
  --map-version <actual-map-version> \
  --alias "<spoken name>" \
  --description "<physical meaning>" \
  --calibrated --enable
```

For an area rather than a point, set `--type STATIC_REGION`; x/y/yaw become the navigation entry pose. Optionally repeat `--region-point X,Y` to store a polygon.

### Update environment/search-prior knowledge

```bash
python3 docker/manage_rag_scene.py <scene_id> \
  --knowledge-type LOCATION_PRIOR \
  --content "<environment relation/search prior>" \
  --target-term "<target/category term>" \
  --location-id <location_id> \
  --confidence 0.9 \
  --requires-visual-verification --enable
```

All such environment facts stay in RAG data; planner prompts contain no site-specific map/place knowledge.

### Current v6.4 BT/gripper ABI

Run `bash docker/upgrade_v6_4_latest_nodes.sh` once after copying v6.4 over an existing runtime, then restart the Agent. The active planner contract uses only the latest supplied custom leaves: `VisualizeObject`, `IsObjectFound`, `NavigateToDetectedObject`, `NavigateToPoint`, `Patrol`, `RotateInPlace`, and `SetGripper`.

The current live port subset is intentionally smaller than the earlier v6.4 snapshot: `IsObjectFound` has only `object_name`; `Patrol` has only `speed`; and `NavigateToDetectedObject` has `speed`, `replan_distance`, `arrive_tolerance`, and `standoff` with a 0.05 m default. Entries such as `<SubTree ID="GoHome"/>` in `/nodes?builtin=0` are registered subtree manifests, not additional planner leaf types.

`SetGripper.position` is an integer from 0 to 100. Zero fully opens/releases; positive values close the jaws. The node has no object-identity input, and no `IsObjectHeld` condition exists in the current ABI, so the Agent never claims that action SUCCESS proves a grasp.

App integration is documented in `docs/APP_API.md`. Structured post-execution feedback is stored under `agent-runtime/state/experience-feedback/` and indexed into Experience RAG.

### Current continuous-search verification

After copying this revision, run the v6.4 upgrade again because the runtime config and RAG knowledge directories are persistent. The upgrade backs up and replaces the affected node semantics, prompts, and the two search-pattern documents.

```bash
cd /mlsteam/workspace
bash docker/upgrade_v6_4_latest_nodes.sh
bash docker/restart_agent.sh
python3 docker/validate_v6_4_release.py
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
python3 docker/test_continuous_search.py --engine-validate
```

The last command prints health/node counts, Agent response, hardened IR, compiler warnings, deterministic BT validation, the complete XML, timing, and live-engine validation. It also saves the full JSON and XML under `agent-runtime/evaluations/continuous-search/`.

For multi-prompt timing and regression profiling:

```bash
bash docker/run_timing_profile.sh 5 --pipeline hybrid
```

Expected node counts are 7 engine-exported custom leaves, 13 local BehaviorTree.CPP-native nodes, and 20 effective nodes. The generated search tree must contain `VisualizeObject` followed by `Timeout(ReactiveFallback(IsObjectFound, Patrol))`, with no search `RotateInPlace` or outer `RetryUntilSuccessful`.
