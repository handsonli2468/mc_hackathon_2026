# v6.2 Location Grounding

The v6.2 pipeline separates **semantic retrieval** from **executable coordinate binding**.

```text
Scene RAG -> retrieved location_id -> calibration/frame/map gate
          -> active Skill Registry -> location_binding
          -> concrete navigation_candidate
          -> closed-set skill expansion
          -> Planner
          -> deterministic location policy
          -> TaskPlanIR / BT XML
```

For `marker_station`, a valid candidate looks like:

```yaml
marker_station:
  actionable: true
  navigation_candidates:
    - skill_id: NavigateToPoint
      arguments:
        x: 1.2
        y: -0.8
        yaw_deg: "89.954374"
```

The stored scene yaw is radians; `NavigateToPoint.yaw_deg` is a string in degrees. The conversion is deterministic.

## Safety gates

A location is not executable when any required condition fails:

- `calibrated` is false;
- the location frame is unsupported by the skill binding;
- its location type is unsupported;
- an active map version is known and conflicts with the location map version;
- no active planner skill provides a valid location binding;
- required BT input ports cannot be bound.

Unexecutable locations remain available as advisory scene priors only.

## Why Skill RAG now has two passes

Requirement extraction happens before Scene RAG and may call a named station a visual target. v6.2 therefore first covers the coarse required capabilities, retrieves scene locations, then adds only the location-navigation skills that can actually execute those retrieved locations. This avoids exposing the entire registry while breaking the previous circular dependency.
