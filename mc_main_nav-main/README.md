# mc_main_nav: BT engine for LLM-generated behavior trees

An LLM agent writes BehaviorTree.CPP **v4** XML → sends it to this engine over HTTP → the engine
checks the XML, runs it, and calls robot modules through ROS 2 actions and services → it reports
status, a trace, and failure reasons back so the LLM can replan.

```
LLM agent ──HTTP──▶ bt_engine (x86 mini PC, Docker) ──ROS 2 actions/services──▶ nav / VLM+SAM2 / tracking / gripper
                                                        └── or bt_mocks (fake robot) for testing
```

**Why it's built this way / what runs where:** [docs/decisions.md](docs/decisions.md)

**For the LLM agent (how to send XML and read results):** [docs/llm_interface.md](docs/llm_interface.md)

**Working on navigation?** `./mc sim`, then [docs/nav_workflow.md](docs/nav_workflow.md)

```bash
./mc help          # every command: sim, run, test, build, doctor, deploy, ssh
./mc sim rviz      # simulator + RViz on this machine
./mc run examples/grab_cup_with_camera.xml
```

**Something broken?** [docs/debugging.md](docs/debugging.md), and run `./scripts/doctor.sh`

## Layout

| Path | What |
|---|---|
| `ws/src/robot_interfaces` | **The contract.** `.action`/`.srv` files that teammates implement. |
| `ws/src/bt_engine` | C++ engine: node palette, run manager, HTTP API. |
| `ws/src/diff_nav` | Robot Pi: object-level navigation on top of **Nav2**, camera-pose bridge, simulator, RViz config. See [docs/nav.md](docs/nav.md). |
| `ws/src/table_map` | Mini PC: table map for Nav2 + web UI to click obstacles (port 8081). |
| `ws/src/bt_mocks` | Python fake robot that implements every interface, with a small world model. |
| `docs/map_places.md` | Named places on the 1.8 x 1.2 m field, packaged as subtrees (`GoHome`, `GoToStorage`, ...) that a tree calls by name. |
| `examples/` | Sample trees. `fetch_cup_mission.xml` is the full mission (search, approach, grasp, deliver, recover); the rest are snippets, a made-up node and a runtime failure. |
| `scripts/` | build/run helpers + `bt_exec.py` HTTP client. |
| `docker/` | Image (ROS 2 Humble, multi-arch) + compose. |

## Quick start (any Docker host, x86 or arm64)

```bash
docker compose -f docker/compose.yaml build
docker compose -f docker/compose.yaml run --rm dev ./scripts/build.sh
docker compose -f docker/compose.yaml up -d engine mocks

./scripts/bt_exec.py run examples/find_and_grab_cup.xml
./scripts/bt_exec.py run examples/bad_hallucinated.xml     # rejected, never ticks
```

Node palette in a browser: **http://localhost:8080/**

With Nav2 and a simulated robot instead of the fake nav (obstacle UI: http://localhost:8081/):

```bash
MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object USE_RVIZ=true \
  docker compose -f docker/compose.yaml --profile sim up -d engine mocks map nav-sim
./scripts/test_nav_sim.py
```

Everything uses `network_mode: host`. Set `ROS_DOMAIN_ID` (default 59) to the same value on every machine.

## HTTP API (port 8080)

| Method | Path | Body / result |
|---|---|---|
| GET | `/` | **Node palette page** — every registered node with its ports, in a browser |
| GET | `/nodes` | TreeNodesModel XML. Put it in the LLM prompt. `?builtin=0` = robot nodes only. |
| POST | `/validate` | XML (raw or `{"xml": "..."}`) → `{ok, error}` (422 if invalid) |
| POST | `/execute` | XML → `202 {ok, run_id, preempted_previous}`. **Preempts** the current run. Invalid XML → 422 and the current run keeps going. |
| GET | `/status`, `/status/<run_id>` | `{state, running_leaves, last_leaf_failure, notes, trace}`. `?trace=full` returns the whole trace. |
| POST | `/cancel` | halts the current tree (cancels ROS action goals) |
| GET | `/runs` | last 20 runs |
| GET | `/health` | `{ok: true}` |

`state` is one of `running | success | failure | canceled | error`. `notes` holds the human-readable
reasons leaves gave for failing (e.g. `"'bottle' not found in view"`, `"action server '/grasp_object' not available"`).
Send them back to the LLM when asking it to replan.

Raw XML or `{"xml": "..."}` is detected from the first non-space character of the body (`{` = JSON); `Content-Type` is ignored.
When the engine is exposed to the internet it runs with a token: every API call needs `X-BT-Token: <token>` (or `?token=`), while the palette page and `/health` stay open. See [docs/deploy.md](docs/deploy.md).
Every reply is JSON, including 404 (`no such endpoint` / `no such run`) and 500 (engine bug).

Groot2 can connect to the running tree on port 1667.

An action node fails (instead of hanging) if its server doesn't answer a goal within 3 s (`goal_response_timeout_ms`) or disappears while the goal is running.

## Node palette

| Node | Kind | Ports | ROS server |
|---|---|---|---|
| VisualizeObject | Action | object_name | camera service `POST /visualize` |
| IsObjectFound | Condition | object_name | camera service `GET /status` (cached) |
| Patrol | Action | speed | `/navigate_to_point`, fixed clockwise rectangle for ever |
| NavigateToDetectedObject | Action | standoff, replan_distance, arrive_tolerance, speed | `/object_goal_pose` then `/navigate_to_point` |
| NavigateToPoint | Action | x, y (m, map frame), yaw_deg (optional) | `/navigate_to_point` |
| RotateInPlace | Action | angle_deg (default 45) | `/rotate_in_place` |
| SetGripper | Action | position (0 = fully open .. 100 = fully closed) | `/set_gripper` (SetGripper.action, publishes on `/gripper`) |
| *(used by diff_nav)* | – | – | `/get_object_pose` (GetObjectPose.srv, served by the VLM) |

Plus **13** BT.CPP built-ins — `Sequence`, `Fallback`, `ReactiveSequence`, `ReactiveFallback`, `Repeat`, `RetryUntilSuccessful`, `Timeout`, `Delay`, `Inverter`, `ForceSuccess`, `ForceFailure`, `KeepRunningUntilFailure`, `SubTree`.

The other 29 built-ins BT.CPP registers (scripting, blackboard, typed loops, `Switch*`, `Parallel`, `Async*`, ...) are **hidden from the palette and rejected at load time**; see C11.

Every action result has `bool success` and `string message`. `success=false` makes the BT node fail, and `message` shows up in `notes`.

### Adding a node

1. Add a `.action` (with `success` + `message` in the result) or reuse `ObjectQuery.srv` in `robot_interfaces`.
2. Register it in `bt_engine/src/register_nodes.cpp`. If the goal is only `object_name`, one line is enough:
   `factory.registerNodeType<ObjectActionLeaf<act::MyThing>>("MyThing", ctx, string("my_thing"));`
3. Add the matching fake behavior to `bt_mocks/mock_robot.py`.

## Mock robot

The mock keeps a small fake world, so trees behave realistically: navigating needs a prior visualize, grasping needs the gripper open, and so on.
You can inject failures and change delays while it runs:

```bash
docker compose -f docker/compose.yaml exec mocks bash
ros2 param set /mock_robot fail.navigate_to_object 0.5
ros2 param set /mock_robot delay.visualize_object 3.0
ros2 param set /mock_robot objects "['cup','bottle']"
```

When a real module is ready, stop serving that interface from the mock (or don't start the mock) and run the real server under the same name.
