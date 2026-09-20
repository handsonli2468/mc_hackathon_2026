# BT Node Synchronization — v6.1

## Goal

Robot Brain must never plan against a stale or hand-copied executable node contract. On every Agent process start/restart it fetches the live custom-node model from:

```http
GET /nodes?builtin=0
```

The synchronization has two separate trust layers:

```text
BT Engine /nodes
  │
  ├─ authoritative formal facts
  │    ID / kind / input-output-inout ports / type / default
  │
  ▼
config/bt_engine_registry.generated.yaml
  │
  │ +
  ▼
config/bt_engine_semantic_overlay.yaml
  │    description / provides / preconditions / verification /
  │    grounding policy / suitable_for / resources / failures ...
  │
  ▼
config/skill_registry.yaml
  │
  ▼
Skill RAG
```

## Startup behavior

`BT_ENGINE_ENABLED=1` causes one synchronous sync attempt during every FastAPI startup. This includes normal start and Agent restart. The old `BT_ENGINE_SYNC_NODES_ON_START` flag is no longer an opt-in gate.

Emergency/offline development only:

```env
BT_ENGINE_SKIP_STARTUP_NODE_SYNC=1
```

The default is `0`.

If the engine is temporarily unavailable, the Agent records the failed attempt and continues with the last-known-good contract. This is visible in `/health` and `/api/bt-engine/sync-status`; it is not silently treated as a successful refresh.

## What “all nodes are ingested” means

Every node present in the `TreeNodesModel` returned by `/nodes?builtin=0` is persisted in:

```text
config/bt_engine_registry.generated.yaml
```

This includes custom Action, Condition, Control and Decorator nodes. Input, output and inout ports are preserved. The sync report contains `formal_ingested_node_ids` and `all_engine_nodes_formally_ingested` so coverage is observable.

Planner/RAG exposure is intentionally narrower. Only Action/Condition nodes with a valid local semantic overlay are promoted to `skill_registry.yaml`. A node is **not lost** when semantics are missing: it remains in the formal registry and is quarantined from planning.

## New node example: NavigateToXY

Suppose the engine starts exporting:

```xml
<Action ID="NavigateToXY">
  <input_port name="x" type="double"/>
  <input_port name="y" type="double"/>
  <input_port name="yaw" type="double"/>
</Action>
```

The next Agent restart immediately ingests that formal contract. Until semantics are reviewed, it appears in:

```text
state/bt_engine_semantic_overlay.missing.yaml
```

A reviewed overlay can be added to `config/bt_engine_semantic_overlay.yaml`:

```yaml
nodes:
  NavigateToXY:
    planner_enabled: true
    description: Navigate to a calibrated pose in the map frame.
    provides:
      - navigate_to_location
      - navigate_to_map_pose
    preconditions: []
    success_semantics:
      - robot_at_map_pose
    suitable_for:
      - calibrated_static_scene_location
    requires_visual_grounding: false
    recommended_verification: []
```

Do **not** duplicate `kind`, port `type`, `default`, or `required` in the semantic overlay. Those values always come from the live engine and are overwritten by the formal merge.

After the next restart or manual sync, `NavigateToXY` becomes a Planner/RAG skill with the live `x/y/yaw` ports.

## Quarantine rules

A leaf is not exposed to Planner/RAG when any of the following is true:

- no semantic overlay exists;
- `planner_enabled: false`;
- overlay references ports that the live engine no longer exports;
- required description/capability metadata is missing;
- its `recommended_verification` points to an unavailable/quarantined node or to a non-Condition node.

This prevents a changed engine ABI from leaving a stale semantic tool active.

## Atomicity and rollback

Before replacing active files, v6.1 validates a candidate merge. Changed contract files are backed up to:

```text
agent-runtime/config-backups/bt-node-sync-<UTC timestamp>/
```

If post-write validation unexpectedly fails, the active XML/formal/skill files are restored from that backup.

## Observability

```bash
curl -s http://127.0.0.1:8000/api/bt-engine/sync-status | jq .
```

Manual refresh:

```bash
curl -s -X POST http://127.0.0.1:8000/api/bt-engine/sync-nodes | jq .
```

Important report fields:

```text
ok
trigger
last_success_at
engine_node_count
engine_leaf_count
builtin_node_count
effective_node_count
formal_ingested_node_ids
planner_skill_count
planner_skill_ids
missing_semantic_overlay
invalid_semantic_overlay
semantic_dependency_issues
quarantined_leaf_nodes
semantic_complete
rag_sync
```

`semantic_complete=false` does not mean the formal `/nodes` fetch lost nodes. It means one or more live leaves are intentionally quarantined from planning until their semantics are reviewed.
