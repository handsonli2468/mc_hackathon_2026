#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env
cd "${PROJECT_ROOT}"

TARGET="${1:-all}"

start_planner() {
  if curl -fsS "http://127.0.0.1:${PLANNER_PORT}/v1/models" >/dev/null 2>&1; then
    echo "[planner] already running on :${PLANNER_PORT}"
    return 0
  fi
  echo "[planner] starting ${PLANNER_MODEL_ID}"
  echo "[planner] log: ${LOG_ROOT}/planner-vllm.log"
  nohup vllm serve "${PLANNER_MODEL_ID}" \
    --served-model-name "${PLANNER_SERVED_NAME}" \
    --host 127.0.0.1 \
    --port "${PLANNER_PORT}" \
    --tensor-parallel-size 1 \
    --max-model-len "${PLANNER_MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${PLANNER_GPU_MEMORY_UTILIZATION}" \
    --reasoning-parser qwen3 \
    > "${LOG_ROOT}/planner-vllm.log" 2>&1 &
  echo $! > "${PID_ROOT}/planner-vllm.pid"
  if ! wait_http "http://127.0.0.1:${PLANNER_PORT}/v1/models" "${MODEL_START_TIMEOUT_SECONDS}" "Qwen planner"; then
    tail -n 120 "${LOG_ROOT}/planner-vllm.log" || true
    return 1
  fi
}

start_compiler() {
  if [[ "${ENABLE_BTGENBOT:-1}" != "1" ]]; then
    echo "[compiler] ENABLE_BTGENBOT=0; skipping BTGenBot-2. Direct baseline remains usable."
    return 0
  fi
  if curl -fsS "http://127.0.0.1:${BT_COMPILER_PORT}/v1/models" >/dev/null 2>&1; then
    echo "[compiler] already running on :${BT_COMPILER_PORT}"
    return 0
  fi

  if ! hf auth whoami >/dev/null 2>&1; then
    cat >&2 <<'EOF'
[compiler] BTGenBot-2 is a gated Hugging Face model.
Before starting it:
  1) accept the model access conditions in your Hugging Face account;
  2) run inside this LAB:
       export HF_HOME=/mlsteam/workspace/agent-runtime/models/huggingface
       hf auth login
Then re-run: bash docker/start_models.sh compiler
You can set ENABLE_BTGENBOT=0 in agent-runtime/config/settings.env to use only the direct baseline.
EOF
    return 2
  fi

  echo "[compiler] starting ${BT_COMPILER_MODEL_ID}"
  echo "[compiler] log: ${LOG_ROOT}/btgenbot-vllm.log"
  nohup vllm serve "${BT_COMPILER_MODEL_ID}" \
    --served-model-name "${BT_COMPILER_SERVED_NAME}" \
    --host 127.0.0.1 \
    --port "${BT_COMPILER_PORT}" \
    --tensor-parallel-size 1 \
    --dtype bfloat16 \
    --max-model-len "${BT_COMPILER_MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${BT_COMPILER_GPU_MEMORY_UTILIZATION}" \
    > "${LOG_ROOT}/btgenbot-vllm.log" 2>&1 &
  echo $! > "${PID_ROOT}/btgenbot-vllm.pid"
  if ! wait_http "http://127.0.0.1:${BT_COMPILER_PORT}/v1/models" "${MODEL_START_TIMEOUT_SECONDS}" "BTGenBot-2"; then
    tail -n 120 "${LOG_ROOT}/btgenbot-vllm.log" || true
    return 1
  fi
}

case "${TARGET}" in
  planner) start_planner ;;
  compiler) start_compiler ;;
  all)
    start_planner
    start_compiler
    ;;
  *) echo "usage: $0 [planner|compiler|all]" >&2; exit 2 ;;
esac
