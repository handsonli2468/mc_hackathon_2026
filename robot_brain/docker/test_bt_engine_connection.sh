#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
load_runtime_env

BASE="${BT_ENGINE_URL:-https://mcpc.taile84e23.ts.net}"
TOKEN="${BT_ENGINE_TOKEN:-}"
AUTH=()
if [[ -n "${TOKEN}" ]]; then
  AUTH=(-H "X-BT-Token: ${TOKEN}")
fi

echo "[bt-engine] URL: ${BASE}"
echo "[bt-engine] token configured: $([[ -n "${TOKEN}" ]] && echo yes || echo no)"
echo "[bt-engine] health (public)"
curl -fsS "${BASE%/}/health" | jq .

echo "[bt-engine] auth probe (/runs)"
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT
CODE=$(curl -sS -o "${TMP}" -w '%{http_code}' "${AUTH[@]}" "${BASE%/}/runs" || true)
cat "${TMP}" | jq . 2>/dev/null || cat "${TMP}"
echo
if [[ "${CODE}" == "401" ]]; then
  echo "[error] BT Engine authentication failed (HTTP 401). Set BT_ENGINE_TOKEN in ${CONFIG_ROOT}/settings.env." >&2
  exit 2
fi
if [[ "${CODE}" -lt 200 || "${CODE}" -ge 300 ]]; then
  echo "[error] /runs returned HTTP ${CODE}." >&2
  exit 3
fi

echo "[bt-engine] custom nodes"
curl -fsS "${AUTH[@]}" "${BASE%/}/nodes?builtin=0" | head -c 8000
echo
