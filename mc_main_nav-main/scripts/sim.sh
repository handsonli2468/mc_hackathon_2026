#!/usr/bin/env bash
# Start / stop the navigation simulator on this machine, with the right environment.
#
#   ./scripts/sim.sh up          # engine + mocks + map + simulated robot (Nav2, camera)
#   ./scripts/sim.sh up rviz     # ... and RViz
#   ./scripts/sim.sh rviz        # RViz only (works against a robot too)
#   ./scripts/sim.sh status      # what is running + a health check
#   ./scripts/sim.sh logs [svc]  # follow logs (default: the robot side)
#   ./scripts/sim.sh down        # stop everything here
#
# Uses ROS_DOMAIN_ID=59, the one domain every machine in this project shares.
set -eo pipefail
cd "$(dirname "$0")/.."

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-59}"
# Hand these calls to the real navigation code; the mock still fakes vision and the gripper.
export MOCK_DISABLE="${MOCK_DISABLE:-navigate_to_object,rotate_in_place,is_at_object,navigate_to_point,track_object,set_gripper}"
COMPOSE=(docker compose -f docker/compose.yaml --profile sim --profile gui)

case "${1:-up}" in
  up)
    [ "${2:-}" = "rviz" ] && export USE_RVIZ=true
    "${COMPOSE[@]}" up -d engine mocks map nav-sim
    echo
    echo "ROS_DOMAIN_ID=$ROS_DOMAIN_ID (the same domain as the real robot: stop one stack before starting the other)"
    echo "node palette : http://localhost:${ENGINE_PORT:-8080}"
    echo "obstacle UI  : http://localhost:8081"
    echo "Groot2       : ./Groot2-v1.9.0-x86_64.AppImage  -> connect to localhost:1667"
    echo
    echo "waiting for Nav2 to come up..."
    for _ in $(seq 1 40); do
      if curl -sf "http://localhost:${ENGINE_PORT:-8080}/health" > /dev/null 2>&1; then break; fi
      sleep 1
    done
    sleep 12   # Nav2's lifecycle nodes take a few seconds after the engine answers
    ./scripts/doctor.sh || true
    echo
    echo "try:  ./scripts/bt_exec.py run examples/grab_cup_with_camera.xml"
    ;;
  rviz)
    exec "${COMPOSE[@]}" run --rm rviz
    ;;
  status)
    "${COMPOSE[@]}" ps --format '{{.Service}}\t{{.Status}}'
    echo
    ./scripts/doctor.sh || true
    ;;
  logs)
    exec "${COMPOSE[@]}" logs -f "${2:-nav-sim}"
    ;;
  down)
    "${COMPOSE[@]}" down
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
