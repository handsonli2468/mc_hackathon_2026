#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env

stop_pidfile() {
  local name="$1" file="$2"
  if [[ -f "$file" ]]; then
    local pid
    pid=$(cat "$file" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "[stop] ${name} pid=${pid}"
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_pidfile agent "${PID_ROOT}/agent.pid"
stop_pidfile btgenbot "${PID_ROOT}/btgenbot-vllm.pid"
stop_pidfile planner "${PID_ROOT}/planner-vllm.pid"
