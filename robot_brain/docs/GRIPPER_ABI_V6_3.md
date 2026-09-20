# v6.3 Gripper ABI policy (superseded)

This document is retained as release history. The reviewed v6.4 contract is `SetGripper(position: int)` with range 0–100; see `BT_NODES_V6_4.md`.

## Current policy

`config/bt_skill_policy.yaml` is applied after every live `/nodes?builtin=0` synchronization.

- `GraspObject`: disabled from Planner/RAG even if still exported by the engine.
- `CloseGripper`: disabled from Planner/RAG even if still exported by the engine.
- `OpenGripper`: required planner node and mapped to the generic `release_object` capability.
- `SetGripper`: required planner node, but remains quarantined until its live formal port contract and semantic meaning are reviewed.

This prevents a stale semantic overlay from resurrecting deprecated nodes.

## Why SetGripper is not guessed

`/nodes` is authoritative for node ID, kind, ports, types, defaults, and port descriptions. It does not necessarily explain higher-level semantics such as which command closes the gripper, what numeric units mean, or what SUCCESS guarantees physically. v6.3 therefore does not invent these facts.

After installing/restarting v6.3:

```bash
python3 docker/inspect_gripper_contract.py
```

Provide the output plus these semantic details:

1. exact meaning of every SetGripper input port;
2. legal enum values or numeric range/units for every input;
3. which input/value performs acquisition/closing;
4. what `SUCCESS` guarantees (command accepted, actuator at target, force reached, object held, etc.);
5. possible failure/error codes and their meaning;
6. whether `IsObjectHeld(object_name)` is a valid post-condition/verification for SetGripper;
7. whether SetGripper receives object identity (`object_name`) or only controls actuator state;
8. whether another action must position/align the arm/gripper before SetGripper;
9. recommended timeout/retry/cancellation behavior.

Once known, update `config/bt_engine_semantic_overlay.yaml` under `SetGripper`, set `planner_enabled: true`, and assign the generic capability `acquire_object` only if that contract really implements acquisition in the current robot stack. Then run:

```bash
curl -s -X POST http://127.0.0.1:8000/api/bt-engine/sync-nodes | jq .
```

A successful sync should remove `SetGripper` from `required_planner_node_issues` and add it to `planner_skill_ids`/Skill RAG.

## Mission-level protection

v6.3 mission semantics are node-name independent. Operations such as relocation/fetch/delivery deterministically require generic `acquire_object` and `release_object` capabilities. The goal-effect closure validator rejects plans that acquire without destination-reaching movement or finish without a release. Consequently, changing the gripper node ABI no longer requires putting a specific gripper node name into the mission prompt.
