#!/usr/bin/env bash
# Start the table map + obstacle web UI (default http://localhost:8081/). Extra args go to ros2 launch,
# e.g. ./scripts/run_map.sh width:=1.5 height:=0.9
set -eo pipefail
source /opt/ros/humble/setup.bash
source "$(dirname "$0")/../ws/install/setup.bash"
exec ros2 launch table_map map.launch.py "$@"
