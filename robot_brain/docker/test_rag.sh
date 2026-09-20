#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${ROBOT_BRAIN_URL:-http://127.0.0.1:8000}"

echo '[rag] sync'
curl -fsS -X POST "${BASE_URL}/api/rag/sync" | jq .

echo '[rag] status'
curl -fsS "${BASE_URL}/api/rag/status" | jq .

echo '[rag] sample skill/pattern retrieval'
curl -fsS -X POST "${BASE_URL}/api/rag/retrieve" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "find the blue box and grab it",
    "required_capabilities": ["locate_object","navigate_to_object","grasp_object","verify_object_found"],
    "retrieval_sketch": {
      "semantic_concepts": ["visual search","grasp"],
      "target_descriptions": ["blue box"],
      "skill_queries": ["visual grounding navigation grasp verification"],
      "pattern_queries": ["bounded visual search action verification"],
      "scene_queries": ["blue box"],
      "experience_queries": ["blue box grasp"]
    }
  }' | jq '{strategy, counts, missing_capabilities, allowed_skill_ids, patterns: [.patterns[].id], scene: [.scene[].id], experiences: [.experiences[].id]}'
