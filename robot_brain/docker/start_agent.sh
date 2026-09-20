#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env
cd "${PROJECT_ROOT}"

if curl -fsS "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
  echo "[agent] already running on :${APP_PORT}"
  exit 0
fi

if ! curl -fsS "http://127.0.0.1:${PLANNER_PORT}/v1/models" >/dev/null 2>&1; then
  echo "[agent] planner is not ready. Run: bash docker/start_models.sh planner" >&2
  exit 1
fi

nohup python3 -m uvicorn agent.app.main:app \
  --app-dir "${PROJECT_ROOT}" \
  --host 0.0.0.0 \
  --port "${APP_PORT}" \
  > "${LOG_ROOT}/agent.log" 2>&1 &
echo $! > "${PID_ROOT}/agent.pid"

echo "[agent] starting FastAPI on :${APP_PORT}"
if ! wait_http "http://127.0.0.1:${APP_PORT}/health" 180 "Robot Brain API"; then
  tail -n 120 "${LOG_ROOT}/agent.log" || true
  exit 1
fi
curl -fsS "http://127.0.0.1:${APP_PORT}/health" | jq . || true
echo "[agent] startup BT-node sync status"
curl -fsS "http://127.0.0.1:${APP_PORT}/api/bt-engine/sync-status" | jq . || true
