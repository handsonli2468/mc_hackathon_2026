#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env

echo "== Paths =="
echo "PROJECT_ROOT=${PROJECT_ROOT}"
echo "RUNTIME_ROOT=${RUNTIME_ROOT}"
echo "HF_HOME=${HF_HOME}"

echo -e "\n== Processes =="
pgrep -af 'vllm|uvicorn' || true

echo -e "\n== Agent health =="
curl -fsS "http://127.0.0.1:${APP_PORT}/health" | jq . || echo "agent unavailable"

echo -e "\n== Planner models =="
curl -fsS "http://127.0.0.1:${PLANNER_PORT}/v1/models" | jq '.data // .' || echo "planner unavailable"

echo -e "\n== BT compiler models =="
curl -fsS "http://127.0.0.1:${BT_COMPILER_PORT}/v1/models" | jq '.data // .' || echo "compiler unavailable"

echo -e "\n== Runtime disk usage =="
du -sh "${RUNTIME_ROOT}" 2>/dev/null || true

echo -e "\n== GPU =="
if command -v rocm-smi >/dev/null 2>&1; then
  rocm-smi --showmemuse --showuse 2>/dev/null || true
else
  echo "rocm-smi not installed in this image; model server health above is still authoritative for service readiness."
fi
