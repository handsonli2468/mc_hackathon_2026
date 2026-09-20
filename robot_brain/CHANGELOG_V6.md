# Robot Brain v6 changelog

## Memory-grounded adaptive RAG

v6 adds mandatory RAG to the Hybrid planning path without adding another LLM call.

### Requirement / retrieval expansion

`MissionRequirements` now contains `retrieval_sketch`:

- semantic concepts
- target descriptions
- skill queries
- pattern queries
- scene queries
- experience queries

The first Qwen call sees only a capability ontology, not the complete SkillManifest.

### Skill RAG

- Exact skill documents are generated from the active formal registry.
- Retrieval is capability-coverage driven and backed by SQLite FTS5.
- Verification and viewpoint-recovery dependencies are added deterministically.
- Detailed planners receive only the retrieved closed skill set.
- TaskPlanIR and final BT XML are both checked against that retrieved set.

### Pattern RAG

Ships eight initial procedural patterns covering visual search, grounding, fail-stop sequencing, ensure-state behavior, verification, tracking, scene priors, and human clarification.

### Scene RAG

Ships a meeting-room semantic knowledge base and structured Location Registry.

- semantic facts are searchable;
- exact coordinates are deterministic registry data, not embedding text;
- scene priors cannot override live perception;
- demo coordinates are uncalibrated and are not treated as executable navigation facts;
- v6 does not invent a `NavigateToLocation` node because the current formal BT engine does not register one.

### Experience RAG

Terminal BT Engine outcomes are automatically stored as episodic experiences. The deterministic summary includes the mission, semantic plan phases, execution state, last failure and notes. No LLM summarization call is added to the normal path.

### UI / API

- New RAG status in the top bar.
- New `RAG Context` tab.
- `GET /api/rag/status`
- `POST /api/rag/sync`
- `POST /api/rag/retrieve`
- `/health` reports RAG status and gates health when `RAG_REQUIRED=1` but no skill documents are indexed.

### Latency

- No new LLM call was introduced.
- Requirements thinking default changes from 768 to 640 tokens when upgrading from the untouched old default.
- Compact planning remains 1280 thinking tokens.
- Planner prompt uses retrieved skill subset rather than the complete SkillManifest.
- RAG context is capped at 10,000 characters.
- Existing full-planner escalation remains available for genuine deterministic validation failures.

The ~30 s normal-case target must still be verified on Manta; it is not claimed from local unit tests.

### Tests

Adds RAG bootstrap/retrieval, capability coverage, scene/location, episodic memory, TaskPlanIR closed-set and BT XML closed-set tests. Final local suite: **78 passed**. Static Python/JS/shell syntax checks also pass.
