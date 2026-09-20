#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v6-rag-${STAMP}"
mkdir -p "${BACKUP}/prompts" "${BACKUP}/rag-knowledge"
for f in settings.env skill_registry.yaml builtin_bt_nodes.yaml bt_engine_nodes.xml; do
  [[ -f "${CONFIG_ROOT}/${f}" ]] && cp "${CONFIG_ROOT}/${f}" "${BACKUP}/${f}"
done
for f in requirement planner critic direct_bt compiler ir_repair bt_repair compact_planner; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}.md" ]] && cp "${CONFIG_ROOT}/prompts/${f}.md" "${BACKUP}/prompts/${f}.md"
done
if [[ -d "${RAG_KNOWLEDGE_ROOT}" ]]; then
  cp -a "${RAG_KNOWLEDGE_ROOT}/." "${BACKUP}/rag-knowledge/" 2>/dev/null || true
fi

# v6 keeps the formal BT engine registry but changes planning prompts so detailed
# skills are retrieved instead of placed wholesale in the planner context.
cp "${PROJECT_ROOT}/agent/defaults/prompts/requirement.md" "${CONFIG_ROOT}/prompts/requirement.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/compact_planner.md" "${CONFIG_ROOT}/prompts/compact_planner.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/planner.md" "${CONFIG_ROOT}/prompts/planner.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/critic.md" "${CONFIG_ROOT}/prompts/critic.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/ir_repair.md" "${CONFIG_ROOT}/prompts/ir_repair.md"

mkdir -p "${RAG_KNOWLEDGE_ROOT}/patterns" "${RAG_KNOWLEDGE_ROOT}/scene"
cp -a "${PROJECT_ROOT}/agent/defaults/rag/patterns/." "${RAG_KNOWLEDGE_ROOT}/patterns/"
cp -a "${PROJECT_ROOT}/agent/defaults/rag/scene/." "${RAG_KNOWLEDGE_ROOT}/scene/"

SETTINGS="${CONFIG_ROOT}/settings.env"
append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${SETTINGS}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS}"
  fi
}
cat >> "${SETTINGS}" <<'EOT'

# v6 adaptive RAG: local SQLite FTS5, no additional LLM call.
EOT
if grep -q '^PLANNER_REQUIREMENTS_THINKING_BUDGET=768$' "${SETTINGS}"; then
  sed -i 's/^PLANNER_REQUIREMENTS_THINKING_BUDGET=768$/PLANNER_REQUIREMENTS_THINKING_BUDGET=640/' "${SETTINGS}"
fi
append_if_missing RAG_ENABLED 1
append_if_missing RAG_REQUIRED 1
append_if_missing RAG_SKILL_MAX_DOCS 8
append_if_missing RAG_PATTERN_ENABLED 1
append_if_missing RAG_PATTERN_MAX_DOCS 2
append_if_missing RAG_SCENE_ENABLED 1
append_if_missing RAG_SCENE_MAX_DOCS 2
append_if_missing RAG_EXPERIENCE_ENABLED 1
append_if_missing RAG_EXPERIENCE_MAX_DOCS 2
append_if_missing RAG_PROMPT_MAX_CHARS 10000

cat <<OUT
[v6] adaptive RAG installed.
[v6] backup: ${BACKUP}
[v6] runtime RAG knowledge: ${RAG_KNOWLEDGE_ROOT}
[v6] no Docker rebuild and no model-server restart are required.
[v6] restart only the FastAPI agent, then sync/check RAG:
  bash docker/restart_agent.sh
  curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
  curl -s http://127.0.0.1:8000/api/rag/status | jq .
OUT
