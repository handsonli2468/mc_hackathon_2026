#!/usr/bin/env bash
# One-shot health check of the whole stack: ROS graph, the topics navigation depends on,
# action servers, services, and the engine's HTTP API. Run it from the repo root on any
# machine that runs part of the stack.
#
#   ./scripts/doctor.sh                 # auto-pick a running container to look from
#   ./scripts/doctor.sh nav             # look from a particular compose service
#   BT_ENGINE_URL=http://192.168.50.125:8090 BT_ENGINE_TOKEN=xxx ./scripts/doctor.sh
#
# Every line is OK / WARN / FAIL plus what to do about it. Nothing here changes anything.
set -uo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker/compose.yaml --profile sim --profile robot --profile gui"
ENGINE_URL="${BT_ENGINE_URL:-http://localhost:${ENGINE_PORT:-8080}}"
TOKEN="${BT_ENGINE_TOKEN:-}"
ok=0; warn=0; bad=0
say() { printf '%-6s %-28s %s\n' "$1" "$2" "${3:-}"; }
pass() { ok=$((ok+1)); say OK "$1" "${2:-}"; }
caution() { warn=$((warn+1)); say WARN "$1" "${2:-}"; }
fail() { bad=$((bad+1)); say FAIL "$1" "${2:-}"; }

# ---------------------------------------------------------------- pick a container
SERVICE="${1:-}"
if [ -z "$SERVICE" ]; then
  for s in nav nav-sim engine mocks map; do
    if $COMPOSE ps --status running --services 2>/dev/null | grep -qx "$s"; then SERVICE=$s; break; fi
  done
fi
if [ -z "$SERVICE" ]; then
  echo "No part of the stack is running here. Start something first (docs/deploy.md)."
  exit 1
fi
echo "=== looking from the '$SERVICE' container on $(hostname), ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-59}"
ros() { $COMPOSE exec -T "$SERVICE" bash -c "source /opt/ros/humble/setup.bash; source ws/install/setup.bash 2>/dev/null; $*" 2>/dev/null; }

# ---------------------------------------------------------------- ROS graph
NODES=$(ros "timeout 8 ros2 node list" | sed '/^$/d')
[ -z "$NODES" ] && { fail "ros2 node list" "nothing found - is the container healthy? docker compose logs $SERVICE"; echo; }
DUPES=$(echo "$NODES" | grep -v transform_listener | sort | uniq -d)
if [ -n "$DUPES" ]; then
  fail "duplicate nodes" "$(echo "$DUPES" | tr '\n' ' ')- two stacks on one ROS_DOMAIN_ID; stop one"
else
  pass "no duplicate nodes"
fi
for n in /bt_engine /diff_nav /pose_bridge /bt_navigator /controller_server /planner_server; do
  if echo "$NODES" | grep -qx "$n"; then pass "node $n"; else caution "node $n" "not visible from here"; fi
done
if echo "$NODES" | grep -qx /mock_robot; then
  caution "mock robot running" "fake modules are answering; not the real robot"
fi

# ---------------------------------------------------------------- topics the robot needs
rate_of() {  # topic -> "12.3" or ""
  ros "timeout 6 ros2 topic hz $1 --window 10" | grep -oE 'average rate: [0-9.]+' | tail -1 | grep -oE '[0-9.]+'
}
POSE_HZ=$(rate_of /robot_pose)
if [ -z "$POSE_HZ" ]; then
  fail "/robot_pose" "no robot position! global camera down -> navigation cannot run"
elif (( $(echo "$POSE_HZ < 5" | bc -l) )); then
  caution "/robot_pose ${POSE_HZ} Hz" "slow; Nav2 wants >= 10 Hz"
else
  pass "/robot_pose ${POSE_HZ} Hz"
fi

DET_HZ=$(rate_of /object_detections)
if [ -z "$DET_HZ" ]; then
  caution "/object_detections" "no onboard-camera stream -> TrackObject will fail"
else
  VIS=$(ros "timeout 5 ros2 topic echo --once /object_detections --field visible" | head -1)
  pass "/object_detections ${DET_HZ} Hz" "latest visible=$VIS (rate counts every tracked object)"
fi

MAP=$(ros "timeout 6 ros2 topic echo --once /map --field info.width" | head -1)
if [ -z "$MAP" ]; then
  fail "/map" "no map -> Nav2 cannot plan; is table_map running on the mini PC?"
else
  pass "/map" "${MAP} cells wide"
fi

TF=$(ros "timeout 6 ros2 run tf2_ros tf2_echo map base_link" | grep -c Translation)
if [ "$TF" -gt 0 ]; then pass "TF map -> base_link"; else fail "TF map -> base_link" "pose_bridge not publishing (needs /robot_pose)"; fi

CMD=$(ros "timeout 5 ros2 topic info -v /cmd_vel" | grep -c 'Node name')
if [ "$CMD" -gt 0 ]; then pass "/cmd_vel wired" "$CMD endpoint(s)"; else caution "/cmd_vel" "nobody publishing or subscribing - is the motor driver up?"; fi

# ---------------------------------------------------------------- action servers and services
ACTIONS=$(ros "timeout 8 ros2 action list")
for a in /navigate_to_object /navigate_to_point /track_object /rotate_in_place /navigate_to_pose /spin; do
  if echo "$ACTIONS" | grep -qx "$a"; then pass "action $a"; else fail "action $a" "missing; check diff_nav / Nav2 logs"; fi
done
SERVICES=$(ros "timeout 8 ros2 service list")
for s in /is_at_object /is_object_visible /is_object_held /get_object_pose; do
  if echo "$SERVICES" | grep -qx "$s"; then pass "service $s"; else fail "service $s" "missing; the VLM or diff_nav is down"; fi
done

# ---------------------------------------------------------------- engine HTTP
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "X-BT-Token: $TOKEN")
HEALTH=$(curl -s -m 5 "$ENGINE_URL/health")
if echo "$HEALTH" | grep -q '"ok": true'; then
  pass "engine $ENGINE_URL" "alive"
  CODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$ENGINE_URL/nodes")
  case "$CODE" in
    200) pass "engine /nodes" "$(curl -s -m 5 "${AUTH[@]}" "$ENGINE_URL/nodes?builtin=0" | grep -c 'ID=') robot nodes";;
    401) fail "engine /nodes" "401 - set BT_ENGINE_TOKEN";;
    *)   fail "engine /nodes" "HTTP $CODE";;
  esac
  STATE=$(curl -s -m 5 "${AUTH[@]}" "$ENGINE_URL/status" | grep -oE '"state": "[a-z]+"' | cut -d'"' -f4)
  [ -n "$STATE" ] && say INFO "last run" "$STATE"
else
  fail "engine $ENGINE_URL" "no answer; wrong port (mini PC uses 8090) or not running"
fi

echo
echo "=== $ok ok, $warn warning(s), $bad failure(s)"
[ "$bad" -gt 0 ] && echo "See docs/debugging.md for what each failure means."
exit 0
