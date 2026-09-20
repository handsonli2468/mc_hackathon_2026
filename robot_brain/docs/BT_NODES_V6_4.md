# Current BT nodes and continuous-search policy

The live BT Engine exports 7 custom leaf nodes. The Agent additionally registers 13 BehaviorTree.CPP-native control, decorator, and structural nodes locally, so the effective planning/validation vocabulary is 20 nodes. `/health.bt_engine_contract.engine_node_count` is therefore expected to be 7; use `effective_node_count` for the combined total.

## Custom engine leaves

- `VisualizeObject(object_name)` selects the query and triggers continuous camera search. SUCCESS means the request was accepted, not that the object was found.
- `IsObjectFound(object_name)` polls the current camera search at the engine-owned cadence. SUCCESS guarantees a current pose; FAILURE means not found at that poll.
- `Patrol(speed="normal")` starts an opaque, engine-owned navigation patrol. The Agent does not plan its route or provide geometry. In continuous search it must return RUNNING while active and support halt.
- `NavigateToDetectedObject(speed="normal", replan_distance=0.05, arrive_tolerance=0.05, standoff=0.05)` follows the latest pose published by perception. Pose timeout and replanning cadence are engine-owned in the current ABI.
- `NavigateToPoint(x, y, yaw_deg="", speed="normal")` navigates only to a map-frame pose copied from an actionable Location-RAG navigation candidate; free-form coordinates are rejected. Only its optional speed may vary.
- `RotateInPlace(angle_deg=45)` performs an explicit rotation when the mission asks for it; it is not the object-search policy.
- `SetGripper(position)` commands percent closure: 0 fully open, 100 fully closed. When initial jaw state is unknown, open to 0 before approach/grasp. An ordinary grasp starts at 50 unless object-specific semantics or relevant high-rated Experience RAG supports another value. SUCCESS does not prove that an object is held.

## BehaviorTree.CPP-native nodes

All 13 IDs below are native BehaviorTree.CPP nodes or native XML structure:

| ID | Kind | Exact intended use |
|---|---|---|
| `Sequence` | Control | Tick children left-to-right. Stop on FAILURE/RUNNING and succeed only when all succeed. Use as the fail-stop mission root. |
| `ReactiveSequence` | Control | Restart at child 1 every tick; an earlier failing condition can halt a later RUNNING child. Use for continuously checked guards. |
| `Fallback` | Control | Try alternatives left-to-right until one returns SUCCESS/RUNNING; fail only if all fail. |
| `ReactiveFallback` | Control | Restart at child 1 every tick; an earlier condition becoming SUCCESS halts a later RUNNING child. This is the search monitor. |
| `Inverter` | Decorator | Swap SUCCESS and FAILURE; pass RUNNING unchanged. |
| `ForceSuccess` | Decorator | Pass RUNNING, then force either terminal child result to SUCCESS. Use only for explicitly optional work. |
| `ForceFailure` | Decorator | Pass RUNNING, then force either terminal child result to FAILURE. Use only to deliberately drive a parent alternative/retry. |
| `KeepRunningUntilFailure` | Decorator | Return RUNNING for child SUCCESS/RUNNING and FAILURE for child FAILURE. Generated uses require a finite enclosing termination policy. |
| `Repeat` | Decorator | Repeat after child SUCCESS for `num_cycles`; fail immediately on child FAILURE. Engine `-1` is infinite, but Agent output permits only 1–10. |
| `RetryUntilSuccessful` | Decorator | Retry after child FAILURE up to `num_attempts`; succeed immediately on child SUCCESS. Engine `-1` is infinite, but Agent output permits only 1–10. |
| `Delay` | Decorator | Wait `delay_msec` before ticking its one child. It delays start; it is not a timeout. |
| `Timeout` | Decorator | Halt a child still RUNNING after `msec` and return FAILURE. |
| `SubTree` | Structural | Invoke another `BehaviorTree` declared in the same XML by ID, with explicit remapping or `_autoremap`. Never reference an undeclared ID. |

## Canonical continuous object search

```xml
<Sequence name="mission_sequence">
  <Sequence name="find_bottle_continuous_search">
    <VisualizeObject name="trigger_bottle_search" object_name="bottle"/>
    <Timeout name="find_bottle_timeout" msec="60000">
      <ReactiveFallback name="find_bottle_monitor">
        <IsObjectFound name="bottle_found" object_name="bottle"/>
        <Patrol name="patrol_while_searching"/>
      </ReactiveFallback>
    </Timeout>
  </Sequence>
  <NavigateToDetectedObject name="approach_bottle" speed="normal"/>
</Sequence>
```

Execution semantics:

1. `VisualizeObject` runs once and starts or changes the continuous camera query.
2. `ReactiveFallback` checks `IsObjectFound` first on every tick.
3. While the condition fails, `Patrol` runs. When the condition succeeds, the reactive control halts Patrol and succeeds.
4. If the timeout expires, it halts Patrol and returns FAILURE.
5. The outer mission `Sequence` propagates FAILURE, so `NavigateToDetectedObject` and every later action remain unticked. The execution bridge reports the failed mission to the API user.

Do not add `RotateInPlace`, `RetryUntilSuccessful`, or `ForceFailure` around this policy. Patrol internals and route geometry belong to the BT Engine implementation, not the Agent or LLM.

## Contract maintenance

Formal IDs, ports, types, and defaults come only from the live `/nodes?builtin=0` export. Planner semantics come from `bt_engine_semantic_overlay.yaml`. When a custom node contract changes, update the formal export, overlay, regenerated registry, prompts/tests, then run the Manta release and search-policy tests.

BehaviorTree.CPP references: [Sequences](https://www.behaviortree.dev/docs/nodes-library/SequenceNode/), [Fallbacks](https://www.behaviortree.dev/docs/nodes-library/FallbackNode/), and [Decorators/SubTree](https://www.behaviortree.dev/docs/nodes-library/DecoratorNode/).
