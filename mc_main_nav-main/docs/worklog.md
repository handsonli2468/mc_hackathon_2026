# Work log

What has been built, how it was checked, and what's next. Newest session at the bottom.
For *why* things are the way they are, see [decisions.md](decisions.md).

## 2026-09-16: environment + engine v0

### Environment
- Test-built `ros:humble-ros-base-jammy` with the BT packages. Found what apt provides:
  `behaviortree-cpp` **4.10.0**, `behaviortree-cpp-v3` 3.8.7, `nav2-behavior-tree` 1.1.20 (v3-based).
- Tried arm64 emulation on the dev PC: not available (`exec format error`, no QEMU/binfmt). Left it alone because it changes host kernel settings.
- Wrote `docker/Dockerfile`, a slimmed version of the Eurobot-2026-Main one:
  - no VNC, Gemini, Nav2 or RViz;
  - added CycloneDDS, `nlohmann-json3-dev`, user `main` (uid 1000).
- Wrote `docker/compose.yaml` with services `dev`, `engine` and `mocks`: host network, repo mounted at `/home/main/mc_main_nav`, `ROS_DOMAIN_ID=42`.
- Wrote the helper scripts in `scripts/`: `build.sh`, `run_engine.sh`, `run_mocks.sh`, and `bt_exec.py`, a client for the HTTP API that only needs the Python standard library.

### Packages (`ws/src`)
- **robot_interfaces:** 6 actions (`NavigateToObject`, `VisualizeObject`, `TrackObject`, `GraspObject`, `RotateInPlace`, `SetGripper`) + `ObjectQuery.srv`.
- **bt_engine** (C++, about 1,060 lines):
  - `ros_nodes.hpp`: templates for leaves backed by ROS actions (halting cancels the goal, even if the server hasn't accepted it yet) and a yes/no service condition with a timeout.
  - `recovery_node.hpp`: Nav2-style `RecoveryNode` for v4.
  - `register_nodes.cpp`: the node palette plus extra checks (required ports, `RecoveryNode` must have 2 children).
  - `run_manager.cpp`: one run at a time, preemption, trace logger (running leaves, last failed leaf, events), 20-run history, Groot2 publisher on port 1667.
  - `http_api.cpp`: `/health /nodes /validate /execute /status /cancel /runs`. Accepts raw XML or `{"xml": ...}`, and sends CORS headers.
  - Bundled cpp-httplib v0.15.3 (`third_party/httplib.h`, compiled as a system include so its warnings are hidden).
- **bt_mocks** (Python): a fake robot with a world model. Failure rate and delay can be changed per action while it runs.
- **examples/:**
  - `find_and_grab_cup.xml`: the flow chart, fixed so `RecoveryNode` has 2 children and the find-retry logic matches what the chart meant;
  - `bad_hallucinated.xml`: uses a made-up node;
  - `find_missing_bottle.xml`: fails at runtime.

### Verified on the dev PC (x86)
| Test | Result |
|---|---|
| `colcon build` (3 packages) | ✅ ~30 s, no warnings from our code |
| Cup tree end-to-end vs mocks | ✅ `success` in ~7 s, running leaves reported step by step |
| Made-up node | ✅ 422 `Node not recognized: FlyToObject`, nothing ticked |
| Wrong port name (`object=`) | ✅ rejected by BT.CPP |
| Missing required port | ✅ rejected by our check |
| `RecoveryNode` with 1 child | ✅ rejected by our check |
| Nav forced to always fail (`fail.navigate_to_object=1.0`) | ✅ 3 nav attempts, 2 recoveries, then `failure` |
| Object not in the world (bottle) | ✅ `failure`, with the reason in `notes`: `'bottle' not found in view` |
| New tree sent mid-run | ✅ old run `canceled`, mock logged `navigate_to_object(cup) canceled` |

### Problems hit
- `TreeNode::config()` (non-const) is protected in 4.10. Fixed by using the const overload.
- The compose project defaulted to the name `docker` and saw other containers on the PC as leftovers ("orphans"). Renamed it to `mc-main-nav`. The old containers were removed by name only, and the other containers were not touched.

### Docs
- `README.md` (how to use it), `docs/decisions.md` (grilling answers + decisions made while building), this file.

## 2026-09-17: host change
- Engine host changed from Pi 5 to an **x86 mini PC** (decisions C1). Updated the docs and the Dockerfile comment. No code changes, since all testing so far was on x86.

- Initialized git (`main`) and committed everything; build outputs (`ws/build`, `ws/install`, `ws/log`) are ignored.

## 2026-09-17 (night): navigation module, branch `feat/diff-nav`
Worked on my own while the user slept. Design choices are N1–N10 in decisions.md; details are in [nav.md](nav.md).

- **diff_nav** (Python):
  - `controller.py`: approach and rotate controllers, acceleration limiter, deadband compensation. No ROS, so it can be unit-tested.
  - `nav_server.py`: the 3 servers, object pose refresh, pose-loss / timeout / cancel / preempt handling.
  - `sim_base.py`: kinematic differential-drive simulator. Noise and command timeout can be changed while it runs.
  - Launch files (`sim`, `robot`), `config/nav.yaml`.
- **robot_interfaces:** added `GetObjectPose.srv`.
- **bt_mocks:**
  - `object_positions` + `/get_object_pose`;
  - a `disable` list to hand servers over to real modules;
  - "at object" isn't checked when nav is external.
- **compose:** profiles `sim` (`nav-sim`) and `robot` (`nav`); `MOCK_DISABLE` env var.
- **scripts:** `run_nav.sh`, `test_nav_sim.sh`. **examples:** `nav_demo.xml`.

### Verified (x86 simulator)
| Test | Result |
|---|---|
| Controller unit tests | ✅ 15/15 |
| `test_nav_sim.sh` (8 checks: demo, unknown-object reason, cancel stops robot, full cup chart, pose loss, IsAtObject far) | ✅ 8/8, both with and without 15 % velocity noise |
| Final pose after the demo | ✅ 0.40 m from the cup (target 0.35 ± 0.05), heading error 0.07 rad (limit 0.08) |
| Mocks-only regression (original 3 examples) | ✅ same results as before |
| Mock killed mid-goal (`docker stop` / `docker kill`) | ✅ tree fails with "went away during the goal" after 15 s / 10 s (DDS lease) |

### Problems hit
- rclpy doesn't allow one line of code to log at two different severities → split the call. (Before the fix, this crashed the goal and the failure reason came back empty.)
- In Humble, a string-array parameter declared without a value raises an error when read → the mock crashed on start when `disable` wasn't set. Fixed.
- **Engine bug:** a goal sent to a dead or stale server left the tree RUNNING forever. Added a goal-response timeout and a check that the server is still there (commit `c00ce4f`).

### Not done
- Nothing tested on arm64 or real hardware (no emulation on this PC, no Pi access).

## 2026-09-18: table scale, then Nav2 (branch `feat/diff-nav`)
**Table scale.** The robot drives on a meeting-room table, so distances are centimetres. `NavigateToObject` got a named `distance` port (`contact`/`near`/`far`): the engine checks the name before running and sends metres in the new `stop_distance` goal field.

**Switched to Nav2** (C3, N11–N18). The hand-written controller and its tests were deleted.
- **diff_nav:**
  - `nav_server` now calls Nav2 (`navigate_to_pose`, `spin`) and picks a free approach spot from the global costmap;
  - new `pose_bridge` (camera pose → TF + `/odom`);
  - `geometry.py` + tests;
  - `sim_base` publishes `/robot_pose` like the camera would, with test settings `teleport` and `camera_enabled`;
  - `nav2_table.yaml` (from Humble's defaults, scaled to the table);
  - `nav2_bt_table.xml` (BackUp 2 cm);
  - launch files `nav`, `sim` (`use_rviz`) and `rviz`, plus `rviz/table.rviz`.
- **New `table_map` package:** map node (camera map / config size + edge + obstacles → `/map`), web UI on 8081, unit tests.
- **Docker image:** added `navigation2`, `nav2-bringup`, `rviz2`, `nav2-rviz-plugins` (image now 3.6 GB+). Compose services `map`, `nav-sim` (sim profile), `nav` (robot profile) and `rviz` (gui profile), with X11 passed through like the team's Eurobot-2026-Navigation2 repo.
- **Tests:** `scripts/test_nav_sim.sh` replaced by `scripts/test_nav_sim.py` (9 tests against real Nav2).

### Verified (x86 simulator)
- Unit tests 12/12; end-to-end 9/9. Numbers are in [nav.md](nav.md#tested-simulator-x86-2026-09-18).
- First end-to-end run found two issues, both fixed:
  - camera loss only failed after the 90 s timeout → now 3.3 s;
  - stops were about 1 cm short → goal tolerance 1 cm → 5 mm; now within 4 mm.
- RViz opens on the dev PC's display from the container. The only error is a harmless GLSL warning.

### Problems hit
- A floating-point tie made a sort-order test flaky → it now rounds before comparing.
- Branch switching needs a clean `ws/build ws/install ws/log` (generated interface files differ between branches).

### Not done
- Nothing on the Pi or real hardware. The web UI was checked through its API and while running, but I haven't clicked through it in a browser.
## 2026-09-18: LLM interface doc
- Wrote `docs/llm_interface.md`: how the LLM agent sends XML and reads results (the request loop, XML rules, every endpoint with real responses, what to send back to the LLM on failure). Response examples were captured from the running engine.
- Note: after switching branches, delete `ws/build ws/install ws/log` before building. Leftovers from `feat/diff-nav` (the generated `GetObjectPose` files) broke the build on `main`.

## 2026-09-18 (main): LLM interface doc gaps
Review of `docs/llm_interface.md` found gaps an autonomous agent would hit. Behaviour checked against the running engine first, then documented:
- how raw XML vs JSON is detected (first non-space character; `Content-Type` ignored);
- which fields exist mid-run: `trace` and `last_leaf_failure` build up live, `notes` is empty until the run ends, `finished_at` only at the end;
- a real `state: "error"` example (produced with a bad `Script` node) and the `error` field in the field table;
- `/status` before any run → 404, same as an unknown id; `/cancel` with nothing running → 200 `was_running: false`;
- concurrency: `/execute` calls are serialized (second preempts, `preempted_previous: true`), `/status` is snapshot-safe for several clients;
- "name your nodes" moved up next to the other XML rules.

**Code change:** the engine now answers every request with JSON, including httplib's own replies — unknown path/method → 404 `no such endpoint`, uncaught exception → 500 with the message (`set_error_handler` / `set_exception_handler`). Verified that our own 404/422/400 bodies are untouched.

## 2026-09-18 (main): node palette page
- The engine now serves a palette page at **`GET /`** (`ws/src/bt_engine/web/index.html`, installed to the package share dir, path overridable with the `web_dir` parameter). It reads `/nodes` and `/nodes?builtin=0`, groups nodes by kind, shows each port with type, default and description, marks built-ins, filters by node or port name, and copies the raw model for the LLM prompt.
- Static files are served with httplib's mount point; the JSON API and its 404 for unknown paths still behave as before.
- Verified by rendering the page headlessly (node + jsdom) against the running engine: 11 robot nodes + 42 built-ins, sections and filters correct, no JS errors.
- Added a "How to run it" section to the same page: copy-paste command blocks (each with a Copy button) for `main` (engine + mocks + example trees + mock failure injection) and for `feat/diff-nav` (clean rebuild, Nav2 sim stack, obstacle UI, `test_nav_sim.py`, unit tests, RViz, robot Pi), plus how to stop everything. Re-checked with jsdom: no JS errors, palette still renders.

## 2026-09-18: merged `feat/diff-nav` into `main`
- Conflicts were additions on both sides (README quick start, worklog sections); both kept.
- Updated after the merge: the palette page's "How to run it" no longer says to switch branch, and `docs/llm_interface.md` documents the `distance` port on `NavigateToObject`.
- **The image had lost Nav2**: the earlier rewind reverted `docker/Dockerfile`, and a later `build dev` produced an image without navigation2, so `nav-sim` failed with `No module named 'nav2_common'`. Fixed by rebuilding the image from the merged Dockerfile — a reminder that a rewind does not undo built images.
- Verified on merged `main`: colcon build 5 packages, unit tests 12/12, the three mocks-only examples unchanged, and `test_nav_sim.py` 9/9 through Nav2.

## 2026-09-18: Dockerfile split + first Raspberry Pi deployment
- `docker/Dockerfile` now has three stages: `base` (ROS 2 Humble, CycloneDDS, colcon, user) → `robot` (+ Nav2, for the Pi) → `pc` (+ BehaviorTree.CPP, RViz). Compose builds `mc-main-nav:pc` for the PC services and `mc-main-nav:robot` for `nav` / `build-robot`.
- Both targets build on x86. **Size saving is small**: robot 3.61 GB vs pc 3.62 GB, because Nav2 dominates and pulls BehaviorTree.CPP in anyway. The point is that the Pi gets no RViz/OpenGL and the roles are explicit.
- `diff_nav/package.xml` no longer exec_depends on rviz2 / nav2_rviz_plugins, so rosdep won't pull a GUI onto the robot.
- **Renamed the project `mc_hack` → `mc_main_nav`** (directory here and on the Pi, compose project, images `mc-main-nav:pc` / `mc-main-nav:robot`, the in-container path `/home/main/mc_main_nav`, and all docs). Both images were rebuilt because the container work directory changed.
- Pi (`ssh mcpi`): aarch64, Ubuntu 24.04, 4 cores, 7.7 GB RAM, 43 GB free, Docker 29.8.1, user in the docker group. Repo copied with rsync to `~/mc_main_nav` (build/install/log and the Groot AppImage excluded).

## 2026-09-18: deployed to the mini PC and the robot Pi
- **Robot Pi (`ssh mcpi`)**: image `mc-main-nav:robot` built natively (~20 min), workspace subset built in 39 s, `nav` + `sim-base` running.
- **Mini PC (`ssh mcpc`)** (x86_64, Ubuntu 24.04, 16 cores, 30 GB RAM, 192.168.50.125): image + workspace built, `engine`, `mocks` (nav servers disabled) and `map` running.
- **Port clash:** the VLM teammate's `vlm-server-tracker` already owns 8080 on the mini PC, so the engine failed to bind. `scripts/run_engine.sh` now takes `ENGINE_PORT` (default 8080); the mini PC runs on **8090**.
- **New `sim-base` compose service** (robot profile) to test the Pi's Nav2 without hardware.
- **Cross-machine test passed:** laptop CLI → engine on the mini PC → mocks there → Nav2/`diff_nav`/fake robot on the Pi; `nav_demo.xml` succeeded in 17 s. ROS 2 discovery across machines worked on this Wi-Fi without extra configuration; `docker/cyclonedds.xml` is there in case a venue network blocks multicast.
- **Docs:** new `docs/deploy.md` (machines, SSH, deployment, checks) and `CLAUDE.md` (project rules for future sessions).
- Watch out: only one engine per ROS domain. Two `/bt_engine` nodes appeared while the laptop stack was still up; the laptop stack was stopped.

## 2026-09-18: stable public URLs (Tailscale Funnel)
- Installed Tailscale **in userspace mode as the normal user** on the mini PC (no root there): binaries in `~/bin`, `tailscaled --tun=userspace-networking`, machine `mcpc` on tailnet `taile84e23.ts.net` (the user approved the login link).
- `tailscale funnel` serves **https://mcpc.taile84e23.ts.net** (engine, port 443) and **https://mcpc.taile84e23.ts.net:8443** (obstacle UI). Cloudflare tunnels stopped; `scripts/expose.sh` now does Funnel by default and keeps Cloudflare as `expose.sh cloudflare`.
- Verified over Funnel: health and pages 200, `/nodes` 401 without the token and 200 with it, map API likewise, and `nav_demo.xml` ran through the public URL to the robot Pi and succeeded.
- Getting the login URL needed patience: `tailscale up` prints nothing over SSH without a terminal; the URL appears in `status --json` (`AuthURL`) and in the daemon log about a minute later.
- Not automatic after a reboot: rerun `./scripts/expose.sh` (enabling linger would need root).

## 2026-09-18: token auth + public tunnels
The LLM runs on a server outside this network, so the engine had to be reachable from the internet.
- **Token auth** in `bt_engine` (`BT_ENGINE_TOKEN` / param `auth_token`, header `X-BT-Token` or `?token=`) and in `table_map` (`TABLE_MAP_TOKEN`, `X-Map-Token`). Only `/`, `/index.html` and `/health` stay open, so the pages can load and ask for the token; everything else answers 401 without it. Both web pages prompt once and keep the token in the browser; `bt_exec.py` reads `BT_ENGINE_TOKEN`.
- **`scripts/expose.sh`** starts Cloudflare quick tunnels for the engine and the map UI on the mini PC and prints the URLs (`--stop` closes them). No sudo or account needed; URLs are random and change on restart.
- Tested locally first (401/200 for header, query, wrong token; pages and health open), then over the public URLs: `/nodes` 401 without the token, 200 with it, and `nav_demo.xml` ran through the tunnel to the Pi and succeeded.
- Two traps worth remembering: compose only passes the token env vars that are listed in the service `environment:`, and `pkill -f "cloudflared tunnel"` over SSH kills the SSH session itself (the pattern matches the remote shell's own command line) — use `pkill -x cloudflared`.

## 2026-09-19: the palette is 20 nodes, not 49
- Trimmed the BT.CPP built-ins from **42 to 13**. With 7 robot nodes that is a **20-node palette**, which is the whole LLM prompt. Kept: `Sequence`, `Fallback`, `ReactiveSequence`, `ReactiveFallback`, `Repeat`, `RetryUntilSuccessful`, `Timeout`, `Delay`, `Inverter`, `ForceSuccess`, `ForceFailure`, `KeepRunningUntilFailure`, `SubTree`.
- Dropped the scripting and blackboard family (`Script`, `ScriptCondition`, `SetBlackboard`, `UnsetBlackboard`, `WasEntryUpdated`, `SkipUnlessUpdated`, `WaitValueUpdate`), the typed loops, `Switch2`..`Switch6`, `Parallel`/`ParallelAll`, `AsyncSequence`/`AsyncFallback`, `SequenceWithMemory`, `TryCatch`, `IfThenElse`, `WhileDoElse`, `Precondition`, `RunOnce`, `Sleep`, `AlwaysSuccess`, `AlwaysFailure`.
- **The obvious implementation does not work.** `factory.unregisterBuilder(id)` throws `BT::LogicError: You can not remove the builtin registration ID [AlwaysFailure]`, and since it ran at startup the engine aborted on boot (`[ros2run]: Aborted`) — the palette went to *zero* nodes before I read the log. BT.CPP guards its built-ins deliberately.
- So the allow-list is enforced in two places instead: `filterBuiltins()` strips hidden entries from the `TreeNodesModel` XML on the way out of `GET /nodes`, and `checkTreeStructure()` rejects any tree that names one. Needed tinyxml2 (`tinyxml2_vendor` in `package.xml` + `CMakeLists.txt`); the first link failed with `undefined reference to tinyxml2::XMLDocument::Parse`.
- Verified: `/nodes` lists exactly 20; `<Script code="x := 1"/>` is rejected with `node 'poke' uses 'Script', which is not in the palette; see GET /nodes`; a tree using `Fallback` + `Timeout` + `RetryUntilSuccessful` + `ForceSuccess` validates and runs to success; all five real examples still validate.
- Widening the set later is one line in `allowedBuiltins()`.

## 2026-09-19: three more nodes out of the palette
- Removed **`IsObjectVisible`**, **`IsAtObject`** and our custom **`RecoveryNode`**. 8 robot nodes left.
- With both conditions gone, `ObjectQueryCondition` had no users, so it is deleted from `ros_nodes.hpp` along with the now-unused `robot_interfaces/srv/object_query.hpp` include. `recovery_node.hpp` is deleted outright, and the "RecoveryNode must have exactly 2 children" check is out of `checkTreeStructure` — with no node to check, it was dead code.
- **Recovery is now plain BT.CPP.** Verified on the wire: `Fallback` with an impossible `NavigateToPoint` then a `RotateInPlace` runs the recovery branch and the tree succeeds. A tree still naming `RecoveryNode` is rejected at validate: `Node not recognized: RecoveryNode`.
- **`diff_nav/config/nav2_bt_table.xml` is untouched and unaffected** — its `RecoveryNode`s are Nav2's own, running inside Nav2's BT.CPP v3 engine, which is separate from `bt_engine`.
- The `/is_at_object` and `/is_object_visible` services and their `diff_nav` / mock servers stay in place, so either condition can be registered again in one line.
- **A stale process, not a stale build:** after rebuilding, `/nodes` still listed all three. The engine container had been up for an hour and was running the old binary — the repo is bind-mounted, so `./mc build` alone never restarts anything. `docker compose restart engine` fixed it. Worth remembering: `./mc sim` does not recreate a container that is already up.
- Verified: `bt_engine` builds clean, all five real examples validate ok, `nav_demo.xml` runs to success.

## 2026-09-19: coordinate mode + camera-guided approach
- **`NavigateToPoint x y [yaw_deg]`**: drive to a coordinate in the map frame. Refuses points that are blocked, inflated or off the table, before moving. Omit `yaw_deg` to arrive facing the way you drove.
- **No odom frame any more.** There is no wheel odometry, so `pose_bridge` publishes TF `map -> base_link` directly and `/odom` (velocity for Nav2's controller) in the map frame; both costmaps and the behavior server now work in `map`.
- **`TrackObject` is now the camera-guided approach** (was a mock placeholder): `visual_servo.py` (pure control law) + a server in `diff_nav` that drives `/cmd_vel` at 10 Hz from `/object_detections`. New message `robot_interfaces/msg/ObjectDetection` is the contract for the vision team. When the camera loses the object in the last centimetres (fixed forward camera), the servo finishes the move blind using distance travelled from the global camera.
- **`sim_camera`** fakes the pipeline (FOV 60°, range 8 cm–1 m, noise) so all of this is testable without hardware; compose service `sim-camera` for the Pi.
- **New example** `examples/grab_cup_with_camera.xml`: find → Nav2 `far` → camera `contact` → grasp.
- **Docs:** `docs/camera_approach.md` (contract for the vision team, control law, failures, settings).

### Verified (simulator)
- Unit tests 23/23, end-to-end 16/16 (4 new coordinate tests, 3 new camera tests).
- Camera tree: final gap 2 cm for a 1 cm target, heading error 0.003 rad, blind finish reported in the result message.
- Object behind the robot → fails with "never saw the object" instead of searching, as agreed.

### Problems hit
- Two Nav2 stacks on one ROS domain (the Pi's was left running on domain 55 from an earlier check) produce "unknown goal response" and aborted goals that look like Nav2 bugs. Dev work now uses `ROS_DOMAIN_ID=55` and the deployed robot stays on 42.
- `_load` and the `fields` import were lost in the earlier Nav2 rewrite; both crashed `TrackObject` at runtime. An action server whose callback raises returns an **empty** message, so the engine now says "aborted by '/track_object' (server crashed? check its log)".
- `/navigate_to_point` "missing" on the Pi was a restart race, not a build problem.

## 2026-09-19: networks change, addresses don't
- The laptop moved off the 192.168.50.x Wi-Fi, so `ssh mcpc` / `ssh mcpi` both stopped working. The mini PC stayed reachable over **Tailscale** (`wildbot@100.113.211.14`, host keys verified identical to the LAN entry) and its Funnel URL kept answering, so deployment continued over that.
- The robot Pi is **not** on Tailscale, so it is unreachable off the robot's network. Worth installing there too.
- `docs/deploy.md` now says this, with the Tailscale address and an `~/.ssh/config` entry.

## 2026-09-19: debugging kit
- **`scripts/doctor.sh`**: one-shot health check from inside a running container — ROS graph (including duplicate nodes, which caused a confusing afternoon), `/robot_pose` and `/object_detections` rates, `/map`, TF `map -> base_link`, `/cmd_vel` wiring, all six action servers, all four services, and the engine's HTTP API. Every line is OK/WARN/FAIL with a hint.
- Checked both ways: healthy stack → 24 OK, 1 warning (the mock is answering); with the mock and camera stopped → the right FAIL/WARN lines appear.
- **`docs/debugging.md`**: who owns which failure, how to read a failed run (`notes` first), a symptom → cause → fix table for the failures we can foresee, how to test one action at a time, the first-time-with-hardware order (motor watchdog first), how to stop the robot in a hurry, the traps already hit, and what to collect when asking for help.

## 2026-09-19: sim.sh, after the workflow doc tripped the user up
- Two copy-paste traps in `nav_workflow.md`: `USE_RVIZ=true` looked like a compose argument (it is an environment variable and must come **before** `docker`), and a sentence-ending period got copied into `... run --rm rviz.`.
- **`scripts/sim.sh`** now does the whole thing: `up` / `up rviz` / `rviz` / `status` / `logs` / `down`. It sets `ROS_DOMAIN_ID=55` and `MOCK_DISABLE` itself, waits for Nav2, runs `doctor.sh`, and prints the three URLs. Tested from a clean stop: 24 OK, 1 warning.
- The doc now leads with `./scripts/sim.sh up` and keeps the long form underneath, with the environment-variable placement spelled out.

## 2026-09-19: navigation workflow guide
- **`docs/nav_workflow.md`**: the task-oriented guide — which containers to start (simulator on `ROS_DOMAIN_ID=55`, never 42), how to run a tree or a single action, the four ways to watch it (RViz, obstacle UI, Groot2 on 1667, run status/logs), a file-to-behaviour table, when a rebuild is needed and when a restart is enough, the test commands, and recipes (slow the robot down, change stop distances, make the camera gentler, inject noise and failures, add a navigation node end to end).
- Every command in it was run before writing it down. One turned out to be a lie: `ros2 param set /bt_engine nav_distance.*` succeeded but changed nothing, because the engine only read those presets at startup.
- **Fixed that instead of documenting it**: the engine now has a parameter callback, so distance presets can be re-tuned live (rejecting non-positive values). Verified by setting `contact` to 3 cm while running — the robot then stopped 4.2 cm from the cup instead of 2 cm.

## 2026-09-19: "the service is not up"
- **Most likely cause: an old URL.** The Cloudflare tunnel URLs handed out first were stopped when Tailscale Funnel took over. Six probes of the live URL returned 200 in ~50 ms; the old one returns nothing.
- **`./scripts/expose.sh ensure`**: idempotent, quiet, safe for cron; installed on the mini PC every 5 minutes. Two bugs found while testing it: `tailscale funnel status` exits non-zero even when it is on (so `pipefail` made the check always fail), and checking for "Funnel on" passed when only one of the two ports was served — it now requires both, and repairs a partial failure.
- Caution: `funnel --https=443 off` takes the engine offline; I did that while testing and restored it. It is the one command here that interrupts the team.

## 2026-09-19: the mock's world kept resetting
Teammate reported the mock on the mini PC "not accessible". It was reachable the whole time: the engine log showed their tree failing five times with `'blue bottle' not found in view`.
- **Cause:** the extra objects had been added with `ros2 param set`, which does not survive a container restart; the mocks container had restarted 12 hours earlier and was back to `['cup']`.
- **Fixes:** `MOCK_OBJECTS="name:x:y,..."` (environment variable, so it survives restarts) sets the fake world, defaulting to cup/box/bottle; the "not found" message now lists the objects the robot knows about, and `get_object_pose` does the same. Deployed with cup, box, bottle and plate.
- **The mock now accepts any object name** (user's call): an unknown name gets a stable invented position on the table, so the LLM can ask for "blue bottle" without anyone pre-registering it. `accept_any_object:=false` restores the strict behaviour.

## 2026-09-19: grasping with just the gripper, and the Pi's new address
- **"not near 'x'"**: the message a teammate hit comes from the mock (`mock_robot.py`), raised by `TrackObject` and `GraspObject` when the mock does not think the robot is standing at that object. `self.at` is only set by `NavigateToObject`, which itself needs `VisualizeObject` first — and `NavigateToPoint` clears it. So the mock enforces visualize -> navigate -> track -> grasp on the *same* name.
- **`CloseGripper` now picks up the object** the robot drove to (user's call: there may be no arm, so closing the fingers *is* the grasp). Previously only `grasp_object` set `held`, so a close-only tree could never satisfy `IsObjectHeld`. When navigation is handed to the real robot the mock never sees the arrival, so it falls back to the last object the tree pointed at. `GraspObject` is unchanged and stays the node for a real manipulation module — nothing in this repo serves `/grasp_object` yet; the action file names manipulation as its owner.
- Verified through the public URL against the deployed engine: a tree ending `TrackObject` + `CloseGripper` + `IsObjectHeld` succeeds, and `find_and_grab_cup.xml` (which uses `GraspObject`) still succeeds.
- **The robot Pi moved to another subnet** (`192.168.68.53`, while the laptop is on `192.168.50.0/24`), which is why it looked dead. The mini PC has a leg on both networks, so `ProxyJump mcpc` in `~/.ssh/config` restores `ssh mcpi`, `./mc ssh pi` and `./mc deploy pi`. Repo resynced to the Pi and rebuilt there (2 packages, 2.9 s). Its `nav` and `sim-base` containers are still stopped; only the teammate's vision client runs.

## 2026-09-19: the real gripper
- The robot does have a gripper, controlled by **one `std_msgs/Int32` on `/gripper`** (1 open, 0 close), with no feedback from the hardware.
- **`ws/src/diff_nav/diff_nav/gripper.py`**: `gripper_server` serves `/set_gripper` for real — publishes the value once, then waits `settle_seconds` (2.0, live-tunable) so the tree cannot move on while the jaws are still travelling. Cancel during the wait reports honestly that the command is already out and the jaws keep moving. The publisher is **transient-local**, so a motor driver that subscribes late, or restarts, still receives the last command; if nothing is subscribed it logs a warning rather than failing silently.
- Started by `nav.launch.py` on the Pi, parameters in `config/nav.yaml` under `gripper_server`.
- Verified on an isolated ROS domain: `open` and `close` each took **2.01 s** and put exactly one message on `/gripper` (`data: 1`, then `data: 0`).
- **The simulator hit this immediately**: `sim.launch.py` includes `nav.launch.py`, so `gripper_server` and `mock_robot` were both serving `/set_gripper`. `scripts/sim.sh` now adds `set_gripper` to `MOCK_DISABLE`, and the **mock subscribes to `/gripper`** instead — the fake world follows the jaws whoever owns the action, so `IsObjectHeld` keeps working. Verified in the simulator: `TrackObject` + `CloseGripper` + `IsObjectHeld` succeeds in 15 s, with each gripper step taking its 2 s.
- Worth knowing: my first test was answered by the **mock**, not the new node — both were serving `/set_gripper` on domain 55, and rclpy only warned "more than one action server". `MOCK_DISABLE=set_gripper` is required on the mini PC once the robot's gripper runs.

## 2026-09-19: one gripper node, not three
- `OpenGripper` and `CloseGripper` are **gone**. `SetGripper position="0..100"` is the only gripper node — the presets were just two fixed goals on the same `/set_gripper` server, so the palette carried three names for one topic (`/gripper`).
- Removed the `GripperPresetLeaf` template from `ros_nodes.hpp` and its two registrations in `register_nodes.cpp`; `GripperLeaf` and the existing pre-run 0..100 range check are untouched.
- Updated both example trees (`find_and_grab_cup.xml`, `grab_cup_with_camera.xml`) to `SetGripper position="0"` / `position="100"`, plus README's palette row (which had never listed `SetGripper` at all), `docs/llm_interface.md`, `docs/deploy.md` and the module docstrings in `gripper.py` / `mock_robot.py`.
- Verified: `./mc build` clean, `./mc nodes` shows exactly one gripper node (**11 robot nodes**, down from 13 — the count in the previous entry), both examples `validate` ok and run to SUCCESS against the sim (`find_and_grab_cup` 15.7 s, `grab_cup_with_camera` green through `close_gripper` → `is_cup_held`), 23 unit tests pass.
- Nothing changed below the BT layer: same `SetGripper.action`, same `gripper_server`, same single latched `Int32` on `/gripper`.

## 2026-09-19: the gripper is a span, not a switch
- The tree sets **how far** the jaws close: `/gripper` carries 0 (fully open) .. 100 (fully closed), chosen by the BT.
- `SetGripper.action` now carries `int32 position` instead of `bool open`. `OpenGripper` (0) and `CloseGripper` (100) stay as the two ends, and a new **`SetGripper position="N"`** node exposes the rest — 13 nodes in the palette now.
- **Out-of-range is caught before anything moves**: `validate` on `position="150"` returns `node 'SetGripper::2' has position='150'; it must be between 0 (open) and 100 (closed)`, the same pre-run check as the `distance` presets. The server also clamps and warns, in case a value arrives from the blackboard.
- The mock treats holding as a **threshold**, not a flag (`gripper_hold_position`, 50): closing to 40 does not pick anything up. Verified — the same tree ending in `CloseGripper` succeeds (12 s), ending in `SetGripper position="40"` fails at `IsObjectHeld`.
- Verified on the wire: a tree of `OpenGripper` / `SetGripper 40` / `CloseGripper` put exactly `0`, `40`, `100` on `/gripper` and took 6.0 s (3 x the 2 s settle).
- **Caught only on the mini PC**: the mock crashed on the first `OpenGripper` with `'SetGripper_Goal' object has no attribute 'x'` — its goal *logging* guessed the goal's shape from `hasattr(goal, "open")`, which the interface change removed. The simulator never saw it because `MOCK_DISABLE` hands `set_gripper` to the real server there, while the mini PC has no `MOCK_DISABLE`. The logging now falls back to an empty string instead of crashing. Lesson: after an interface change, test on the machine where the mock still owns the action.

## 2026-09-19: the funnel was lying, and so was my test
Two teammates, on completely different machines (a Linux k8s pod and a Windows laptop), got TLS
handshake errors from `https://mcpc.taile84e23.ts.net` while Cloudflare worked.
- **My first diagnosis was wrong.** I probed the public URL from this laptop, got 200 in 30 ms, and concluded the service was fine and their network was at fault. But this laptop is *on the tailnet*: MagicDNS resolved the name to `100.113.211.14` and every probe went over the VPN, never touching the public path. Forcing public DNS (`curl --doh-url`, or `--resolve` to the ingress IP) reproduced their exact error immediately.
- **My second diagnosis was also wrong.** I blamed `Hostinfo.IngressEnabled` going false and dated the outage to 12:50 from `handleIngress: ... unconfigured; rejecting` lines. Both were my own footprints: the 12:50 rejections were me testing `ensure` earlier that day, and the only `IngressEnabled changed to false` in the whole log is my own toggle at 15:44:32. Ingress had been enabled continuously since 18 Sep.
- **Real cause: a default-route flap.** The mini PC is dual-homed (`enp100s0` 192.168.68.51 metric 100, `wlp98s0` 192.168.50.125 metric 600) and the default flips between them for about two seconds roughly every 40 minutes — 01:47, 13:57, 14:37, 15:22 — which looks like DHCP lease renewal. Each flip kills tailscaled's control connection (11 `PollNetMap ... use of closed network connection` today). At **15:22:56** it reconnected but the Funnel registration did not come back, so Tailscale's ingress servers stopped forwarding for the hostname and closed the TLS connections themselves. That is why mcpc logged **nothing** during the outage and why both teammates saw a handshake error instead of any HTTP status.
- **Fix:** toggle the funnel off and on, which pushes a fresh registration to the control plane; the public endpoint recovers in about 15 s (verified on both ingress IPs, 103.84.155.153 and .217). **It will recur** — the flap is on a ~40 minute cycle — so the keep-alive is what actually contains this. Open: stop the flapping (`ipv4.never-default yes` on the Wi-Fi, or a static address) and/or run the keep-alive more often than every 5 minutes.
- **Why nothing caught it:** `expose.sh ensure` checked the local config, which was correct the whole time. It now probes the real public URL with `--doh-url` (so MagicDNS cannot fake a pass) and re-announces the funnel when that fails. Tested both ways: silent and 0.46 s when healthy; when I turned ingress off behind its back it noticed, repaired it and confirmed reachability. `expose.sh` with no arguments now also verifies the public endpoint before printing the URLs.
- **Hardened what actually threatened uptime**, which turned out not to be the funnel: on the mini PC the containers had restart policy `no`, `tailscaled` has no service unit, and cron had no `@reboot` entry — so a power blip would have taken everything down until someone noticed. Now: `restart: ${RESTART_POLICY:-no}` in compose (deployed machines set `unless-stopped`, dev machines do not), an `@reboot` entry that brings the tunnel back, and the keep-alive moved from 5 minutes to 2.
- Verified recovery properly: my first test used `docker kill`, which Docker treats as a manual stop, so the container stayed down and proved nothing (and briefly took the map UI offline). Killing PID 1 *inside* the container is a real crash — it came back on its own and served 200.
- Cloudflare quick tunnels are running alongside as a second opinion and a fallback.
- **Rule of thumb, now in `docs/debugging.md`:** a TLS handshake error is never a token problem. A bad token returns a normal 401 JSON, and `/health` takes no token at all.

## 2026-09-19: palette cut to 8 nodes
User's call: drop `GraspObject`, `IsObjectHeld`, then `NavigateToObject` as well.
- **`GraspObject`** was never implemented by anything but the mock — the action file names manipulation as its owner. **`IsObjectHeld`** was inferred from the last gripper command, not a sensor, so it could only ever confirm what the tree had just done. Picking up is now `SetGripper position="0"` → `TrackObject` → `SetGripper position="100"`.
- **`NavigateToObject`** went because `TrackObject` already drives to a named object, using what the camera sees instead of the map estimate.
- Nothing below the BT layer was deleted: the actions, the services, `diff_nav`'s servers and the mock's all stay, so any of the three can come back by re-registering it in `register_nodes.cpp`.
- The `distance` preset check moved from `NavigateToObject` to `TrackObject`, which is now the only node with that port — it had never been validated there before, so bad presets on `TrackObject` used to slip through to runtime.
- **The trade-off to remember:** `TrackObject` drives `/cmd_vel` straight at the object and ignores the costmap. `NavigateToPoint` is now the only node that plans around the obstacles drawn in the map UI, so crossing the table safely means going to a coordinate first. All four working examples were rewritten that way.
- **The mock needed a matching change.** Its `track_object` required `self.at == obj`, which only `navigate_to_object` ever set — so with `NavigateToObject` gone, every `TrackObject` failed with `not near 'cup'` in the pure-mock setup on the mini PC. `track_object` now does the approaching itself: it requires the object to have been located (`VisualizeObject` first) and sets `at`. Caught only because I re-ran the examples against mcpc, where the mock still owns the navigation calls; the simulator hands them to `diff_nav` and never exercised that path.
- Verified in the simulator: `find_and_grab_cup` 11.2 s and `grab_cup_with_camera` 11.3 s succeed, `nav_demo` 18.6 s succeeds including its deliberate inverted failure, `find_missing_bottle` fails as designed, `bad_hallucinated` is still rejected at validation, 23 unit tests pass.

## 2026-09-19: search nodes (patrol, camera service, track when found)
Three new nodes so the robot can look for something instead of being told where it is.
- **`VisualizeObject` is now an HTTP decorator.** One POST to the camera team's service ("start looking for cup"), sent once per activation, then it just runs its child. The old ROS-action version is gone from the palette.
- **`Patrol`** drives a clockwise rectangle for ever, one `NavigateToPoint` goal per corner, facing along each leg. Ports `x`/`y`/`width`/`height` default to a 60 x 40 cm loop in the middle of the field, so a bare `<Patrol/>` is already sensible. One unreachable corner is skipped with a note; four in a row fails.
- **`TrackWhenFound`** polls the camera service on a timer while its child searches. When the camera reports the object it halts the child and drives to the pose the camera publishes, re-issuing the goal if the object moves more than `replan_distance`. Five consecutive HTTP failures fail the node, so a blip does not end the search but a dead service does.
- **The pose comes in on `/object_goal_pose`, deliberately not `/goal_pose`** — Nav2's `bt_navigator` subscribes to that name and would have driven off on its own the moment the camera published, behind the tree's back. Confirmed with `ros2 topic info` before choosing.
- **`standoff` (0.15 m) was added after the first end-to-end run failed.** Driving exactly onto the reported pose left the robot on top of the cup, closer than the onboard camera's 8 cm minimum, so the `TrackObject` handoff could never work. The decorator now stops short along the robot-to-object line, which needed a `/robot_pose` subscription in the engine.
- **The camera service is now a hard dependency for every object-based tree.** With the ROS `VisualizeObject` action gone, nothing tells the ROS side where an object is, so `/get_object_pose` answers `'cup' has not been located yet` and `TrackObject`/`IsAtObject` have nothing to work with. `scripts/fake_camera.py` models the real thing: it answers the HTTP calls, publishes the pose, **and** announces the object on `/visualize_object` so the rest of ROS knows about it.
- Endpoints are all ROS parameters (`camera.base_url`, `camera.start_path`, `camera.status_path`, `camera.token`, `camera.timeout_ms`, `camera.goal_pose_topic`), settable while running, because the real contract is the camera team's to define. The status reply parser accepts `visualized`/`found`/`detected`/`visible`/`present` booleans or `status: "found"`; only the `visualized` shape has been exercised end to end.
- HTTPS works: httplib is now built with `CPPHTTPLIB_OPENSSL_SUPPORT`.
- Field size updated from 1.2 x 0.8 m to **1.8 x 1.2 m**.
- **All six examples were rewritten** — every one broke, because `VisualizeObject` as a decorator cannot be a leaf and the old "look, else rotate and look again" pattern no longer exists (the service does the looking; the tree polls). Verified in the simulator against the stub: `search_and_track` 28.9 s, `find_and_grab_cup` 34.4 s, `grab_cup_with_camera` 39.7 s, `nav_demo` 47.2 s all succeed; `find_missing_bottle` fails as designed; `bad_hallucinated` is rejected at validation.

## 2026-09-19: the search nodes, split properly
User's push-back on the first cut, and they were right: `TrackWhenFound` was doing two unrelated jobs
(polling the camera and driving to the object), which is why it needed four ports and an internal
state machine. Split into one node per job, with **standard BT control flow doing the sequencing**:
- `VisualizeObject` (action) starts the search over HTTP. `IsObjectFound` (condition) asks whether it has been seen, caching the answer for `poll_ms` so the 20 Hz tick does not hammer their service. `Patrol` is unchanged. `NavigateToDetectedObject` (action) drives to the published pose with the standoff.
- **`ReactiveFallback` replaces the custom decorator entirely** — it re-ticks the condition every tick, keeps the search running while it fails, and halts it the moment it succeeds. Nothing custom left in the control flow, and the search is now swappable: `Patrol`, or `Repeat num_cycles="-1"` around `RotateInPlace`, or a few `NavigateToPoint`s, without touching the rest.
- Bounding the search is now the built-in `Timeout`, which the old decorator had no way to express.
- **This flooded the trace**: a reactive condition at 20 Hz produced ~200 identical `IsObjectFound -> FAILURE` entries, burying the run in exactly the output the LLM reads back. The trace logger now collapses a run of identical transitions into one entry with a `repeated` count. Same run: **17 entries instead of 200+**, and nothing lost.
- Verified in the simulator: `search_and_track` 28.6 s, `find_and_grab_cup` 34.9 s, `grab_cup_with_camera` 39.0 s (with a 60 s `Timeout` around the search), all success; the other three behave as before.

## 2026-09-19: following a moving object
User's point on `NavigateToDetectedObject`: the goal pose gets updated, so the node has to expect that
and end up *around* the pose rather than driving to one fixed point.
- **Arrival is now judged by distance to the latest pose**, not by the navigation goal finishing: within `standoff + arrive_tolerance` (15 + 5 cm) and it succeeds, cancelling whatever goal is in flight. The old version could have waited for ever — each pose update cancels and re-sends the goal, so with a pose that keeps shifting no goal ever completes.
- Re-sends are rate-limited to `replan_min_interval_ms` (400 ms) so a jittery pose cannot thrash Nav2, and the aim point is recomputed from the robot's current position each tick so the standoff stays on the robot-to-object line as both move.
- **The first test passed for the wrong reason.** With the object drifting 2 cm/s the tree succeeded, but the log showed only *one* navigation goal — the stub published the pose only when polled over HTTP, and `IsObjectFound` stops being ticked once the `ReactiveFallback` succeeds, so the pose froze at the moment of discovery. That was my stub, not the node: the real camera publishes continuously. `fake_camera.py` now publishes on a 5 Hz timer, with `--drift` to move the object.
- Retested properly at 3 cm/s drift: seven goals, tracking the object out to `0.518 -> 0.573 -> 0.624 -> 0.677 -> 0.727 -> 0.770`, success in 29.6 s.
- All examples re-run: `search_and_track` 27.9 s, `find_and_grab_cup` 31.5 s, `grab_cup_with_camera` 35.4 s, `nav_demo` 47.0 s success; `find_missing_bottle` fails as designed; 23 unit tests pass.

## 2026-09-19: wired to the real camera service
Got the actual endpoints from the camera teammate, and they did not match my guesses.
- **`POST /api/query` takes `{"text": ...}`, not `{"object_name": ...}`**, and the status endpoint they gave me (`GET /api/target`) **404s** — I probed the server and found `GET /api/status` instead.
- Their status reply has no boolean at all: it is `recent[]`, newest last, each entry `{"status": "FOUND", "client_stamp": ...}`. The client now understands that shape, and `start_field`, `start_path`, `status_path`, `status_query_param` are all parameters, since guessing this twice was enough.
- **`recent` keeps old sightings for ever**, so a hit from twenty minutes ago would have read as FOUND. Added `camera.max_age_s` (3 s): a sighting older than that counts as not seen. This is not hypothetical — when I tested, the newest entry was **1072 s old** because their detection loop was not running, and the guard correctly said "not found".
- Verified against the live server from the laptop: `POST /api/query` accepted, and with the age guard relaxed `IsObjectFound` returned SUCCESS in 0.02 s off their real `recent[].status`.
- **`CAMERA_BASE_URL` is now a startup environment variable**, not a runtime parameter — same lesson as `MOCK_OBJECTS`: `ros2 param set` does not survive a container restart, and a silently unconfigured camera URL fails every search tree.

## 2026-09-19: three nodes dropped, two of them by accident first
- While wiring the camera API I found the deployed palette had **8 nodes, not 11**: `IsObjectVisible`, `IsAtObject` and `RecoveryNode` had vanished, along with `recovery_node.hpp` and the RecoveryNode arity check. `git show` on each commit pinned it to **73587b5** (the "follow the object pose" fix), where a block edit took more than it should have.
- **My verification is what let it through**: I ran the examples and they all passed, but none of them still used those three nodes, and I checked the palette before that commit rather than after. Running the examples is not the same as checking the contract.
- Offered to restore them; the user chose to **drop them for good**. Visibility is the camera service's business now (`IsObjectFound`), arrival is judged inside `NavigateToDetectedObject` and `TrackObject`, and recovery is expressible with the built-in `Fallback` / `RetryUntilSuccessful`. `recovery_node.hpp` deleted.
- **Palette is 8 robot nodes:** `VisualizeObject`, `IsObjectFound`, `Patrol`, `NavigateToDetectedObject`, `NavigateToPoint`, `TrackObject`, `RotateInPlace`, `SetGripper`.

## 2026-09-19: TrackObject dropped, palette page rebuilt
- **`TrackObject` is out of the palette** (user's call, temporarily). The reason it had stopped making sense: `/object_detections` has exactly one publisher in this repo, `sim_camera.py`, which only runs in simulation. On the deployed domain the topic has no publisher at all, because the camera team's service is HTTP plus a map-frame pose. The servo had nothing to servo on. `/track_object`, `diff_nav`'s `_execute_track` and `visual_servo.py` all stay; only the BT node is gone, so it returns the day `/object_detections` has a real publisher. **The approach now ends 15 cm out**, at `NavigateToDetectedObject`'s standoff, with no closed loop for the last stretch.
- Its `distance` preset check went with it -- it was the only node with that port, so the validation was dead code.
- **Palette page rewritten.** New layout, dark mode, proper type scale, coloured kind badges, per-card port tables with required ports marked. The content was as stale as the looks: the old "How to run it" section still showed `MOCK_DISABLE=navigate_to_object` and raw `docker compose` lines from before `./mc` existed. It now covers `./mc sim|run|validate|nodes|doctor|test`, the camera service settings, the stub, and a worked search tree. Verified headless with jsdom: 7 cards, grouped Actions (6) / Conditions (1), 6 required-port marks, 8 copy buttons.
- **`scripts/fake_camera.py` now mirrors the real API exactly**: `POST /api/query` with `{"text": ...}` and `GET /api/status` returning the `recent[]` shape with `client_stamp`. It also grew `--findable`, so an object that is simply not there can be tested -- `find_missing_bottle` was passing because the stub found anything you asked for.
- Two self-inflicted detours worth remembering: a stale `ws/build/bt_engine` made the link fail after `TrackObject` and `recovery_node` were removed (`rm -rf ws/build/bt_engine` fixed it), and `docker compose up -d engine` without `ROS_DOMAIN_ID=55` silently started the engine on domain 42, where it could see no action servers.
- Verified: `search_and_track` 23.8 s, `find_and_grab_cup` 28.2 s, `grab_cup_with_camera` 31.8 s, `nav_demo` 47.0 s success; `find_missing_bottle` fails at its 8 s timeout as designed; 23 unit tests pass.

## 2026-09-19: state at power-off
The mini PC is being shut down for the day. Left so it comes back on its own:
- **Containers** `engine` (port 8090), `mocks`, `map` (8081) all `unless-stopped`, so Docker restarts them when the daemon starts. Deliberately **not** stopped by hand — an explicit stop defeats `unless-stopped` and they would stay down after the reboot.
- **Cron** keeps the public URL alive: `expose.sh ensure` every 2 minutes (probes the real public endpoint, repairs the funnel) and `@reboot` restores it 90 s after boot.
- **Engine config that lives only on the `docker compose up` command line**, not in the repo: `ENGINE_PORT=8090`, `CAMERA_BASE_URL=http://192.168.50.125:8080`, the two tokens, `MOCK_OBJECTS`. The full line is in `docs/deploy.md`.
- **Palette is 20 nodes**: 7 robot (`VisualizeObject`, `IsObjectFound`, `Patrol`, `NavigateToDetectedObject`, `NavigateToPoint`, `RotateInPlace`, `SetGripper`) + 13 built-ins. Verified on the deployed engine, not just locally.
- Tidied a collision between two Claude sessions editing at once: both had claimed **C10** in the decisions log. `TrackObject` keeps C10; the built-in palette becomes **C11**, and the README cross-reference follows it.
- **Still outstanding for tomorrow:** the robot Pi has not had a deploy since the gripper and search work (it dropped off the network mid-afternoon and `ssh mcpi` goes through the mini PC, so it needs the PC on); `/object_detections` has no publisher, so the approach still ends 15 cm out; and nobody has run a search tree against the camera service while its detection loop is actually live.

## 2026-09-19: speed profiles
- **`speed="slow" | "normal" | "fast"`** on `NavigateToPoint`, `NavigateToDetectedObject` and `Patrol` (default `normal`). Asked for on "NavigateToObject and NavigateToPoint"; `NavigateToObject` has not been in the palette since C6, so I read it as the two navigation nodes that exist and added `Patrol` as well, since a patrol is a cruise rather than a dash.
- **Delivered through Nav2's own mechanism**: `diff_nav` publishes a `nav2_msgs/SpeedLimit` (percentage) on `/speed_limit`, which `controller_server` already subscribes to. No Nav2 parameters are touched, so nothing has to be put back if a goal is cancelled -- though the limit *is* reset to 100% when each goal ends, or one slow leg would quietly slow everything after it.
- Modelled on the `distance` presets: names are the contract, `speed_profile.slow|normal|fast` are engine parameters, live-tunable and rejected outside 0-100. Verified: setting `slow` to 15 succeeds, to 150 fails with "speed profiles are a percentage of the maximum, 0 < x <= 100".
- **Measured rather than assumed.** The same 1.0 m leg: `fast` **13.4 s**, `normal` **19.0 s**, `slow` **43.2 s**.
- **The pre-run validation went missing once and I caught it by testing.** `speed="turbo"` validated as `ok` because the other session's rewrite of `register_nodes.cpp` had dropped my check; the runtime message was still correct, but the LLM would only have learned about it after the robot started moving. Restored: validate now returns `node 'NavigateToPoint::1' has speed='turbo'; use one of: fast, normal, slow`.
- Interface change: `NavigateToPoint.action` gained `float32 speed_percent` (0 = leave the limit alone), so everything downstream needed a rebuild.
- All examples still behave: `find_and_grab_cup` 34.7 s, `grab_cup_with_camera` 36.2 s, `search_and_track` 24.4 s, `nav_demo` 62.3 s success, `find_missing_bottle` fails at its timeout, `bad_hallucinated` rejected; 23 unit tests pass.
- **Not deployed**: the mini PC is powered off. It needs a sync, a full rebuild (the action changed) and an engine restart when it is back.

## 2026-09-20: one domain, a smaller port surface, and a whole-mission example
- **Everything is on `ROS_DOMAIN_ID=59`** (was 42 deployed / 55 simulator). 59 is what the robot team's own containers on the Pi (`uros-agent`, `robot-localization`) were already using, so this is their number, not a new one. Defaults changed in `docker/compose.yaml`, `mc`, `scripts/sim.sh`, `scripts/doctor.sh`, plus the prose in README and docs.
- **Cross-machine DDS works for the first time.** From the mini PC's engine container: `/cubeMX_node` and both EKF nodes on the Pi; from the Pi: `/bt_engine`, `/mock_robot`, `/table_map`. Same node list from both sides.
- **The 42/55 split is gone, and with it the guarantee that the simulator cannot drive the real robot.** Nothing publishes `/cmd_vel` today (0 publishers, 1 subscriber) and the mock does not touch `/gripper`, so it is safe now -- but once Nav2 runs on the Pi, both it and the mock will offer `/navigate_to_point`. Stop `mocks` before driving for real. `/gripper` already shows two subscribers with **mismatched types** (`Int16` and `Int32`); our `SetGripper` publishes `Int32`, so that needs settling with the firmware owner.
- **After the mini PC's force shutdown, all three containers came back by themselves** (`restart: unless-stopped`) and the funnel came back via the `@reboot` cron -- but they were **deaf**: CycloneDDS had bound an Ethernet interface that no longer existed after the machine fell back to Wi-Fi, so `ros2 node list` returned nothing while `docker ps` looked healthy. A recreate fixed it. `restart: unless-stopped` cannot see this failure; it is worth remembering as a shape.
- Same reboot moved the PC to SSID `hackathon-duck` / `192.168.68.51`, so `192.168.50.125` is dead: `~/.ssh/config` now points `mcpc` at Tailscale (`100.113.211.14`), and `CAMERA_BASE_URL` is now `http://127.0.0.1:8080` (host networking, immune to the next IP change).
- **`Patrol` lost its geometry ports.** The rectangle is fixed in the source: 0.60 x 0.40 m centred at (0.90, 0.60), the same box the old defaults produced. Only `speed` remains, so a generated tree can no longer put patrol corners inside the wall inflation.
- **Three timing ports became engine parameters**: `camera_poll_ms` (500), `object_pose_timeout_ms` (5000), `replan_min_interval_ms` (400). `std::atomic<int>` on `RosContext`, live-settable while a tree runs, non-positive values refused. Verified both ways on the deployed engine.
- **`standoff` default is now 0.05 m** (was 0.15): the robot stops 5 cm short of the reported pose, close enough to grasp, so the approach is one stage rather than a fast leg plus a creep.
- Port removals are **breaking for already-generated trees**: BT.CPP rejects an unknown attribute outright (`a port with name [poll_ms] is found in the XML ... but not in the providedPorts()`). All examples and docs updated; the LLM needs to re-read `/nodes`.
- **New `examples/fetch_cup_mission.xml`**: the first end-to-end mission rather than a snippet. Five subtrees (prepare, find, pick up, deliver, give up safely), two search strategies in a `Fallback` (patrol, then centre-and-spin), a single `slow` approach that stops 5 cm short, and a recovery wrapped in `ForceFailure` so the robot ends up safe **and** the run still reports FAILURE.
- **`SubTree` static remapping works**, validated and at runtime: `<SubTree ID="Prepare" target="the white paper cup"/>` with `object_name="{target}"` inside. That is how the mission keeps the object name in one place.
- **Ran the whole mission against the live camera service and the mocks**: gripper opened, `VisualizeObject` reached the real VLM server, `IsObjectFound` polled (627 identical ticks collapsed into one trace entry), patrol ran 60 s, the `Timeout` handed over to the sweep strategy, that expired after 40 s, and `GiveUpSafely` opened the gripper, drove home and reported FAILURE at 103 s. Exactly the intended shape.
- The teammate's VLM server is back up on 8080 (`model_ready: true`), so search trees are unblocked -- but nothing has been *found* yet, so the approach and grasp half of the mission has still never run for real.
- **Still not deployed to the Pi.** It is reachable again (same subnet as the PC now), and it is several commits behind including the `NavigateToPoint` interface change.

## Next
- [ ] Build + run the tests on the mini PC (needs SSH access).
- [ ] Check DDS between the mini PC and the robot (same subnet, `ROS_DOMAIN_ID`).
- [ ] Give each teammate `robot_interfaces` and agree who owns each server.
- [ ] Swap mocks for real modules one at a time.
- [ ] Review N14–N18 (Nav2 choices) and merge `feat/diff-nav` into `main`.
- [ ] Get from teammates: motor driver topic + watchdog, camera pose topic/frame, `/table_map`, VLM `/get_object_pose`, robot size.
- [ ] Nav2 on the robot Pi: build the image there (arm64), then drive with RViz "2D Goal Pose" before using the BT.
- [ ] Give the LLM teammate `GET /nodes` + the response format for replanning.
