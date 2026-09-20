#!/usr/bin/env bash
set -euo pipefail
cd /mlsteam/workspace
RUNS="${1:-3}"
shift || true
python3 agent/tests/run_timing_profile.py --runs "$RUNS" "$@"
