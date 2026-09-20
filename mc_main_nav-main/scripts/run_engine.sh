#!/usr/bin/env bash
# Start the BT engine HTTP server.
#   ENGINE_PORT       port to listen on (default 8080)
#   CAMERA_BASE_URL   the camera team's service, e.g. http://192.168.50.125:8080
#                     Set it here rather than with `ros2 param set`, which is lost on restart.
# Extra args go to ros2 run.
set -eo pipefail
source /opt/ros/humble/setup.bash
source "$(dirname "$0")/../ws/install/setup.bash"
args=(-p "http_port:=${ENGINE_PORT:-8080}")
[ -n "${CAMERA_BASE_URL:-}" ] && args+=(-p "camera.base_url:=${CAMERA_BASE_URL}")

exec ros2 run bt_engine bt_engine --ros-args "${args[@]}" "$@"
