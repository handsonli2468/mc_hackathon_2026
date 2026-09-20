# mc_main_nav

LLM agent → BehaviorTree XML → `bt_engine` (HTTP) → ROS 2 modules → robot.

## Read first
- [README.md](README.md) — what each package is, HTTP API, node palette.
- [docs/decisions.md](docs/decisions.md) — every decision and why; ✅ = confirmed by the user, 🟡 = assumed.
- [docs/worklog.md](docs/worklog.md) — what was done, session by session, with test results.
- [docs/deploy.md](docs/deploy.md) — machines, SSH, how to deploy.
- [docs/nav_workflow.md](docs/nav_workflow.md) — how to work on navigation: what to start, edit, watch, test.
- [docs/debugging.md](docs/debugging.md) — when something misbehaves; start with `./scripts/doctor.sh`.
- [docs/nav.md](docs/nav.md), [docs/camera_approach.md](docs/camera_approach.md),
  [docs/llm_interface.md](docs/llm_interface.md) — navigation, camera-guided approach, agent-facing API.

**Keep `docs/decisions.md` and `docs/worklog.md` up to date** at the end of each chunk of work, without being
asked. Replaced decisions are struck through and get a change-log entry rather than being deleted.

## Machines (full detail in docs/deploy.md)
| Machine | SSH | Runs |
|---|---|---|
| dev laptop | – | development, simulator, RViz |
| mini PC | `ssh mcpc` | `bt_engine` :8080, `table_map` :8081, mocks |
| robot Pi | `ssh mcpi` | `pose_bridge` + Nav2 + `diff_nav` |

Repo lives at `~/mc_main_nav` on every machine; key-based SSH, user in the `docker` group.
Deploy with `rsync` (see docs/deploy.md), never copy `ws/build`, `ws/install` or `ws/log` between machines.

## The `./mc` tool
`./mc help` lists everything: `sim` / `rviz` / `status` / `logs` / `down`, `run` / `validate` / `nodes` /
`cancel`, `build` / `image` / `test`, `shell [svc] [cmd]` (into a container, ROS sourced),
`deploy pc|pi`, `ssh pc|pi`. Prefer it over long compose lines,
and add new common operations to it rather than to the docs.

## Working rules
- **Everything runs in Docker**, ROS 2 Humble. Images: `mc-main-nav:pc` (PC/laptop) and `mc-main-nav:robot`
  (Pi, Nav2 without RViz). One Dockerfile with `base` / `robot` / `pc` targets.
- **Two different builds:** `build dev` rebuilds the Docker image (only when `docker/Dockerfile` changes);
  `run --rm dev ./scripts/build.sh` runs colcon on `ws/src`.
- **After switching branches, delete `ws/build ws/install ws/log`** — generated interface files differ.
- **Verify before claiming.** Run it: `./scripts/bt_exec.py run examples/...` for the engine,
  `./scripts/test_nav_sim.py` for navigation (needs the sim stack up), pytest for the unit tests.
- **Exposed to the internet?** Then the services must run with `BT_ENGINE_TOKEN` / `TABLE_MAP_TOKEN`
  set; see docs/deploy.md. Never open a tunnel to services running without a token.
- **The node palette is the contract with the LLM.** Adding a node means: interface in `robot_interfaces`,
  registration in `bt_engine/src/register_nodes.cpp`, fake behaviour in `bt_mocks`.
