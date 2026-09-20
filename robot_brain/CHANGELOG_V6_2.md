# Robot Brain v6.2.0

## Deterministic location grounding

- Adds `agent/app/location_grounding.py` to resolve Scene-RAG location records into executable navigation candidates.
- A location is executable only after calibration, frame, location-type, map-version and skill-binding checks.
- Adds generic semantic-overlay `location_binding` support. The current `NavigateToPoint` binding maps `x/y` directly and converts stored `yaw` radians to the formal `yaw_deg` BT port deterministically.
- Adds a second, location-aware Skill-RAG expansion pass. `NavigateToPoint` can enter the closed `allowed_skill_ids` set even when the requirement model initially requested object-relative navigation.
- Planner prompts receive only already-bound navigation candidates; they do not perform coordinate or unit conversion themselves.

## Deterministic plan correction

- Adds `agent/app/location_policy.py`.
- Exact actionable static destinations can rewrite `NavigateToObject("marker station")` to `NavigateToPoint(...)`.
- Redundant `VisualizeObject/TrackObject` phases for the same static destination are removed after the rewrite.
- Movable objects/people remain on the existing perception-grounded `NavigateToObject` path.
- RAG utilization telemetry now records deterministic location bindings as causal use of Scene RAG.

## Formal node / semantic contract

- Adds the live `NavigateToPoint(yaw_deg, y, x)` formal contract to the shipped node model.
- Adds a curated `NavigateToPoint` semantic overlay with `navigate_to_location`, `navigate_to_xy`, and `navigate_to_map_pose` capabilities.
- Fresh-runtime bootstrap now correctly installs the curated semantic overlay instead of accidentally treating the newly copied default merged registry as a legacy registry.

## Scene-location safety

- Legacy scene `yaw` values are explicitly annotated with `yaw_unit: rad`.
- Optional `ROBOT_MAP_VERSION` / `world_state.map.version` is checked against stored location `map_version` when available.
- `LOCATION_REQUIRE_ACTIVE_MAP_VERSION=1` can make active-map knowledge mandatory.
- Uncalibrated locations remain advisory and never expand a location-navigation skill.
- Adds `docker/set_scene_location.py` for backed-up, explicit location calibration edits.

## Observability

- Adds `location_skill_expansion` and `location_grounding` to RAG context.
- Adds `location_grounding_policy` to API/UI planning artifacts.
- `TaskPlanIR.retrieval_sketch` is preserved instead of becoming empty after compact/full-plan enrichment.

## Upgrade

Use `docker/upgrade_v6_2_location_grounding.sh`; it preserves runtime scene coordinates/calibration flags, fills missing NavigateToPoint semantic fields, annotates legacy yaw units, updates planner prompts, and relies on the next Agent restart to perform the normal automatic `/nodes` sync.
