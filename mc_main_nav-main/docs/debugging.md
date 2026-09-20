# Debugging the stack

For when something misbehaves on the real robot. Start at the top.

```bash
./scripts/doctor.sh          # on any machine running part of the stack
```
It checks the ROS graph, the topics navigation depends on, every action server and service, and the
engine's HTTP API, and prints OK / WARN / FAIL with a hint per line. On the mini PC, give it the port
and token: `BT_ENGINE_URL=http://localhost:8090 BT_ENGINE_TOKEN=... ./scripts/doctor.sh`.

## "I cannot reach the engine" from outside

Work from the outside in, and **do not test from a machine on the tailnet** — MagicDNS makes the
Funnel URL resolve to the node itself, so it passes while the public endpoint is dead.

```bash
# 1. is it really unreachable publicly?  (--doh-url forces public DNS)
curl -sv --doh-url https://1.1.1.1/dns-query https://mcpc.taile84e23.ts.net/health
```

| What you see | What it means |
|---|---|
| `200` | The service is fine; the caller's URL, network or client is the problem |
| `401` + JSON `missing or wrong token` | Token problem — the API works |
| TLS handshake error / `unexpected eof` | Never a token problem. The funnel is not announcing; see below |
| Connection refused / 502 | The funnel is up but the engine behind it is down — check `docker ps` |

```bash
# 2. what the daemon thinks, versus what it is actually doing
ssh mcpc
~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock funnel status   # can lie: says "on" regardless
tail -20 ~/.tailscale/daemon.log                                          # tells the truth
```
**If the log shows nothing at all** while outside clients get TLS errors, the connections are being
dropped at Tailscale's ingress servers, not here: the node is no longer registered for Funnel. This
follows a default-route flap (`monitor: gateway and self IP changed`, then `PollNetMap: ... use of
closed network connection`), which the mini PC does every ~40 minutes because it is dual-homed.
`handleIngress: got ingress conn for unconfigured ...; rejecting` is the *other* case: the connection
did arrive but no serve config matched. Either way the repair is the same — toggle it:
`funnel --https=443 off`, then `funnel --bg --https=443 http://localhost:8090`, and give it ~15 s.
`./scripts/expose.sh ensure` now does this automatically, within 5 minutes.

Cloudflare quick tunnels (`./scripts/expose.sh cloudflare`) are a useful second opinion: if Cloudflare
works and Funnel does not, the problem is the funnel, not the caller.

## Who is responsible for what

```
LLM ──HTTP──▶ bt_engine ──action/service──▶ diff_nav ──▶ Nav2 ──▶ /cmd_vel ──▶ motor driver
 (tree)        (mini PC)                     (robot Pi)                          (robot)
                  ▲                              ▲   ▲
                  │                              │   └── /object_detections  (onboard camera, vision team)
        table_map │/map                          └────── /robot_pose         (global camera, vision team)
                  └── obstacle UI :8081                   /get_object_pose    (VLM)
```

| Symptom points at | Look here |
|---|---|
| Tree rejected, wrong node names, tree logic | `bt_engine` on the mini PC |
| "no free spot", wrong stop distance, camera approach | `diff_nav` on the Pi |
| Robot wanders, plans silly paths, refuses to move | Nav2 on the Pi + the costmap |
| Robot doesn't move at all, or won't stop | motor driver + `/cmd_vel` |
| Everything fails at once | `/robot_pose` (global camera) or the network |

## Reading a failed run

```bash
BT_ENGINE_URL=... BT_ENGINE_TOKEN=... ./scripts/bt_exec.py status        # last run
curl -s -H "X-BT-Token: $TOKEN" "$BT/status?trace=full" | less           # everything
```
- **`notes`** — the reason each leaf failed, in words. Read this first; it usually names the cause.
- **`last_leaf_failure`** — which action or condition failed last.
- **`trace`** — every status change with timestamps, so you can see how far it got and what retried.
- **`state`** — `failure` (a node failed), `canceled` (something stopped it), `error` (a bug in the engine or a node; `error` holds the text).

Live view of a running tree: **Groot2** connects to port 1667 on the machine running the engine.

## Symptom → cause → fix

### Nothing works; every node fails with "action server not available"
The Pi isn't running, or the two machines can't see each other.
```bash
ssh mcpi 'cd ~/mc_main_nav && docker compose -f docker/compose.yaml --profile robot ps'
./scripts/doctor.sh          # on each machine: do you see the other's nodes?
```
If each machine only sees its own nodes, ROS 2 discovery is blocked by the Wi-Fi: use
`docker/cyclonedds.xml` (see docs/deploy.md). Also check `ROS_DOMAIN_ID` matches (59 everywhere).

### Odd aborts, "unknown goal response", goals that die instantly
**Two stacks on one ROS domain.** `doctor.sh` reports duplicate nodes. Two `bt_engine`s, or the Pi's
`nav` plus a laptop `nav-sim`, both answer the same goals. Stop one. Keep development on
`ROS_DOMAIN_ID=59`, the same domain as the robot, so only one stack may run at a time.

### The robot doesn't move, but everything reports OK
Check in this order:
```bash
ros2 topic echo /cmd_vel                     # is anything being commanded?
ros2 topic info -v /cmd_vel                  # is the motor driver subscribed?
ros2 topic pub -r 10 /cmd_vel geometry_msgs/Twist "{linear: {x: 0.05}}"   # bypass everything
```
Moves with the manual publish but not under Nav2 → the problem is above the driver (Nav2 or `diff_nav`).
Doesn't move even then → the driver, wiring, power or wheels.

### The robot moves and then stops dead
Usually the position feed died: `pose_bridge` stops publishing TF, Nav2 halts, `diff_nav` fails with
"lost the robot pose (global camera)" after 3 s. Check `/robot_pose` rate with `doctor.sh`. The motor
driver's own watchdog (0.5 s without commands) also stops the robot, which is correct behaviour.

### Nav2 says "aborted" with no detail
```bash
ssh mcpi 'cd ~/mc_main_nav && docker compose -f docker/compose.yaml --profile robot logs nav | tail -50'
```
Common causes: the goal is inside an obstacle or its inflation; the robot is already inside inflated
space (it "can't get out"); the map hasn't arrived; TF is stale. `RViz` shows all of this at a glance
(`docker compose --profile gui run --rm rviz` on a machine with a screen).

### "no free spot 5.0 cm from 'cup'"
`diff_nav` tried 16 positions around the object and every one is blocked in the global costmap. Either
an obstacle in the UI is drawn over the object, the object is against the table edge, or
`inflation_radius` (10 cm) is larger than the free space around it. Look at the obstacle UI first.

### Camera approach fails with "never saw the object"
The onboard camera never reported it. Not a bug: `TrackObject` doesn't search. Check the stream:
```bash
ros2 topic echo /object_detections --once      # visible: true/false, x/y in metres
```
- Nothing published → the vision pipeline is down.
- `visible: false` forever → the object is outside the camera's view, occluded, or the pipeline can't
  find it. Turn the robot to face it first (`RotateInPlace`, or `NavigateToPoint` to drive closer).

### Camera approach stops short or overshoots
Check `robot_front_offset` (robot centre to front edge) and the vision team's frame convention: `x`/`y`
must be measured from the **robot's centre**, not the camera. A constant error equal to the camera's
mounting offset means that convention is wrong.

### It stops at the wrong distance from objects
`NavigateToPoint` accuracy is limited by Nav2's goal tolerance (5 mm) plus the VLM's estimate of the
object position. If the object position itself is off, use `TrackObject` for the last stretch, which
ignores the map entirely.

### Obstacles drawn in the UI have no effect
The UI writes `/map`; Nav2's global costmap subscribes with transient-local durability. Check the map is
arriving: `doctor.sh` prints its width. Then confirm in RViz that the costmap has the obstacle. The
global costmap updates at 2 Hz, so allow a second.

### The API answers 401
The engine is running with a token. Send `X-BT-Token: <token>`, or `?token=`. See docs/llm_interface.md.

### A node aborts with "aborted by '/track_object' (server crashed? check its log)"
An empty abort means the Python callback raised. The traceback is in that container's log:
```bash
ssh mcpi 'cd ~/mc_main_nav && docker compose -f docker/compose.yaml --profile robot logs nav | grep -A15 Traceback | tail -25'
```

## Testing one piece at a time

```bash
# a single action, bypassing the engine and the tree
ros2 action send_goal /navigate_to_object robot_interfaces/action/NavigateToObject \
  "{object_name: cup, stop_distance: 0.05}" --feedback
ros2 action send_goal /track_object robot_interfaces/action/TrackObject \
  "{object_name: cup, stop_distance: 0.01}" --feedback
ros2 action send_goal /navigate_to_pose nav2_msgs/action/NavigateToPose \
  "{pose: {header: {frame_id: map}, pose: {position: {x: 0.5, y: 0.3}, orientation: {w: 1.0}}}}"

# a single condition
ros2 service call /is_at_object robot_interfaces/srv/ObjectQuery "{object_name: cup}"

# change behaviour without a rebuild
ros2 param set /diff_nav servo.max_linear 0.03
ros2 param set /diff_nav robot_front_offset 0.06
ros2 param list /diff_nav
```
Put a mock back in place of a broken module: restart `mocks` **without** that name in `MOCK_DISABLE`,
and it answers instead of the real one (docs/deploy.md).

## First time with the real robot

Do these in order; each one is safe on its own. Stop at the first that misbehaves.

1. **Motor driver alone.** `ros2 topic pub -r 10 /cmd_vel ...` as above. Forward is forward, positive
   `angular.z` turns left, and it stops within ~0.5 s when you stop publishing. **If the watchdog doesn't
   work, fix that before anything else** — it's what stops the robot when software crashes.
2. **Global camera.** `ros2 topic hz /robot_pose` ≥ 10 Hz. Push the robot by hand: x/y must follow, and
   yaw must increase when it turns left. Check the origin and axes match the obstacle UI.
3. **The map.** Open the obstacle UI, draw one obstacle, confirm it appears in RViz.
4. **Rotation.** `RotateInPlace angle_deg="90"` through the engine. The robot should turn about 90°, and
   nothing else should move.
5. **A short drive.** `NavigateToPoint` to a point 20 cm away in open space.
6. **Object approach.** `VisualizeObject` then `TrackObject distance="far"`.
7. **Camera approach.** `TrackObject distance="contact"`, with a hand near the robot the first time.
8. **The full tree.** `examples/grab_cup_with_camera.xml`.

Measure and set these before step 5: `robot_radius` and `robot_front_offset` (both currently 5 cm
placeholders) and `max_linear` / `desired_linear_vel` (8 cm/s).

## Stopping the robot in a hurry

```bash
curl -s -XPOST -H "X-BT-Token: $TOKEN" $BT/cancel     # stop the tree, cancel robot goals
ssh mcpi 'cd ~/mc_main_nav && docker compose -f docker/compose.yaml --profile robot stop nav'
```
Stopping `nav` stops the commands, and the motor driver's watchdog stops the wheels. Nothing beats a
hardware switch; add one if the robot can hurt itself.

## Traps we have already hit

| Trap | What it looks like |
|---|---|
| Two stacks on one domain | "unknown goal response", goals abort instantly |
| Restarting a container and testing immediately | "action server not available" for a few seconds |
| Compose env vars not listed under `environment:` | A token or port setting is silently ignored |
| `pkill -f "cloudflared tunnel"` over SSH | Kills the SSH session itself; use `pkill -x` |
| Rewind/restore of files | Docker images are **not** reverted; rebuild after changing the Dockerfile |
| Stale `ws/build` after switching branches | Confusing compile errors about generated interfaces |

## What to collect when asking for help

```bash
./scripts/doctor.sh > /tmp/doctor.txt
curl -s -H "X-BT-Token: $TOKEN" "$BT/status?trace=full" > /tmp/run.json
ssh mcpi 'cd ~/mc_main_nav && docker compose -f docker/compose.yaml --profile robot logs --tail 200 nav' > /tmp/nav.log
```
Those three, plus the XML you sent, are enough to diagnose almost anything here.
