# Robot Brain v6.0.1

## Why this patch exists

The first v6 Manta profile showed good stability but exposed three observability/evaluation issues:

1. five timing runs reused one identical prompt, so the benchmark measured repeatability rather than semantic flexibility;
2. weak OR-term FTS matches could surface unrelated Scene RAG priors for open-world objects;
3. the UI showed what RAG retrieved but did not distinguish hard-enforced RAG effects from advisory context that may or may not have influenced the LLM.

## Changes

### Diverse timing profile by default

`agent/tests/run_timing_profile.py` now rotates through five different semantic inputs when `--message` is omitted. The profile output stores `configuration.messages`, `message_mode`, and the exact message used by each run. `--message` still intentionally repeats one prompt, and `--messages-file` accepts a newline-delimited or JSON-list custom suite.

### Scene-RAG relevance guard

Scene retrieval now performs a post-FTS discriminative-anchor check against the original mission and target descriptions. Generic expansion terms such as `search`, `room`, and `workspace` are not enough to pull an unrelated scene prior. A blue-box mission therefore no longer retrieves the presentation-remote prior merely because both documents mention generic search concepts.

### RAG utilization telemetry

Planning results now include `rag_utilization`:

- skills used from the retrieved closed set;
- pattern documents plus observable plan matches;
- scene locations with `calibrated`, `actionable`, `referenced_in_plan`, and a reason when a retrieved location cannot actually affect the BT;
- experience documents marked as planner-prompt context without claiming causal use.

The RAG Context UI includes this utilization report.


### Release-verification semantic guard

The quality gate now rejects a positive `IsObjectHeld` verification after `OpenGripper`. That combination is logically inverted and can cause the ensure-state Fallback to skip the release action. Until the runtime exposes `IsGripperOpen` / `IsObjectReleased` or TaskPlanIR gains explicit negative verification, the agent will not silently accept that contradiction.

### No change to BT-engine node synchronization policy

`BT_ENGINE_SYNC_NODES_ON_START` remains `0` by default. Manual `Sync /nodes` remains available. Setting it to `1` performs a non-fatal startup sync. New engine nodes still require planner semantic metadata before they can become Skill-RAG planning tools.

## Local validation

Run with the project root exported (the Manta runtime already sets this path):

```bash
PROJECT_ROOT=$PWD RUNTIME_ROOT=$PWD/agent-runtime python -m pytest -q
```

Expected for this patch: `83 passed`.
