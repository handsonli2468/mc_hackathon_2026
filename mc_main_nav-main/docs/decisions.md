# Decisions log

What we decided and why. Add new decisions at the bottom of the relevant section.
Don't delete decisions that get replaced: mark them ~~struck through~~ and link to the change log.
Status: ✅ decided · 🟡 assumed (not explicitly confirmed) · ❓ open

## System picture

```
User ──▶ LLM agent ──HTTP :8080──▶ bt_engine (x86 mini PC, Docker)
                                       │  ROS 2 actions / services (DDS, ROS_DOMAIN_ID=59)
             ┌──────────────┬──────────┼──────────────┬──────────────┐
             ▼              ▼          ▼              ▼              ▼
        VLM + SAM2     navigation   tracking       gripper      (bt_mocks stands in
          (PC)          (robot)     (robot)                     for any module not
                                                                ready yet)
```

### What runs where

| Machine | Runs | Source |
|---|---|---|
| **x86 mini PC** (Docker) | **bt_engine** (HTTP API + tree execution) | ✅ C1 (replaces the Pi from Q6/Q7) |
| Robot (Raspberry Pi) | **Nav2 + diff_nav + pose_bridge** (see [nav.md](nav.md)) + tracking | ✅ nav on the Pi, from the user; tracking 🟡 from the sketch |
| **x86 mini PC** | **table_map** (map + obstacle web UI, port 8081) | ✅ N13 |
| PC | LLM agent, VLM + SAM2 | 🟡 from the sketch ("vlm + SAM2 (PC)") |
| Anywhere | bt_mocks (fake robot) | ✅ Q1 |

## Round 1: fundamentals

| # | Question | Decision | Why |
|---|---|---|---|
| Q1 | What does "environment" include? | ✅ **BT runner + mock action servers** (no simulator) | Other modules arrive late, and mocks let the LLM → XML → run loop be tested from day one. |
| Q2 | Engine / language | ✅ **BehaviorTree.CPP (C++)** | LLM already writes BT.CPP-style XML; Groot2; already in the team Dockerfile. |
| Q2b | BT.CPP version | 🟡 **v4** (4.10.0 from apt) | Nav2 Humble's BT plugins are v3, but we only need Nav2's *action server*, not its BT nodes. |
| Q3 | How leaves reach modules | ✅ **ROS 2**: actions for long tasks, services for yes/no checks | Machines talk over DDS; one transport. Modules that aren't on ROS get a thin wrapper. |
| Q4 | How XML comes in / what goes back | ✅ **HTTP endpoint**, returns status + failed node + reasons + trace | Status and failure reasons go back to the LLM so it can replan. |
| Q5 | Protecting against bad LLM XML | ✅ **Fixed node palette + validation before ticking** | Unknown node / bad port / missing port / bad structure → 422, nothing runs. |
| Q6 | Where the engine runs; concurrency | ~~Pi~~ → **x86 mini PC** (see C1); ✅ **one tree at a time, new tree preempts** the old one | Lets you abort a bad plan during the demo; halting cancels the ROS goals. |

## Round 2: shape of the system

| # | Question | Decision | Why |
|---|---|---|---|
| Q7 | Engine host hardware / OS | ~~Pi 5, Ubuntu 22/24~~ → **x86 mini PC** (see C1); ✅ **Docker** | Image is `ros:humble-ros-base-jammy`, works on any host OS. |
| Q8 | HTTP style | ✅ **Async**: `POST /execute` → `run_id`, poll `/status`, `POST /cancel` | Long tasks don't hold HTTP open; live progress. Server is in-process (cpp-httplib), one binary. |
| Q9 | Node palette | ✅ **The flow chart's nodes are the v0 contract**; more will be added | Mock anything the hardware doesn't have yet. |
| Q10 | Data between nodes | ✅ **By object name** (`object_name="cup"`); modules keep their own state | Simple XML, fewer LLM wiring errors. Blackboard ports can be added later if needed. |
| Q11 | Who owns the interfaces | ✅ **Us**: `robot_interfaces` package | We're the integration point; teammates implement against it. |

## Decisions made while building

| Decision | Why |
|---|---|
| ~~Own `RecoveryNode` (Nav2 semantics, exactly 2 children)~~ → removed in C9 | Nav2's version is v3-only. Built-in `Fallback` / `RetryUntilSuccessful` cover what the trees actually did. |
| Every action result has `bool success` + `string message` | `message` becomes the failure reason sent to the LLM (`notes`). |
| Conditions share one `ObjectQuery.srv` on different service names | Fewer interface files. |
| ~~Open/CloseGripper → one `/set_gripper` action (`bool open`)~~ → a single `SetGripper position="0..100"` node (see C5) | One server *and* one node for the gripper. |
| `RotateInPlace` takes `angle_deg` (default 45) | Chart had no parameter; degrees are easier for the LLM. |
| Mock keeps a tiny world model (must see before nav, gripper open before grasp) | Lets trees fail in realistic ways. |
| Validation also checks required ports (~~and RecoveryNode child count~~, dropped with the node in C9) | BT.CPP alone only catches these at tick time. |
| Groot2 publisher on port 1667 | Live tree view for debugging/demo. |
| Dropped `--platform=$BUILDPLATFORM` from the team Dockerfile | It makes cross-builds produce x86 images. The image still builds on arm64 if the engine ever moves back. |
| Compose project name `mc-main-nav` | Default name `docker` clashed with other containers on the dev PC. |

## Navigation (`diff_nav`): decided by Claude while you slept, please review

| # | Decision | Why |
|---|---|---|
| N1 | ~~Own lightweight controller, not Nav2~~ → ✅ **Nav2** (see C3) | Table will have obstacles at known positions; Nav2 plans around them. |
| N2 | 🟡 **Python (rclpy)** for our nav code (diff_nav, table_map) | Nothing to compile on the Pi; teammates can tweak it quickly. Nav2 itself is C++ from apt. |
| N3 | 🟡 **Nav asks the VLM for object positions** via new `/get_object_pose` (`GetObjectPose.srv`, map frame) | Keeps Q10 (the BT passes only `object_name`). Nav re-asks every 1 s while driving. |
| N4 | 🟡 **Interfaces to the rest of the robot: `/cmd_vel` out, pose topic in** (Odometry / PoseStamped / PoseWithCovarianceStamped, configurable) | Standard ROS; works with any motor driver or localization. |
| N5 | ~~0.35 m short of the object~~ → ✅ **named `distance` on `NavigateToObject`: contact 1 cm / near 5 cm / far 10 cm** (robot front → object centre) | Table scale; the tree chooses, like an enum. Names are the LLM contract, metres are engine settings. |
| N6 | 🟡 diff_nav serves `/navigate_to_object`, `/rotate_in_place`, `/is_at_object` | Everything pose-related lives in one place. |
| N7 | 🟡 Safety: stop on cancel/preempt/timeout; stop when the pose is stale (0.5 s), fail after 3 s; the motor driver must have its own 0.5 s watchdog | A crashed nav can't send a stop command. |
| N8 | 🟡 One motion goal at a time; a new goal preempts the old one | Same idea as Q6. |
| N9 | 🟡 Kinematic `sim_base` + mock `disable` list for testing | Lets the whole stack run without hardware; swap modules one at a time. |
| N10 | ✅ (bug fix) Engine fails an action leaf if its server never answers (3 s) or disappears mid-goal | Found while testing: a dead module made the tree run forever. |
| N11 | ✅ **Position only from the global camera (ArUco)**, no wheel odometry | User (Q1 of the Nav2 round). `pose_bridge` turns it into TF + `/odom`. |
| N12 | ✅ **Map: 5 mm cells; from the global camera's `/table_map` topic when available**, else a table size from config (placeholder 1.2 × 0.8 m) | User: map size not known yet; the camera can send it. |
| N13 | ✅ **Obstacles drawn in a web UI and sent to Nav2 live**, saved to a file; UI on the mini PC | User (Q3b). |
| N14 | 🟡 Nav2 setup: no AMCL/map_server; static-layer costmaps; NavFn A*; **Regulated Pure Pursuit** controller; 5 cm robot radius; 8 cm/s; goal tolerance 5 mm / 0.1 rad | Nothing to localize against or sense with; RPP is simple to tune for a diff-drive robot. |
| N15 | 🟡 `NavigateToObject` picks the first free spot of 16 around the object (robot's side first), using Nav2's global costmap | Obstacles may block the direct approach. |
| N16 | 🟡 Own copy of Nav2's recovery tree with BackUp 2 cm (default 30 cm) | Don't reverse off the table. |
| N17 | 🟡 Camera lost > 3 s while moving → cancel Nav2 and fail | Nav2 alone just waits (90 s in tests). |
| N18 | 🟡 RViz via X11 from Docker (like DIT-ROBOTICS/Eurobot-2026-Navigation2): `use_rviz` on the sim, plus an `rviz` compose service | User asked to launch sim/RViz like the team repo. |

## Change log

| # | Date | Change | Impact |
|---|---|---|---|
| C1 | 2026-09-17 | Engine host: **Raspberry Pi 5 → x86 mini PC** | None for the code: everything was built and tested on x86 already. No Pi build needed. |
| C2 | 2026-09-17 | Added `GetObjectPose.srv` to the contract (owner: VLM) | VLM teammate must serve `/get_object_pose` in the same frame as the robot pose. |
| C3 | 2026-09-18 | Navigation: **hand-written controller → Nav2** (hand-written controller deleted). Obstacles are set in advance and placed by clicking on a map in a **web UI**. Scale is a meeting-room table (cm). | diff_nav's controller gets replaced by Nav2; the BT side (`/navigate_to_object`, named distances) stays. |
| C4 | 2026-09-19 | The robot **has** a gripper. `/set_gripper` is served by `gripper_server` on the Pi, which publishes a single `std_msgs/Int16` on **`/gripper`** (the type the MCU subscribes to) carrying a **0 (open) .. 100 (closed) position chosen by the tree**, then waits 2 s for the jaws. | Fire-and-forget: no hardware feedback, so the wait is the only guarantee. closing to 100 alone counts as a grasp (node names superseded by C5); `SetGripper position="N"` gives the LLM a partial grip; `GraspObject` stays reserved for a real manipulation module. |
| C5 | 2026-09-19 | ✅ **`OpenGripper` and `CloseGripper` removed**; `SetGripper position="0..100"` is the only gripper node. | The two presets were sugar over the same `/set_gripper` goal. The palette drops to 11 robot nodes; trees say `SetGripper position="0"` / `position="100"` instead. `GripperPresetLeaf` is gone from `ros_nodes.hpp`. |
| C6 | 2026-09-19 | **`GraspObject`, `IsObjectHeld` and `NavigateToObject` removed from the BT palette.** Picking up is `SetGripper position="0"` → `TrackObject` → `SetGripper position="100"`. | Nothing served `/grasp_object` for real, and `IsObjectHeld` was inferred from the last gripper command rather than a sensor, so it could only ever confirm what the tree had just done. `NavigateToObject` went too because `TrackObject` already drives to an object, on what the camera sees rather than the map. The actions, services and their `diff_nav`/mock servers all stay, so any of them can come back. **8 robot nodes now.** Cost to accept: `TrackObject` ignores the costmap, so `NavigateToPoint` is the only obstacle-avoiding way to cross the table. |
| C7 | 2026-09-19 | **Search nodes, one job each**: `VisualizeObject` (action, one HTTP POST to start a search), `IsObjectFound` (condition, cached HTTP poll), `Patrol` (action, clockwise rectangle for ever), `NavigateToDetectedObject` (action, drives to the published pose with a standoff). The control flow is a plain `ReactiveFallback`, not a custom decorator. Field is **1.8 x 1.2 m**. | The camera team's service is now a **hard dependency for every object-based tree**: nothing else tells ROS where an object is, so `/get_object_pose` stays empty without it. Set `camera.base_url` or these nodes fail with a clear message. |
| C8 | 2026-09-19 | The camera publishes the object pose on **`/object_goal_pose`**, not `/goal_pose`. | Nav2's `bt_navigator` subscribes to `/goal_pose` and would drive there by itself the moment a pose arrived, behind the tree's back — two controllers, one robot. |
| C9 | 2026-09-19 | ✅ **`IsObjectVisible`, `IsAtObject` and our custom `RecoveryNode` removed from the palette.** | The two conditions were the last users of `ObjectQuery.srv` in the engine; `IsObjectFound` (C7) is how a tree asks whether the camera has the object now. `RecoveryNode` went with them, so recovery is expressed with BT.CPP built-ins (`Fallback`, `RetryUntilSuccessful`). `recovery_node.hpp` and `ObjectQueryCondition` are deleted, along with the child-count check in `checkTreeStructure`. **8 robot nodes now.** The `/is_at_object` and `/is_object_visible` services and their `diff_nav`/mock servers stay, so the conditions can come back. Unaffected: `diff_nav/config/nav2_bt_table.xml`, which uses **Nav2's own** RecoveryNode inside Nav2's v3 engine. |
| C10 | 2026-09-19 | **`TrackObject` dropped** (temporarily, user's call). **7 robot nodes.** | Nothing publishes `/object_detections` outside the simulator: the camera team's service is HTTP plus a map-frame pose, so the onboard-camera servo had no input on the real robot. The approach now ends at `NavigateToDetectedObject`, which parks at `standoff` (15 cm). `/track_object`, `diff_nav`'s servo and `visual_servo.py` all stay, so it returns the day `/object_detections` has a publisher. |
| C9 | 2026-09-19 | **`IsObjectVisible`, `IsAtObject` and `RecoveryNode` dropped** from the palette (user's call, after a refactor had already removed them by accident). **8 robot nodes.** | Whether the object is visible is now the camera service's business (`IsObjectFound`), and arrival is judged inside `NavigateToDetectedObject`/`TrackObject`. Recovery is expressed with the built-in `Fallback` / `RetryUntilSuccessful` instead of the Nav2-style `RecoveryNode`; `recovery_node.hpp` is gone. |
| C11 | 2026-09-19 | ✅ **Only 13 BT.CPP built-ins are in the palette**: `Sequence`, `Fallback`, `ReactiveSequence`, `ReactiveFallback`, `Repeat`, `RetryUntilSuccessful`, `Timeout`, `Delay`, `Inverter`, `ForceSuccess`, `ForceFailure`, `KeepRunningUntilFailure`, `SubTree`. The other 29 are hidden from `GET /nodes` and rejected at load time. | The palette is the LLM prompt, and 42 built-ins was mostly noise: scripting and blackboard nodes have nothing to carry between runs (each tree is generated fresh), and `Switch*` / typed `Loop*` / `Parallel` / `Async*` have no use in a table-top robot tree. **20 nodes total now** (7 robot + 13 built-in). **BT.CPP will not let you unregister a builtin** — `unregisterBuilder` throws `You can not remove the builtin registration ID`, which aborts the engine at startup — so the list is enforced in two places instead: `filterBuiltins` strips them from the palette XML, and `checkTreeStructure` rejects a tree that names one. Widening the set is a one-line edit to `allowedBuiltins()`. |
| C12 | 2026-09-19 | **Named `speed` profiles** on `NavigateToPoint`, `NavigateToDetectedObject` and `Patrol`: `slow` / `normal` / `fast` = 30% / 70% / 100% of Nav2's maximum. | Same pattern as the `distance` presets: names are the LLM contract, numbers are engine parameters (`speed_profile.*`, live-tunable, rejected outside 0-100). Delivered as a `nav2_msgs/SpeedLimit` on `/speed_limit`, which Nav2's controller already subscribes to, so no Nav2 parameters are touched. The limit is reset to 100% when each goal finishes. |
| C13 | 2026-09-20 | **One ROS domain: `ROS_DOMAIN_ID=59` everywhere**, replacing 42 (deployed) / 55 (simulator). | 59 is the domain the robot team's own Pi containers already use, so this joins theirs rather than inventing a third. It costs the old guarantee that the simulator could never drive the real robot: **stop `mocks` before driving for real**, or two `/navigate_to_point` servers will compete. |
| C14 | 2026-09-20 | **Tuning knobs leave the tree.** `Patrol` loses `x`/`y`/`width`/`height` (rectangle fixed at 0.60 x 0.40 m centred on (0.90, 0.60)); `IsObjectFound` loses `poll_ms`; `NavigateToDetectedObject` loses `pose_timeout_ms` and `replan_min_interval_ms`. They become engine parameters `camera_poll_ms`, `object_pose_timeout_ms`, `replan_min_interval_ms` — atomic, live-settable, non-positive values refused. `standoff` defaults to **0.05 m** (was 0.15). | The palette is the LLM's prompt: millisecond timings and patrol geometry are engine business it cannot reason about, and a bad value there is a wedged tree or corners inside the wall inflation. Fewer ports, fewer ways to be wrong. **Breaking**: BT.CPP rejects an unknown attribute outright, so trees generated against the old palette fail to load until the LLM re-reads `/nodes`. |

## Open

- ❓ Is the mini PC the same machine as the "PC" running the LLM / VLM + SAM2, or a separate box on the robot?
- ~~Does the robot really have a gripper/arm?~~ Yes — a gripper on `/gripper`, see C4. Whether there is also an arm (and so a real `/grasp_object`) is still open.
- ❓ Which teammate owns each real server (`/navigate_to_object`, `/visualize_object`, ...)?
- ❓ Robot hardware for nav: motor driver topic (`/cmd_vel`?) and watchdog, robot radius, front offset, top speed.
- ❓ Global camera: topic/type/frame of the robot pose (we assume `/robot_pose`, PoseStamped, map) and whether it will publish `/table_map`.
- ❓ Field is 1.8 x 1.2 m; where is the map origin (ArUco frame)?
- ❓ Will the camera team also publish `/object_detections` (onboard camera, robot frame)? Without it `TrackObject` cannot come back and the last 15 cm has no closed loop.
- ❓ `/object_goal_pose` is a bare `PoseStamped`, so it carries no object name — fine while one search runs at a time, wrong as soon as two do.
- ❓ First build + test on the actual mini PC (needs SSH access). Needs Docker installed, and DDS must be able to reach the robot's network (same subnet, same `ROS_DOMAIN_ID`).
