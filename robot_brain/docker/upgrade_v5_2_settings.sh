#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
CFG="${RUNTIME_ROOT:-$ROOT/agent-runtime}/config/settings.env"
DEFAULTS="$ROOT/agent/defaults/settings.env"

mkdir -p "$(dirname "$CFG")"
if [[ ! -f "$CFG" ]]; then
  cp "$DEFAULTS" "$CFG"
  echo "[v5.2] created $CFG from defaults"
  exit 0
fi

backup="${CFG}.pre-v5.2-$(date +%Y%m%d-%H%M%S)"
cp "$CFG" "$backup"
echo "[v5.2] backup: $backup"

# Only lower the simple-task first-pass budget when the runtime still has the
# shipped v5/v5.1 value. Preserve any user-customized value.
if grep -q '^PLANNER_TASK_THINKING_BUDGET=4096$' "$CFG"; then
  sed -i 's/^PLANNER_TASK_THINKING_BUDGET=4096$/PLANNER_TASK_THINKING_BUDGET=3072/' "$CFG"
  echo "[v5.2] optimized PLANNER_TASK_THINKING_BUDGET 4096 -> 3072"
elif grep -q '^PLANNER_TASK_THINKING_BUDGET=' "$CFG"; then
  echo "[v5.2] keep customized $(grep '^PLANNER_TASK_THINKING_BUDGET=' "$CFG" | tail -n1)"
else
  echo 'PLANNER_TASK_THINKING_BUDGET=3072' >> "$CFG"
  echo "[v5.2] added PLANNER_TASK_THINKING_BUDGET=3072"
fi

keys=(
  PLANNER_TASK_RETRY_THINKING_BUDGET
  PLAN_AUTO_HARDEN_SEARCH_RECOVERY
  AUTO_SEARCH_RECOVERY_DEGREES
)
for key in "${keys[@]}"; do
  if grep -qE "^${key}=" "$CFG"; then
    echo "[v5.2] keep existing $key"
    continue
  fi
  line="$(grep -E "^${key}=" "$DEFAULTS" | tail -n1 || true)"
  if [[ -n "$line" ]]; then
    printf '\n%s\n' "$line" >> "$CFG"
    echo "[v5.2] added $line"
  fi
done

echo "[v5.2] settings upgrade complete; Qwen thinking remains enabled. Restart only the FastAPI agent."
