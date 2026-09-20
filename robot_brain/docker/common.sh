#!/usr/bin/env bash
set -euo pipefail

export PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
export RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/agent-runtime}"
export CONFIG_ROOT="${RUNTIME_ROOT}/config"
export LOG_ROOT="${RUNTIME_ROOT}/logs"
export PID_ROOT="${RUNTIME_ROOT}/pids"
export STATE_ROOT="${RUNTIME_ROOT}/state"
export MODEL_ROOT="${RUNTIME_ROOT}/models"
export EVAL_ROOT="${RUNTIME_ROOT}/evaluations"
export RAG_KNOWLEDGE_ROOT="${RAG_KNOWLEDGE_ROOT:-${RUNTIME_ROOT}/rag-knowledge}"
export HF_HOME="${HF_HOME:-${MODEL_ROOT}/huggingface}"

bootstrap_runtime() {
  mkdir -p "${CONFIG_ROOT}/prompts" "${LOG_ROOT}" "${PID_ROOT}" "${STATE_ROOT}" "${MODEL_ROOT}" "${EVAL_ROOT}" "${RAG_KNOWLEDGE_ROOT}" "${HF_HOME}"

  local defaults="${PROJECT_ROOT}/agent/defaults"
  [[ -f "${CONFIG_ROOT}/settings.env" ]] || cp "${defaults}/settings.env" "${CONFIG_ROOT}/settings.env"
  local created_skill_registry=0
  if [[ ! -f "${CONFIG_ROOT}/skill_registry.yaml" ]]; then
    cp "${defaults}/skill_registry.yaml" "${CONFIG_ROOT}/skill_registry.yaml"
    created_skill_registry=1
  fi
  [[ -f "${CONFIG_ROOT}/skill_registry.demo.yaml" ]] || cp "${defaults}/skill_registry.demo.yaml" "${CONFIG_ROOT}/skill_registry.demo.yaml"
  [[ -f "${CONFIG_ROOT}/builtin_bt_nodes.yaml" ]] || cp "${defaults}/builtin_bt_nodes.yaml" "${CONFIG_ROOT}/builtin_bt_nodes.yaml"
  [[ -f "${CONFIG_ROOT}/bt_engine_nodes.xml" ]] || cp "${defaults}/bt_engine_nodes.xml" "${CONFIG_ROOT}/bt_engine_nodes.xml"
  # For a fresh runtime, install the curated semantic overlay immediately. For an
  # existing v6.0.x runtime, leave it absent here so Python bootstrap_runtime() can
  # migrate semantic fields from the existing skill_registry.yaml instead of
  # overwriting local knowledge with defaults.
  if [[ ! -f "${CONFIG_ROOT}/bt_engine_semantic_overlay.yaml" && "${created_skill_registry}" == "1" ]]; then
    cp "${defaults}/bt_engine_semantic_overlay.yaml" "${CONFIG_ROOT}/bt_engine_semantic_overlay.yaml"
  fi
  [[ -f "${CONFIG_ROOT}/bt_engine_registry.generated.yaml" ]] || cp "${defaults}/bt_engine_registry.generated.yaml" "${CONFIG_ROOT}/bt_engine_registry.generated.yaml"

  for f in requirement planner critic direct_bt compiler ir_repair bt_repair compact_planner; do
    [[ -f "${CONFIG_ROOT}/prompts/${f}.md" ]] || cp "${defaults}/prompts/${f}.md" "${CONFIG_ROOT}/prompts/${f}.md"
  done

  if [[ -d "${defaults}/rag" ]]; then
    while IFS= read -r -d '' src; do
      rel="${src#${defaults}/rag/}"
      dst="${RAG_KNOWLEDGE_ROOT}/${rel}"
      mkdir -p "$(dirname "${dst}")"
      [[ -f "${dst}" ]] || cp "${src}" "${dst}"
    done < <(find "${defaults}/rag" -type f -print0)
  fi

}

load_runtime_env() {
  bootstrap_runtime
  set -a
  # shellcheck disable=SC1090
  source "${CONFIG_ROOT}/settings.env"
  set +a
  export PROJECT_ROOT RUNTIME_ROOT CONFIG_ROOT LOG_ROOT PID_ROOT STATE_ROOT MODEL_ROOT EVAL_ROOT RAG_KNOWLEDGE_ROOT HF_HOME
}

wait_http() {
  local url="$1"
  local timeout="${2:-900}"
  local label="${3:-service}"
  local start
  start=$(date +%s)
  while true; do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      echo "[ready] ${label}: ${url}"
      return 0
    fi
    if (( $(date +%s) - start > timeout )); then
      echo "[error] timeout waiting for ${label}: ${url}" >&2
      return 1
    fi
    sleep 3
  done
}

pid_alive() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid=$(cat "$file" 2>/dev/null || true)
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}
