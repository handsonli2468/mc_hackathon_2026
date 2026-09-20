# Navigation (Nav2 on a table-top robot)

The robot is a small differential-drive robot on a meeting-room table. **Nav2 (ROS 2 Humble)** plans
around obstacles we place in advance by clicking on a map in a web UI. The robot's only position
source is the **global camera** (ArUco). There is no lidar and no wheel odometry.

## Pieces

```
                 mini PC                                          robot Pi
┌─────────────────────────────────────┐          ┌──────────────────────────────────────────────┐
│ bt_engine  ──NavigateToObject(cup, ─┼──────────▶ diff_nav nav_server                           │
│              distance="near")       │          │   asks /get_object_pose, picks a free spot   │
│                                     │          │   at the stop distance, facing the object    │
│ table_map  ── /map (5 mm grid) ─────┼──────────▶   └─▶ Nav2 NavigateToPose / Spin              │
│  web UI :8081 (click obstacles)     │          │        (planner, RPP controller, costmaps)   │
│                                     │          │            └─▶ /cmd_vel ─▶ motor driver      │
└─────────────────────────────────────┘          │ pose_bridge: /robot_pose ─▶ TF + /odom       │
 global camera ── /robot_pose, /table_map ───────▶                                              │
 VLM ── /get_object_pose                         └──────────────────────────────────────────────┘
```

| Package / node | Runs on | Does |
|---|---|---|
| `table_map/map_node` | mini PC | Builds `/map`: the camera's `/table_map` if one is published (otherwise a table size from config), plus a 1 cm "don't fall off" edge, plus obstacles from the web UI. Re-publishes on every change (latched). Serves the UI on port **8081**. Obstacles are saved to `data/obstacles.json`. |
| `diff_nav/pose_bridge` | Pi | Global-camera `/robot_pose` (PoseStamped, map frame) → TF `map→odom` (identity) and `odom→base_link`, plus `/odom` with speed estimated from the camera. It stops publishing if the camera stops, so Nav2 halts. |
| Nav2 (`nav2_bringup/navigation_launch.py`) | Pi | NavFn A* planner, Regulated Pure Pursuit controller, static-layer costmaps, Spin/BackUp behaviours. Settings: `diff_nav/config/nav2_table.yaml`. |
| `diff_nav/nav_server` | Pi | Serves the BT interfaces (below). |
| `diff_nav/sim_base` | PC (simulator) | Pretend robot and camera: `/cmd_vel` in, `/robot_pose` out. |

## BT interfaces

| BT node | Server | Behaviour |
|---|---|---|
| `/navigate_to_object`&nbsp;(no BT node since C6) |  | Asks the VLM where the object is. Tries up to 16 spots around it, starting on the robot's side, and takes the first that is free in Nav2's global costmap. Sends Nav2 there, facing the object. |
| `RotateInPlace angle_deg` | `/rotate_in_place` | Nav2 Spin by a relative angle. |
| `/is_at_object`&nbsp;(no BT node since C9) |  | True if the robot's front is within the last stop distance used for that object + 2 cm. |

`distance` is a **named setting**, like an enum (the engine rejects anything else before running):

| Name | Gap from robot front to object centre |
|---|---|
| `contact` | 1 cm |
| `near` (default) | 5 cm |
| `far` | 10 cm |

The names are the contract with the LLM. The metre values are engine settings (`nav_distance.<name>`).
`NavigateToObject.action` carries the metres (`stop_distance`).

Failure reasons that reach the LLM (`notes`):
- `no free spot 5.0 cm from 'cup' (blocked by obstacles or the table edge)`
- `'bottle' has not been located` / `no position for 'bottle'` (from the VLM)
- `lost the robot pose (global camera) while moving`: the camera was gone for more than 3 s. Nav2 is cancelled and the robot stops.
- `navigating to 'cup': Nav2 'navigate_to_pose' aborted` / `timed out after 90s`

## Obstacle UI

Open `http://<mini-pc>:8081/`.
- **Rectangle / Circle:** drag on the table to draw.
- **Select:** click a shape, then press Delete.
- Every change goes to Nav2 straight away (within about 0.5 s).
- The page also shows the robot (green) and Nav2's planned path (purple).
- API: `GET /api/state[?grid=1]`, `PUT /api/obstacles` with `[{"type":"rect","x","y","w","h"} | {"type":"circle","x","y","r"}]` (metres, map frame).

**Map from the global camera:** publish a `nav_msgs/OccupancyGrid` on `/table_map` (latched / transient-local is best). Its size and origin become the map's, it is resampled to 5 mm, and cells ≥ 50 count as walls. Until it arrives, the map is `width` × `height` from `table_map/launch/map.launch.py` (placeholder 1.2 × 0.8 m, origin at the corner).

## Running

```bash
# Simulator on a PC (engine + mock + map UI + fake robot + Nav2), with RViz:
MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object USE_RVIZ=true \
  docker compose -f docker/compose.yaml --profile sim up -d engine mocks map nav-sim
# or RViz on its own (also works to watch the real robot on the same ROS_DOMAIN_ID):
docker compose -f docker/compose.yaml --profile gui run --rm rviz

./scripts/test_nav_sim.py          # 9 end-to-end tests (~2 min)
./scripts/test_nav_sim.py detour   # just one

# Mini PC: map + UI
docker compose -f docker/compose.yaml up -d map
# Robot Pi: pose_bridge + Nav2 + diff_nav
docker compose -f docker/compose.yaml --profile robot up -d nav
```

In RViz, "2D Goal Pose" (or the Nav2 goal tool) sends a raw Nav2 goal that bypasses the BT engine. That's handy for tuning.
If RViz can't open a window, check that `DISPLAY` and `XAUTHORITY` are set in the shell running `docker compose`.

## Settings to measure on the real robot

| Setting | File | Placeholder |
|---|---|---|
| `robot_radius` (both costmaps) | `nav2_table.yaml` | 0.05 m |
| `robot_front_offset` | `nav.yaml` | 0.05 m (robot centre → front edge) |
| Speeds: `desired_linear_vel`, velocity smoother limits | `nav2_table.yaml` | 0.08 m/s, 1.0 rad/s |
| `inflation_radius` / `cost_scaling_factor` | `nav2_table.yaml` | 0.10 m / 30 |
| Table size (until the camera sends `/table_map`) | `map.launch.py` args | 1.2 × 0.8 m |

The motor driver must take `/cmd_vel` and **stop by itself if commands stop for about 0.5 s**.

## Tested (simulator, x86, 2026-09-18)

| Test | Result |
|---|---|
| Unit: approach-spot geometry, grid lookup, map rendering, camera map resampling | ✅ 12/12 |
| `distance` far / near / contact | ✅ gap 10.4 / 5.4 / 1.4 cm, facing within 0.06 rad |
| Wall between robot and cup | ✅ went around it; closest the robot centre came to the wall was 10.3 cm (robot radius 5 cm) |
| Obstacle on the direct approach side | ✅ stopped on another side of the cup |
| Cup surrounded by an obstacle | ✅ fails with "no free spot" |
| Unknown object | ✅ fails with the VLM's reason |
| `RotateInPlace 90` | ✅ turned 92° |
| Cancel mid-drive (simulator watchdog off) | ✅ 8 cm/s → 0 |
| Camera lost mid-drive (simulator watchdog off) | ✅ failed after 3.3 s, robot stopped |
| Unknown `distance` name | ✅ 422 before running |

**Not tested:** real hardware, and Nav2 running on the Pi (arm64). The simulator uses an ideal robot with a perfect camera.
The obstacle UI was exercised through its API; I haven't clicked through it in a browser.

## Known limits

- An object is a point. If it's also drawn as an obstacle, stopping at `contact` is impossible (inflation blocks it).
- Only obstacles known in advance: nothing detects new ones. Dynamic obstacles would need a sensor layer.
- Nav2's recovery tree (`nav2_bt_table.xml`) is Humble's default with BackUp shortened to 2 cm. Spin recovery is still allowed.
