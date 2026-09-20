#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/agent-runtime}"
CONFIG_ROOT="${RUNTIME_ROOT}/config"
mkdir -p "${CONFIG_ROOT}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/formal-bt-engine-${STAMP}"
mkdir -p "${BACKUP}"
for f in skill_registry.yaml builtin_bt_nodes.yaml bt_engine_nodes.xml; do
  [[ -f "${CONFIG_ROOT}/${f}" ]] && cp "${CONFIG_ROOT}/${f}" "${BACKUP}/${f}"
done
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.yaml" "${CONFIG_ROOT}/skill_registry.yaml"
cp "${PROJECT_ROOT}/agent/defaults/builtin_bt_nodes.yaml" "${CONFIG_ROOT}/builtin_bt_nodes.yaml"
cp "${PROJECT_ROOT}/agent/defaults/bt_engine_nodes.xml" "${CONFIG_ROOT}/bt_engine_nodes.xml"
echo "[v5.3] formal team BT-engine contract installed."
echo "[v5.3] backup: ${BACKUP}"
