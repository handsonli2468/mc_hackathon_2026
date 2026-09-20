#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${CONFIG_ROOT}/settings.env"; then
    printf '\n%s=%s\n' "$key" "$value" >> "${CONFIG_ROOT}/settings.env"
  fi
}

append_if_missing BT_ENGINE_SKIP_STARTUP_NODE_SYNC 0
append_if_missing BT_ENGINE_SYNC_MIN_CUSTOM_NODES 1
append_if_missing BT_ENGINE_SEMANTIC_COMPLETE_FOR_HEALTH 1

# Run Python bootstrap once so an existing v6.0.x skill_registry.yaml is migrated
# into the semantic overlay before the first live sync.
PYTHONPATH="${PROJECT_ROOT}" PROJECT_ROOT="${PROJECT_ROOT}" RUNTIME_ROOT="${RUNTIME_ROOT}" \
python3 - <<'PY'
from agent.app.bootstrap import bootstrap_runtime
from agent.app.settings import settings
bootstrap_runtime()
print(f"[v6.1] semantic overlay: {settings.config_root / 'bt_engine_semantic_overlay.yaml'}")
print(f"[v6.1] restart Agent to trigger automatic GET /nodes?builtin=0 synchronization")
PY
