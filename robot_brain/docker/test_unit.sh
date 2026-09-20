#!/usr/bin/env bash
set -euo pipefail
cd /mlsteam/workspace
python3 -m pytest -q agent/tests
