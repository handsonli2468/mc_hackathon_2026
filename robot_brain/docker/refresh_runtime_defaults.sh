#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v6-${STAMP}"
mkdir -p "${BACKUP}/prompts" "${BACKUP}/rag-knowledge"

# Back up only files this upgrade may overwrite.
[[ -f "${CONFIG_ROOT}/builtin_bt_nodes.yaml" ]] && cp "${CONFIG_ROOT}/builtin_bt_nodes.yaml" "${BACKUP}/builtin_bt_nodes.yaml"
[[ -f "${CONFIG_ROOT}/skill_registry.demo.yaml" ]] && cp "${CONFIG_ROOT}/skill_registry.demo.yaml" "${BACKUP}/skill_registry.demo.yaml"
[[ -f "${CONFIG_ROOT}/bt_engine_nodes.xml" ]] && cp "${CONFIG_ROOT}/bt_engine_nodes.xml" "${BACKUP}/bt_engine_nodes.xml"
for f in requirement planner critic direct_bt compiler ir_repair bt_repair compact_planner; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}.md" ]] && cp "${CONFIG_ROOT}/prompts/${f}.md" "${BACKUP}/prompts/${f}.md"
done
[[ -d "${RAG_KNOWLEDGE_ROOT}" ]] && cp -a "${RAG_KNOWLEDGE_ROOT}/." "${BACKUP}/rag-knowledge/" 2>/dev/null || true

cp "${PROJECT_ROOT}/agent/defaults/builtin_bt_nodes.yaml" "${CONFIG_ROOT}/builtin_bt_nodes.yaml"
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.demo.yaml" "${CONFIG_ROOT}/skill_registry.demo.yaml"
cp "${PROJECT_ROOT}/agent/defaults/bt_engine_nodes.xml" "${CONFIG_ROOT}/bt_engine_nodes.xml"
for f in requirement planner critic direct_bt compiler ir_repair bt_repair compact_planner; do
  cp "${PROJECT_ROOT}/agent/defaults/prompts/${f}.md" "${CONFIG_ROOT}/prompts/${f}.md"
done
mkdir -p "${RAG_KNOWLEDGE_ROOT}/patterns" "${RAG_KNOWLEDGE_ROOT}/scene"
cp -a "${PROJECT_ROOT}/agent/defaults/rag/patterns/." "${RAG_KNOWLEDGE_ROOT}/patterns/"
cp -a "${PROJECT_ROOT}/agent/defaults/rag/scene/." "${RAG_KNOWLEDGE_ROOT}/scene/"

cat <<EOF
[refresh] v6 shipped prompts/builtins/engine-model/RAG knowledge copied into runtime config.
[refresh] backup: ${BACKUP}
[refresh] preserved active files:
  ${CONFIG_ROOT}/skill_registry.yaml
  ${CONFIG_ROOT}/settings.env
EOF
