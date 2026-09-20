# Working on navigation

How to sit down and change navigation behaviour: what to start, what to edit, how to watch it, how to
test it. For *why* it is built this way see [nav.md](nav.md) and [camera_approach.md](camera_approach.md);
for failures see [debugging.md](debugging.md).

## 1. Start the simulator (on the laptop)

Everything runs against a simulated robot, so you can work with no hardware and without touching the
deployed robot. **Everything uses `ROS_DOMAIN_ID=59`**, including the real robot, so run the simulator only when the robot stack is stopped.

```bash
cd ~/dev/mc_main_nav
./mc sim            # everything below, then waits for Nav2 and runs a health check
./mc sim rviz       # the same, with RViz
```

`./mc help` lists the rest: `run`, `validate`, `nodes`, `test`, `build`, `doctor`, `logs`, `deploy`, `ssh`.

That is the whole setup. It is the long version below with the right environment already set:

```bash
export ROS_DOMAIN_ID=59
export MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object,navigate_to_point,track_object
docker compose -f docker/compose.yaml --profile sim up -d engine mocks map nav-sim
```

Note `USE_RVIZ=true` and `ROS_DOMAIN_ID` are **environment variables, so they go before `docker`**, not
after the service names:
`USE_RVIZ=true docker compose -f docker/compose.yaml --profile sim up -d ... nav-sim`.

| Container | What it gives you |
|---|---|
| `engine` | The BT engine: HTTP API + node palette on http://localhost:8080 |
| `mocks` | Fake VLM and gripper. `MOCK_DISABLE` hands the navigation calls to the real code |
| `map` | The table map and obstacle UI on http://localhost:8081 |
| `nav-sim` | The robot side: fake robot + fake onboard camera + Nav2 + `diff_nav` |

```bash
./mc rviz           # RViz on its own (works against the real robot too)
./mc status         # what is running, plus the health check
./mc logs           # follow the robot side (add a service name for another)
./mc down           # stop everything on this machine
```

To work against the **real robot** instead: same thing without `nav-sim`, with the Pi running
`--profile robot up -d nav` and `ROS_DOMAIN_ID=59`.

## 2. Run something

```bash
./mc run examples/nav_demo.xml               # drive around, one failure on purpose
./mc run examples/grab_cup_with_camera.xml   # Nav2 then camera approach then grasp

# one action, no tree involved
./mc shell nav-sim          # ROS and the workspace are already sourced
  ros2 action send_goal /navigate_to_point robot_interfaces/action/NavigateToPoint \
    "{x: 0.7, y: 0.5, use_yaw: false}" --feedback

# or as a one-liner from outside
./mc shell nav-sim 'ros2 action list'
```

Move the robot or the objects between runs:
```bash
./mc shell nav-sim 'ros2 param set /sim_base teleport "0.10,0.10,0.0"'          # x,y,yaw
./mc shell mocks   'ros2 param set /mock_robot object_positions "[\"cup:0.40:0.20\"]"'
```

## 3. Watch what it is doing

| Tool | Shows | How |
|---|---|---|
| **RViz** | Map, costmap, the planned path, the robot's footprint, the goal | `./mc rviz`; "2D Goal Pose" sends a raw Nav2 goal, bypassing the tree |
| **Obstacle UI** | The table, your obstacles, the robot, Nav2's path | http://localhost:8081 |
| **Groot2** | The behavior tree ticking live, node by node | `./Groot2-v1.9.0-x86_64.AppImage`, connect to `localhost:1667` |
| **Run status** | Which leaf failed and why | `./scripts/bt_exec.py status`, or `?trace=full` for everything |
| **doctor** | The whole chain at a glance | `./mc doctor` |
| **Logs** | The real error text | `./mc logs` (or `./mc logs engine`) |

Useful one-liners — `./mc shell nav-sim` first, or prefix each with `./mc shell nav-sim '...'`:
```bash
ros2 topic echo /cmd_vel                  # what the wheels are being told
ros2 topic echo /object_detections --once # what the onboard camera reports
ros2 run tf2_ros tf2_echo map base_link   # where the robot thinks it is
ros2 topic hz /robot_pose                 # is the global camera alive
```

## 4. Where the code is

| To change | Edit | Then |
|---|---|---|
| How the robot picks a spot next to an object, timeouts, failure messages | `ws/src/diff_nav/diff_nav/nav_server.py` | restart `nav-sim` |
| The camera-approach control law | `ws/src/diff_nav/diff_nav/visual_servo.py` (pure maths, unit-tested) | restart `nav-sim` |
| Approach-spot geometry, grid lookups | `ws/src/diff_nav/diff_nav/geometry.py` | restart `nav-sim` |
| Speeds, tolerances, robot size, planner and controller | `ws/src/diff_nav/config/nav2_table.yaml` | restart `nav-sim` |
| Stop distances, servo gains, timeouts | `ws/src/diff_nav/config/nav.yaml` | restart `nav-sim`, or `ros2 param set` live |
| Nav2's own recovery behaviour | `ws/src/diff_nav/config/nav2_bt_table.xml` | restart `nav-sim` |
| Camera pose → TF and `/odom` | `ws/src/diff_nav/diff_nav/pose_bridge.py` | restart `nav-sim` |
| The fake robot / fake camera | `sim_base.py`, `sim_camera.py` | restart `nav-sim` |
| The map, the table size, the obstacle UI | `ws/src/table_map/` | restart `map` |
| The node palette the LLM sees | `ws/src/bt_engine/src/register_nodes.cpp` (C++) | rebuild, restart `engine` |
| Fake behaviour of a module | `ws/src/bt_mocks/bt_mocks/mock_robot.py` | restart `mocks` |

**Python changes need no build**, just a restart:
```bash
docker compose -f docker/compose.yaml --profile sim restart nav-sim
```
(`ROS_DOMAIN_ID=59` must be exported in that shell, or use `./scripts/sim.sh up` again.)
**C++ changes (the engine) need a build:**
```bash
./mc build bt_engine
docker compose -f docker/compose.yaml --profile sim restart engine
```
Changing a `.action`, `.srv` or `.msg` means rebuilding everything: `./mc build`, then restart every
container that uses it.

Wait a few seconds after a restart before testing: a container that is still starting looks exactly like
a missing action server.

## 5. Test

```bash
./mc test unit      # control law, geometry, map rendering. Fast, no robot needed.
./mc test sim       # end-to-end against the simulator (~3 min for all 16)
./mc test           # both

./scripts/test_nav_sim.py camera      # only tests whose name contains "camera"
./scripts/test_nav_sim.py point detour
```
Add a test for whatever you change: unit tests for maths in `ws/src/diff_nav/test/`, behaviour tests in
`scripts/test_nav_sim.py` (they drive the real engine and Nav2, and can teleport the robot and move objects).

## 6. Recipes

**Make the robot slower** (`config/nav2_table.yaml`): `desired_linear_vel`, and `velocity_smoother`'s
`max_velocity`. Live try-out: `ros2 param set /controller_server FollowPath.desired_linear_vel 0.04`.

**Change how close it stops**: the named distances live in the engine and can be re-tuned **while it
runs** — `ros2 param set /bt_engine nav_distance.contact 0.02` (metres) applies to the next goal. To
make it permanent, edit the defaults in `bt_engine/include/bt_engine/ros_context.hpp` and rebuild.

**Tune the search timings**: they are engine parameters, not tree ports, and apply while it runs.
`ros2 param set /bt_engine camera_poll_ms 250` (how often `IsObjectFound` really asks the camera),
`object_pose_timeout_ms` (how long `NavigateToDetectedObject` waits for a first pose, 15000) and
`replan_min_interval_ms` (the floor between re-sent goals when the pose moves, 400). Each must be
positive; the engine refuses anything else. Defaults are in `bt_engine/include/bt_engine/ros_context.hpp`.
The VLM runs on an edge device, so these and `camera.max_age_s` (8 s: how old a sighting may be and
still count as FOUND) are sized for a slow detector. Measure the gap between `client_stamp` values in
`/api/status` once detections flow, and set `camera.max_age_s` to about three times it.

**Make the camera approach gentler** (`config/nav.yaml`, under `servo:`): lower `k_linear`/`max_linear`,
or raise `distance_tolerance`. Live: `ros2 param set /diff_nav servo.max_linear 0.03`.

**Give the robot a hard time**: draw obstacles in the UI, or
`ros2 param set /sim_base velocity_noise 0.15` (sloppy wheels),
`ros2 param set /sim_base pose_noise 0.005` (jittery global camera),
`ros2 param set /sim_camera bearing_noise 0.05` (noisy detections),
`ros2 param set /sim_camera min_range 0.15` (camera goes blind sooner),
`ros2 param set /mock_robot fail.grasp_object 1.0` (grasping always fails).

**Add a navigation node** (say `BackUp`), end to end:
1. `ws/src/robot_interfaces/action/BackUp.action` with `bool success` + `string message` in the result,
   and list it in that package's `CMakeLists.txt`.
2. Serve it in `nav_server.py` (an `ActionServer` plus an `_execute_...` method).
3. Register it in `bt_engine/src/register_nodes.cpp` so the LLM may use it.
4. Add fake behaviour in `bt_mocks/mock_robot.py` so trees work without the robot.
5. `./mc build`, restart, then check it appears at http://localhost:8080 and in
   `ros2 action list`.
6. Add a test to `scripts/test_nav_sim.py`.

## 7. Before touching the real robot

Read the bring-up order in [debugging.md](debugging.md#first-time-with-the-real-robot). The short version:
the motor driver's watchdog first, then the global camera, then the map, then rotate, then a short drive.
Set `robot_radius` and `robot_front_offset` (both 5 cm placeholders) from the actual robot first.
