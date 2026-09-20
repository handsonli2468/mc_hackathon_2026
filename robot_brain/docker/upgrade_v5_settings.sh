#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
CFG="${RUNTIME_ROOT:-$ROOT/agent-runtime}/config/settings.env"
DEFAULTS="$ROOT/agent/defaults/settings.env"

mkdir -p "$(dirname "$CFG")"
if [[ ! -f "$CFG" ]]; then
  cp "$DEFAULTS" "$CFG"
  echo "[v5] created $CFG from defaults"
  exit 0
fi

backup="${CFG}.pre-v5-$(date +%Y%m%d-%H%M%S)"
cp "$CFG" "$backup"

echo "[v5] backup: $backup"
keys=(
  PLANNER_FORCE_THINKING
  PLANNER_THINKING_BUDGET_FALLBACK
  PLANNER_REQUIREMENTS_THINKING_BUDGET
  PLANNER_TASK_THINKING_BUDGET
  PLANNER_TASK_THINKING_BUDGET_MEDIUM
  PLANNER_TASK_THINKING_BUDGET_COMPLEX
  PLANNER_CRITIC_THINKING_BUDGET
  PLANNER_REPAIR_THINKING_BUDGET
  PLANNER_DIRECT_THINKING_BUDGET
  PLAN_CRITIC_MODE
)

for key in "${keys[@]}"; do
  if grep -qE "^${key}=" "$CFG"; then
    echo "[v5] keep existing $key"
    continue
  fi
  line="$(grep -E "^${key}=" "$DEFAULTS" | tail -n1 || true)"
  if [[ -n "$line" ]]; then
    printf '\n%s\n' "$line" >> "$CFG"
    echo "[v5] added $line"
  fi
done

echo "[v5] settings upgrade complete; restart only the FastAPI agent to apply them."
