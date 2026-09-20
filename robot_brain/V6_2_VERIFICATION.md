# v6.2 Verification

## Automated regression suite

Run from the project root:

```bash
python3 -m pytest -q agent/tests
```

Release result:

```text
96 passed
```

The test suite now isolates `PROJECT_ROOT`, `RUNTIME_ROOT`, and `RAG_KNOWLEDGE_ROOT`, so it can be executed from an extracted release instead of accidentally reading `/mlsteam/workspace/agent-runtime`.

## Location-grounding regression

The v6.2 tests cover the original marker-station failure mode:

1. Requirement extraction may still initially request `navigate_to_object`.
2. Scene RAG retrieves `marker_station`.
3. When the stored pose is calibrated and the active map matches, the second Skill-RAG pass adds `NavigateToPoint` to the closed allowed set.
4. `x/y` are copied from the location record and stored-radian yaw is converted deterministically to the formal `yaw_deg` BT port.
5. The location policy rewrites `NavigateToObject("marker station")` to `NavigateToPoint(...)` and removes the redundant visual-search phase for that same static destination.
6. An uncalibrated location remains advisory and does not expose a location-navigation skill.
7. A known map-version mismatch blocks execution of the stored location.

## Static checks

The release was also checked with:

```bash
python3 -m compileall -q agent docker/set_scene_location.py
bash -n docker/upgrade_v6_2_location_grounding.sh
bash -n docker/restart_agent.sh
python3 docker/set_scene_location.py --help
```

All completed successfully.

## Safety note

The shipped meeting-room poses remain `calibrated: false`. They are demo placeholders and are deliberately not executable until an operator records a measured pose. Use `docker/set_scene_location.py ... --calibrated` only with a pose measured in the active robot map.
