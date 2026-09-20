# v6.1 verification

Validation performed before packaging:

- Python compileall: passed for `agent/app`.
- Existing + new unit/regression tests: **90 passed**.
- Frontend JavaScript syntax: `node --check frontend/app.js` passed.
- Shell syntax: `docker/common.sh`, `docker/start_agent.sh`, and `docker/upgrade_v6_1_node_sync.sh` passed `bash -n`.
- Clean-runtime sync integration: 11/11 live custom nodes formally ingested; 10/10 Action/Condition leaves promoted with the shipped semantic overlay; semantic contract complete.
- New-node simulation: added `NavigateToXY(x,y,yaw)` is formally ingested (12/12 total) and emitted to the semantic review scaffold, but quarantined from Planner/RAG until semantics are reviewed.
- v6.0.x migration simulation: semantic metadata from the legacy `skill_registry.yaml` is migrated into `bt_engine_semantic_overlay.yaml`.
