#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env

PID_FILE="${PID_ROOT}/agent.pid"
if pid_alive "${PID_FILE}"; then
  pid="$(cat "${PID_FILE}")"
  echo "[agent] stopping PID ${pid}"
  kill "${pid}" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  kill -9 "${pid}" 2>/dev/null || true
else
  echo "[agent] PID file absent/stale; checking uvicorn process"
  pkill -f 'uvicorn agent.app.main:app' 2>/dev/null || true
fi
rm -f "${PID_FILE}"
