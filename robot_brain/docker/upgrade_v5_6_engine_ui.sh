#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v5.6-engine-ui-${STAMP}"
mkdir -p "${BACKUP}/prompts"
for f in settings.env skill_registry.yaml builtin_bt_nodes.yaml bt_engine_nodes.xml; do
  [[ -f "${CONFIG_ROOT}/${f}" ]] && cp "${CONFIG_ROOT}/${f}" "${BACKUP}/${f}"
done
for f in requirement.md compact_planner.md; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}" ]] && cp "${CONFIG_ROOT}/prompts/${f}" "${BACKUP}/prompts/${f}"
done

# Install the current formal runtime contract. v5.6 adds NavigateToObject.distance
# while preserving the same registered node IDs and the team RecoveryNode contract.
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.yaml" "${CONFIG_ROOT}/skill_registry.yaml"
cp "${PROJECT_ROOT}/agent/defaults/builtin_bt_nodes.yaml" "${CONFIG_ROOT}/builtin_bt_nodes.yaml"
cp "${PROJECT_ROOT}/agent/defaults/bt_engine_nodes.xml" "${CONFIG_ROOT}/bt_engine_nodes.xml"
cp "${PROJECT_ROOT}/agent/defaults/prompts/requirement.md" "${CONFIG_ROOT}/prompts/requirement.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/compact_planner.md" "${CONFIG_ROOT}/prompts/compact_planner.md"

SETTINGS="${CONFIG_ROOT}/settings.env"
append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${SETTINGS}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS}"
  fi
}

cat >> "${SETTINGS}" <<'EOT'

# v5.6 BT-engine authentication / public endpoint support
EOT
append_if_missing BT_ENGINE_ENABLED 1
append_if_missing BT_ENGINE_TOKEN ""
append_if_missing BT_ENGINE_REQUEST_TIMEOUT_SECONDS 5
append_if_missing BT_ENGINE_HEALTH_TIMEOUT_SECONDS 1.5
append_if_missing BT_ENGINE_POLL_INTERVAL_SECONDS 0.3
append_if_missing BT_ENGINE_MAX_POLL_SECONDS 900
append_if_missing BT_ENGINE_SYNC_NODES_ON_START 0
append_if_missing BT_ENGINE_REQUIRED_FOR_HEALTH 0

# Only migrate the old project default. Preserve any URL the user already customized.
if grep -q '^BT_ENGINE_URL=http://127\.0\.0\.1:8080$' "${SETTINGS}"; then
  sed -i 's#^BT_ENGINE_URL=http://127\.0\.0\.1:8080$#BT_ENGINE_URL=https://mcpc.taile84e23.ts.net#' "${SETTINGS}"
elif ! grep -q '^BT_ENGINE_URL=' "${SETTINGS}"; then
  printf '%s\n' 'BT_ENGINE_URL=https://mcpc.taile84e23.ts.net' >> "${SETTINGS}"
fi

cat <<OUT
[v5.6] formal BT-engine auth/contract + UI update installed.
[v5.6] backup: ${BACKUP}
[v5.6] Set the engine token in:
  ${SETTINGS}
  BT_ENGINE_TOKEN=<token from the BT-engine owner>
[v5.6] Default public endpoint:
  BT_ENGINE_URL=https://mcpc.taile84e23.ts.net
[v5.6] Lab-network alternative:
  BT_ENGINE_URL=http://192.168.50.125:8090
[v5.6] Qwen and BTGenBot model servers do not need restart; restart the agent only.
OUT
