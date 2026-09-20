#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/agent-runtime}"
mkdir -p "${RUNTIME_ROOT}/config"
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.yaml" "${RUNTIME_ROOT}/config/skill_registry.yaml"
cp "${PROJECT_ROOT}/agent/defaults/bt_engine_nodes.xml" "${RUNTIME_ROOT}/config/bt_engine_nodes.xml"
echo "[deprecated-name] Enabled the formal BT-engine skill registry: ${RUNTIME_ROOT}/config/skill_registry.yaml"
