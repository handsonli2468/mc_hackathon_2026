# v6 RAG architecture

## Memory types

| Memory | Retrieval source | Purpose |
|---|---|---|
| Capability Memory | Skill RAG | Which exact robot skills/ports can satisfy required semantic capabilities? |
| Procedural Memory | Pattern RAG | What robust BT/planning pattern is relevant to this situation? |
| Semantic Memory | Scene RAG + Location Registry | What is normally true about this meeting environment and where are fixed semantic regions? |
| Episodic Memory | Experience RAG | What happened in previous similar plans/executions? |

Live perception/world_state is working memory and has higher authority than every retrieved prior.

## Retrieval sequence

```text
User mission
  -> Requirement + Retrieval Sketch LLM
  -> Skill retrieval by semantic capability coverage
  -> Pattern/scene/experience FTS5 retrieval
  -> Context bundle
  -> Compact planner
```

The retrieval-sketch call is not an additional LLM call: it extends the pre-existing requirement extraction call.

## Why not ask the first model for exact skill IDs?

If the first model hallucinates an API name and retrieval follows only that name, the wrong first decision can lock the whole pipeline onto irrelevant documents. v6 therefore asks for capabilities and semantic queries first. Exact IDs are introduced only by the Skill RAG database built from the formal registry.

## Closed-set enforcement

After retrieval, `allowed_skill_ids` is authoritative for that mission. The normal planner, repair path and direct baseline are prompted with that subset. Deterministic validation rejects:

1. TaskPlanIR action/condition/recovery skills outside the set.
2. Final BehaviorTree leaf nodes outside the set.

This makes RAG a real grounding boundary rather than decorative context.

## Scene memory and coordinates

Semantic text can point to location IDs, while XY/yaw are stored as structured fields in `rag_locations` and looked up exactly. Raw coordinates are not indexed as retrieval text. For an actionable retrieved location, deterministic grounding exposes a typed `NavigateToPoint` navigation candidate to the planner.

The current engine exports `NavigateToPoint(x, y, yaw_deg, speed)`. Every planned `NavigateToPoint` must match a navigation candidate from an actionable Location-RAG record; coordinates or heading invented by the model are deterministically rejected. The planner may vary only the optional speed using task semantics or relevant high-rated experience.

## Experience writing

Terminal engine states trigger deterministic episode writing. An episode includes the exact plan and engine result. Future work can add offline LLM reflection, but v6 deliberately avoids another online model call.

Execution episodes and human-feedback summaries are indexed in `agent-runtime/state/rag.db`. Human feedback is additionally preserved as readable JSON under `agent-runtime/state/experience-feedback/`.

## Editing knowledge

Runtime editable knowledge lives at:

```text
agent-runtime/rag-knowledge/patterns/
agent-runtime/rag-knowledge/scene/
agent-runtime/rag-knowledge/locations/
```

After editing:

```bash
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
```
