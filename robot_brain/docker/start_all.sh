#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "${SCRIPT_DIR}/start_models.sh" all
bash "${SCRIPT_DIR}/start_agent.sh"

echo
echo "Robot Brain is ready."
echo "Frontend/API: http://127.0.0.1:8000/"
echo "Planner:      http://127.0.0.1:8101/v1"
echo "BT compiler:  http://127.0.0.1:8102/v1"
