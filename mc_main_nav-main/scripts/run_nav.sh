#!/usr/bin/env bash
# Start navigation.
#   ./scripts/run_nav.sh sim   [launch args]   # simulated robot + Nav2 + diff_nav (any PC)
#   ./scripts/run_nav.sh robot [launch args]   # pose_bridge + Nav2 + diff_nav (robot Pi)
set -eo pipefail
source /opt/ros/humble/setup.bash
source "$(dirname "$0")/../ws/install/setup.bash"
mode="${1:-sim}"
shift || true
case "$mode" in
  sim) exec ros2 launch diff_nav sim.launch.py "$@" ;;
  robot) exec ros2 launch diff_nav nav.launch.py "$@" ;;
  *) echo "usage: $0 sim|robot [launch args]" >&2; exit 1 ;;
esac
