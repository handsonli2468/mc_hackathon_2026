#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v5.3-contract-${STAMP}"
mkdir -p "${BACKUP}/prompts"
for f in skill_registry.yaml builtin_bt_nodes.yaml bt_engine_nodes.xml settings.env; do
  [[ -f "${CONFIG_ROOT}/${f}" ]] && cp "${CONFIG_ROOT}/${f}" "${BACKUP}/${f}"
done
for f in requirement planner critic direct_bt compiler ir_repair bt_repair; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}.md" ]] && cp "${CONFIG_ROOT}/prompts/${f}.md" "${BACKUP}/prompts/${f}.md"
done

cp "${PROJECT_ROOT}/agent/defaults/skill_registry.yaml" "${CONFIG_ROOT}/skill_registry.yaml"
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.demo.yaml" "${CONFIG_ROOT}/skill_registry.demo.yaml"
cp "${PROJECT_ROOT}/agent/defaults/builtin_bt_nodes.yaml" "${CONFIG_ROOT}/builtin_bt_nodes.yaml"
cp "${PROJECT_ROOT}/agent/defaults/bt_engine_nodes.xml" "${CONFIG_ROOT}/bt_engine_nodes.xml"
for f in requirement planner critic direct_bt compiler ir_repair bt_repair; do
  cp "${PROJECT_ROOT}/agent/defaults/prompts/${f}.md" "${CONFIG_ROOT}/prompts/${f}.md"
done

cat <<OUT
[v5.3] installed formal team BT-engine skill/control contract and v5.3 prompts.
[v5.3] backup: ${BACKUP}
[v5.3] preserved settings.env and all model-server settings.
[v5.3] restart only the FastAPI agent; Qwen/BTGenBot servers do not need restart.
OUT
