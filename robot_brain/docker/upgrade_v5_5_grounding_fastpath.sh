#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v5.5-grounding-fastpath-${STAMP}"
mkdir -p "${BACKUP}/prompts"
for f in settings.env skill_registry.yaml; do
  [[ -f "${CONFIG_ROOT}/${f}" ]] && cp "${CONFIG_ROOT}/${f}" "${BACKUP}/${f}"
done
for f in requirement.md planner.md compact_planner.md; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}" ]] && cp "${CONFIG_ROOT}/prompts/${f}" "${BACKUP}/prompts/${f}"
done

# Install the formal registry with v5.5 planner-side grounding annotations. Node IDs,
# kinds, ports and types are unchanged from the team BT-engine contract.
cp "${PROJECT_ROOT}/agent/defaults/skill_registry.yaml" "${CONFIG_ROOT}/skill_registry.yaml"
cp "${PROJECT_ROOT}/agent/defaults/prompts/requirement.md" "${CONFIG_ROOT}/prompts/requirement.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/planner.md" "${CONFIG_ROOT}/prompts/planner.md"
cp "${PROJECT_ROOT}/agent/defaults/prompts/compact_planner.md" "${CONFIG_ROOT}/prompts/compact_planner.md"

SETTINGS="${CONFIG_ROOT}/settings.env"
append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${SETTINGS}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS}"
  fi
}
cat >> "${SETTINGS}" <<'EOT'

# v5.5 grounding-aware fast-first planner
EOT
append_if_missing COMPACT_PLANNER_ENABLED 1
append_if_missing COMPACT_PLANNER_THINKING_BUDGET 1280
append_if_missing COMPACT_PLANNER_RETRY_THINKING_BUDGET 3072
append_if_missing COMPACT_PLANNER_MAX_CAPABILITIES 8

cat <<OUT
[v5.5] grounding-aware fast-first planner installed.
[v5.5] backup: ${BACKUP}
[v5.5] Qwen thinking remains enabled. BTGenBot remains enabled.
[v5.5] Qwen/BTGenBot model servers do not need restart; restart the agent only.
OUT
