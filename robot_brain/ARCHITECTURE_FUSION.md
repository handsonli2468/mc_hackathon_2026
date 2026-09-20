# Robot Brain v6 architecture

```text
User / Human
    │
    ▼
Conversation resolver
    │
    ▼
Requirement + Retrieval Sketch — Qwen thinking
    │
    ├─ semantic capabilities
    ├─ targets / relations
    └─ RAG queries
    │
    ▼
Adaptive RAG Controller — SQLite FTS5
    │
    ├─ Skill RAG       = Capability Memory
    ├─ Pattern RAG     = Procedural Memory
    ├─ Scene RAG       = Semantic Memory
    └─ Experience RAG  = Episodic Memory
    │
    ▼
RAG Context Bundle
    │
    ├─ allowed_skill_ids (closed set)
    ├─ exact skill ports / semantics
    ├─ relevant BT patterns
    ├─ scene priors / semantic location IDs
    └─ similar execution episodes
    │
    ▼
Compact Qwen Planner — thinking ON
    │
    ▼
Deterministic Full TaskPlanIR Enricher
    │
    ├─ verification
    ├─ timeout/failure metadata
    ├─ grounding policy
    ├─ search hardening
    └─ RAG closed-set validation
    │
    ▼
BTGenBot-2 phase compiler
    │
    ▼
Deterministic BT assembly
    │
    ├─ fail-stop Sequence
    ├─ RecoveryNode
    ├─ Fallback ensure-state guards
    ├─ RetryUntilSuccessful
    └─ Timeout
    │
    ▼
Structural + semantic + RAG XML validation
    │
    ▼
BT Engine HTTP
    │
    ├─ live trace -> UI / Agent
    └─ terminal result -> deterministic Experience Writer
                            │
                            └────> Experience RAG
```

## Authority boundary

RAG is context, not control authority.

```text
Formal BT engine contract / validators
> live world state / perception
> retrieved exact skill contracts
> calibrated static location registry
> patterns
> scene priors
> previous experience
```

## Why v6 keeps two model calls

The pre-existing requirement extraction call now also performs retrieval-query expansion. This avoids a third LLM round trip. Local retrieval then selects a compact context before the normal 1280-token compact planner runs.

## Why detailed skills are not shown before RAG

The first LLM sees capability names only. Exact node IDs, ports, failure semantics, verification pairs and allowed values come from Skill RAG. This makes retrieval responsible for grounding the planner to the actual robot API.

## Scene locations

The Scene RAG database can point to deterministic location IDs and exact XY/yaw records. v6 deliberately does not compile coordinate navigation because the current formal BT-engine contract lacks such a node. Once the engine team registers one, it becomes another retrievable skill rather than a special planner hack.
