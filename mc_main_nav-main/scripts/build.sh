#!/usr/bin/env bash
# Build the colcon workspace (run inside the container).
set -eo pipefail
source /opt/ros/humble/setup.bash
cd "$(dirname "$0")/../ws"
colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release "$@"
