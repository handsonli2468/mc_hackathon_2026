#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v5.4-engine-${STAMP}"
mkdir -p "${BACKUP}"
[[ -f "${CONFIG_ROOT}/settings.env" ]] && cp "${CONFIG_ROOT}/settings.env" "${BACKUP}/settings.env"

SETTINGS="${CONFIG_ROOT}/settings.env"
append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${SETTINGS}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS}"
  fi
}

cat >> "${SETTINGS}" <<'EOF'

# v5.4 BT-engine HTTP bridge (added by upgrade_v5_4_engine.sh)
EOF
append_if_missing BT_ENGINE_ENABLED 1
append_if_missing BT_ENGINE_URL http://127.0.0.1:8080
append_if_missing BT_ENGINE_REQUEST_TIMEOUT_SECONDS 5
append_if_missing BT_ENGINE_HEALTH_TIMEOUT_SECONDS 1.5
append_if_missing BT_ENGINE_POLL_INTERVAL_SECONDS 0.3
append_if_missing BT_ENGINE_MAX_POLL_SECONDS 900
append_if_missing BT_ENGINE_SYNC_NODES_ON_START 0
append_if_missing BT_ENGINE_REQUIRED_FOR_HEALTH 0

cat <<OUT
[v5.4] BT-engine HTTP bridge settings installed.
[v5.4] backup: ${BACKUP}
[v5.4] IMPORTANT: if bt_engine is on another machine, edit:
  ${SETTINGS}
  BT_ENGINE_URL=http://<mini-pc-ip>:8080
[v5.4] Qwen and BTGenBot model servers do not need restart.
OUT
