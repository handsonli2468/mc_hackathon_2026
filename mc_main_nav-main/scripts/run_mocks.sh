#!/usr/bin/env bash
# Start the mock robot (fake action/service servers). Extra args go to ros2 run,
# e.g. ./scripts/run_mocks.sh -p fail.navigate_to_object:=0.5
set -eo pipefail
source /opt/ros/humble/setup.bash
source "$(dirname "$0")/../ws/install/setup.bash"
# MOCK_DISABLE="navigate_to_object,rotate_in_place,is_at_object" hands those servers to real modules.
args=()
if [ -n "${MOCK_DISABLE:-}" ]; then
  args+=(-p "disable:=[${MOCK_DISABLE}]")
fi
exec ros2 run bt_mocks mock_robot --ros-args "${args[@]}" "$@"
