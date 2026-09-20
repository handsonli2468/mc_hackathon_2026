# Sending BT XML to the engine (for the LLM agent)

How the LLM agent hands a behavior tree to `bt_engine` and gets the result back.
Everything is plain HTTP + JSON; no ROS needed on the LLM side.

- **Base URL:** `https://mcpc.taile84e23.ts.net` (public). On the lab network: `http://192.168.50.125:8090`.
  On your own machine running the stack: `http://localhost:8080`.
- **Token:** the public engine requires one on every API call (see below). Ask kesler for it.
- **Format:** BehaviorTree.CPP **v4** XML
- **One tree at a time:** sending a new tree **stops the running one**

## Authentication

When the engine is reachable from the internet it runs with a token. Send it as a header on every API call:

```
X-BT-Token: <token>
```

or, where headers are awkward, as a query parameter: `/nodes?token=<token>`, `/execute?token=<token>`.

| Request | Without the token |
|---|---|
| `/nodes`, `/validate`, `/execute`, `/status`, `/cancel`, `/runs` | **401** `{"ok": false, "error": "missing or wrong token"}` |
| `/` (palette page), `/health` | fine — the page loads and asks for the token in the browser |

A 401 means the token is missing or wrong; it is never about the tree itself. Don't retry with a different
tree, fix the token.

```python
import requests
H = {"X-BT-Token": TOKEN}
r = requests.post(f"{BASE}/execute", json={"xml": xml}, headers=H)
st = requests.get(f"{BASE}/status/{r.json()['run_id']}", headers=H).json()
```

```bash
curl -H "X-BT-Token: $TOKEN" --data-binary @tree.xml https://mcpc.taile84e23.ts.net/execute
BT_ENGINE_URL=https://mcpc.taile84e23.ts.net BT_ENGINE_TOKEN=$TOKEN ./scripts/bt_exec.py run tree.xml
```

A local engine started without a token accepts everything, so the same code works with or without it
(sending an unnecessary token header is harmless).

## The loop

```
0. every request carries  X-BT-Token: <token>   (see Authentication)
1. GET  /nodes                 → put the node list in the LLM prompt (once, at startup)
2. LLM writes XML
3. POST /execute   (XML)       → 422? send "error" back to the LLM, go to 2
                               → 202  {run_id}
4. GET  /status/<run_id>       → poll every ~0.3 s until state != "running"
5. state == "success"          → done
   state == "failure"/"error"  → send notes + last_leaf_failure to the LLM, go to 2
```

## 1. Get the node list: `GET /nodes`

Returns XML (`TreeNodesModel`) describing every node the LLM may use and its parameters (ports).
Paste it into the system prompt so the LLM doesn't invent nodes.

| Query | Returns |
|---|---|
| `GET /nodes` | robot nodes **and** the 13 allowed BT.CPP built-ins (`Sequence`, `Fallback`, `RetryUntilSuccessful`, ...) |
| `GET /` | the same list as a **web page**, for humans (filter, built-ins toggle, copy for the prompt) |
| `GET /nodes?builtin=0` | robot nodes only (shorter prompt) |

```xml
<root BTCPP_format="4">
    <TreeNodesModel>
        <Action ID="TrackObject">
            <input_port name="object_name" type="std::string">Name of the target object, e.g. cup</input_port>
        </Action>
        ...
```

## 2. What the XML must look like

```xml
<root BTCPP_format="4" main_tree_to_execute="main">
  <BehaviorTree ID="main">
    <Sequence name="find_and_go">
      <VisualizeObject name="find_cup" object_name="cup"/>
      <TrackObject name="go_to_cup" object_name="cup" distance="near"/>
    </Sequence>
  </BehaviorTree>
</root>
```

`TrackObject` finishes the approach using the onboard camera, so it lines up with the real object
rather than the map estimate, and it is now **the only way to drive to an object by name**.
It needs the object in the camera's view and does not search by itself — on
`never saw the object`, rotate a little and retry (see `examples/grab_cup_with_camera.xml`).
`NavigateToPoint x y [yaw_deg]` drives to a coordinate in the map frame.

`TrackObject` takes `distance="contact" | "near" | "far"` (1 / 5 / 10 cm from the robot's front to the object's centre; default `near`). Any other value is rejected before the tree runs.

**`TrackObject` drives straight at the object and does not avoid obstacles.** `NavigateToPoint` is the
only node that plans around what is drawn on the map, so for anything further than the camera can see,
drive to a coordinate first and then track.

The gripper is a **0 .. 100 span**: 0 is fully open, 100 is fully closed. `SetGripper position="0"`
opens, `position="100"` closes, and anything in between is a grip that should not crush what it is
holding. A position outside 0..100 is rejected before the tree runs.
There is **no feedback from the gripper**, so no node can confirm a grasp worked: the wait inside
`SetGripper` is the only guarantee. Pick something up with
`SetGripper position="0"` → `TrackObject` → `SetGripper position="100"`.

**Finding an object is a search.** Four nodes, each doing one thing, wired with ordinary BT control
flow — there is nothing custom about the structure:

```xml
<Sequence>
  <VisualizeObject object_name="cup"/>              <!-- ask the camera to start looking -->

  <ReactiveFallback>                                <!-- search until it reports the object -->
    <IsObjectFound object_name="cup"/>
    <Patrol/>
  </ReactiveFallback>

  <NavigateToDetectedObject standoff="0.05"/>       <!-- drive to where it says it is -->
  <SetGripper position="100"/>
</Sequence>
```

`ReactiveFallback` re-ticks `IsObjectFound` every tick; while it fails the search keeps running, and
the moment it succeeds the fallback halts the search and moves on. Swap `Patrol` for any other search
— `<Repeat num_cycles="-1"><RotateInPlace angle_deg="45"/></Repeat>` turns on the spot — without
touching anything else.

- **`VisualizeObject object_name`** — one HTTP request to the camera service to start looking.
  Succeeds as soon as the request is accepted; it does not wait for a result.
- **`IsObjectFound object_name`** — asks the camera service whether it has seen the object.
  The answer is cached for `camera_poll_ms` (an engine parameter, 500 ms), so ticking it at 20 Hz
  does not hammer their service.
- **`Patrol [speed]`** — walks a fixed rectangle clockwise, for ever: a 0.60 x 0.40 m box centred at
  (0.90, 0.60) on the 1.8 x 1.2 m field. The area is not adjustable from the tree.
  It never succeeds on its own, so it only makes sense under something that stops it.
- **`NavigateToDetectedObject [standoff arrive_tolerance replan_distance speed]`** — drives to where
  the camera says the object is **now**. The pose keeps being republished and can move, so this
  follows it: when the target shifts more than `replan_distance` (5 cm) it sends a fresh goal, at
  most every `replan_min_interval_ms` (an engine parameter, 400 ms) so a jittery pose cannot thrash
  Nav2. If the camera publishes no pose at all within `object_pose_timeout_ms` (15 s) it fails.
  It stops `standoff` metres short (0.05) so it does not drive onto the object, and
  **succeeds when the robot is within `standoff + arrive_tolerance` of the latest pose**,
  not when a navigation goal finishes — with a moving pose, waiting for one goal to complete could
  wait for ever. This is the end of the approach: there is no closer node at the moment.

**Bound the search** with the built-in `Timeout`, or a patrol with nothing to find runs until the run
is cancelled: `<Timeout msec="60000"><ReactiveFallback>...</ReactiveFallback></Timeout>`. The same
applies to the approach if the object keeps moving away faster than the robot drives.

**Speed** is a named profile on every node that drives: `speed="slow" | "normal" | "fast"`, default
`normal`. It becomes a share of Nav2's configured maximum (30% / 70% / 100%), published as a
`nav2_msgs/SpeedLimit`, and full speed is restored when the goal ends — so one slow leg cannot slow
down everything after it. Any other name is rejected before the tree runs. `NavigateToPoint`,
`NavigateToDetectedObject` and `Patrol` all take it.

Rules (checked before anything runs):
- Only nodes from `/nodes`. Parameter names must match exactly (`object_name`, not `object`).
- Most BT.CPP built-ins are **not** in the palette. The allowed ones are `Sequence`, `Fallback`, `ReactiveSequence`, `ReactiveFallback`, `Repeat`, `RetryUntilSuccessful`, `Timeout`, `Delay`, `Inverter`, `ForceSuccess`, `ForceFailure`, `KeepRunningUntilFailure`, `SubTree`; anything else (`Script`, `SetBlackboard`, `Switch*`, `Parallel`, `Sleep`, `AlwaysSuccess`, ...) is rejected with `uses 'X', which is not in the palette`.
- Required parameters (no default in `/nodes`) must be given.
- With more than one `<BehaviorTree>`, `main_tree_to_execute` must name the one to run.
- **Give every node a `name=`.** Without it, results refer to nodes as `Type::N` (e.g. `VisualizeObject::2`), which is much harder to read.

Full example: [`examples/find_and_grab_cup.xml`](../examples/find_and_grab_cup.xml).

## 2b. What the LLM actually fills in

`GET /nodes` says which ports *exist*. This says which ones **you must write**, and what kind of
value each one takes. Ports are marked:

- **`[SET]`** — no default; the tree is rejected at load time if you leave it out.
- **`[DEFAULT]`** — has a default; write it only when you mean something other than the default.
- **`[NAME]`** — not a port at all, but an attribute you should always write (see the rules above).

Value kinds: **number** (a bare numeral, no units, no quotes around a formula — `0.05`, not `5cm`),
**word** (one of a fixed set), **text** (free English, which is what the camera team's model reads),
**variable** (`{key}`, a blackboard key filled in by a `SubTree` remap), **id** (must match a
`<BehaviorTree ID="...">` in the same file).

```xml
<TreeNodesModel>

  <!-- ======================= robot nodes ======================= -->

  <Action ID="SetGripper">
    <!-- name                [NAME]    e.g. "close_gripper"                                 -->
    <input_port name="position" type="int">
      [SET] number 0-100. 0 = fully open, 100 = fully closed. Integer, not 0.0.
    </input_port>
  </Action>

  <Action ID="VisualizeObject">
    <!-- name                [NAME]    e.g. "start_camera_search"                           -->
    <input_port name="object_name" type="std::string">
      [SET] text or variable. Free English the camera model reads: "the white paper cup".
      In the mission tree this is the variable {target}, set once by the SubTree remap.
    </input_port>
  </Action>

  <Condition ID="IsObjectFound">
    <!-- name                [NAME]    e.g. "found_while_patrolling"                        -->
    <input_port name="object_name" type="std::string">
      [SET] text or variable. Use the SAME text as VisualizeObject, or the same {variable}.
    </input_port>
  </Condition>

  <Action ID="Patrol">
    <!-- name                [NAME]    e.g. "patrol_middle"                                 -->
    <input_port name="speed" type="std::string" default="normal">
      [DEFAULT] word: slow | normal | fast. Nothing else to set - the patrol rectangle is
      fixed in the engine (0.60 x 0.40 m centred on (0.90, 0.60)).
    </input_port>
  </Action>

  <Action ID="NavigateToPoint">
    <!-- name                [NAME]    e.g. "go_to_dropoff"                                 -->
    <input_port name="x" type="double">
      [SET] number, metres, map frame. Field is 0..1.8.
    </input_port>
    <input_port name="y" type="double">
      [SET] number, metres, map frame. Field is 0..1.2.
    </input_port>
    <input_port name="yaw_deg" type="std::string" default="">
      [DEFAULT] number as text, degrees, 0 = +x. Empty = face the way you drove.
    </input_port>
    <input_port name="speed" type="std::string" default="normal">
      [DEFAULT] word: slow | normal | fast.
    </input_port>
  </Action>

  <Action ID="NavigateToDetectedObject">
    <!-- name                [NAME]    e.g. "drive_to_cup"                                  -->
    <!-- No object_name: it drives to whatever VisualizeObject started, so all four ports
         are [DEFAULT] and the node is usable as <NavigateToDetectedObject name="..."/>.   -->
    <input_port name="standoff" type="double" default="0.050000">
      [DEFAULT] number, metres. Stop this far short of the reported pose.
    </input_port>
    <input_port name="arrive_tolerance" type="double" default="0.050000">
      [DEFAULT] number, metres. Succeeds within standoff + this.
    </input_port>
    <input_port name="replan_distance" type="double" default="0.050000">
      [DEFAULT] number, metres. Re-send the goal when the pose moves more than this.
    </input_port>
    <input_port name="speed" type="std::string" default="normal">
      [DEFAULT] word: slow | normal | fast.
    </input_port>
  </Action>

  <Action ID="RotateInPlace">
    <!-- name                [NAME]    e.g. "turn_45"                                       -->
    <input_port name="angle_deg" type="double" default="45.000000">
      [DEFAULT] number, degrees, positive = counter-clockwise.
    </input_port>
  </Action>

  <!-- ==================== control flow used ==================== -->

  <Control ID="Sequence"/>          <!-- name [NAME] only. All children must succeed, in order. -->
  <Control ID="Fallback"/>          <!-- name [NAME] only. First child that succeeds wins.      -->
  <Control ID="ReactiveFallback"/>  <!-- name [NAME] only. Re-ticks earlier children every tick. -->

  <Decorator ID="Timeout">
    <input_port name="msec" type="unsigned int">
      [SET] number, whole milliseconds. 60000 is one minute, not 60. A fractional value
      is NOT caught by /validate but can fail when the node ticks, so write integers.
    </input_port>
  </Decorator>

  <Decorator ID="Repeat">
    <input_port name="num_cycles" type="int">
      [SET] number. -1 = for ever (only safe under a Timeout or a ReactiveFallback).
    </input_port>
  </Decorator>

  <Decorator ID="RetryUntilSuccessful">
    <input_port name="num_attempts" type="int">
      [SET] number of tries before giving up. -1 = for ever.
    </input_port>
  </Decorator>

  <Decorator ID="ForceFailure"/>    <!-- name [NAME] only. Runs the child, reports FAILURE anyway. -->

  <!-- Packaged places: GoHome, GoToStorage, GoToCentre, GoToVantageNW/NE/SE/SW,
       ScanFromCentre, TourTheCorners. The engine already defines these, so a tree only
       CALLS them: <SubTree ID="GoHome" name="go_home"/>. There is NO import line to write -
       no <include>, no file path, nothing. Never redefine one (the engine drops a
       redefinition and says so), and never write the coordinates by hand.
       See docs/map_places.md. -->

  <SubTree ID="SubTree">
    <!-- ID                  [SET] id, must match a <BehaviorTree ID="..."> in the same file -->
    <!-- name                [NAME]    e.g. "find_it"                                        -->
    <!-- <any>="<value>"     [SET] variable binding: the attribute name is the {key} used
             inside that subtree, the value is the text bound to it. This is how the mission
             tree writes target="the white paper cup" once and reads {target} in two places. -->
    <input_port name="_autoremap" type="bool" default="false">
      [DEFAULT] leave it out; bind the keys you need by name instead.
    </input_port>
  </SubTree>

</TreeNodesModel>
```

**Summary of everything the LLM must decide** for a mission like `examples/fetch_cup_mission.xml`:

| What | Where | Kind | In the example |
|---|---|---|---|
| The object, once | `SubTree target=` | text | `the white paper cup` |
| The object, at each use | `object_name=` | variable | `{target}` |
| Gripper open / closed | `SetGripper position=` | number 0-100 | `0` then `100` |
| Where to drive | `NavigateToPoint x= y=` | numbers, metres | `1.55`, `0.25` |
| Facing on arrival | `NavigateToPoint yaw_deg=` | number, degrees | `0`, `180` |
| How fast each leg is | `speed=` | word | `slow` / `normal` |
| How long each phase may take | `Timeout msec=` | number, ms | `45000`, `40000`, `30000` |
| How many retries | `RetryUntilSuccessful num_attempts=` | number | `3` |
| Turn size when sweeping | `RotateInPlace angle_deg=` | number, degrees | `45` |
| A readable name for every node | `name=` | identifier | `patrol_middle` |

Everything else — the patrol rectangle, poll intervals, replan timing — is engine configuration and
is **not** the LLM's to choose.

## 3. Send it: `POST /execute`

Body is **either** the raw XML **or** JSON `{"xml": "<root ...>"}` (JSON is easier from Python, since there's no escaping to worry about).

**You don't say which:** the engine looks at the first character of the body that isn't whitespace. `{` means JSON (it reads the `xml` field), anything else means raw XML. The `Content-Type` header is ignored, so either form works with or without one.

```bash
curl -X POST --data-binary @tree.xml http://localhost:8080/execute
```
```python
import requests
r = requests.post(f"{BASE}/execute", json={"xml": xml})
```

| HTTP | Body | Meaning |
|---|---|---|
| **202** | `{"ok": true, "run_id": "run-1", "preempted_previous": false}` | Started. `preempted_previous: true` = a running tree was stopped. |
| **422** | `{"ok": false, "error": "Error at line 7: -> Node not recognized: FlyToObject"}` | Invalid tree, **nothing ran**, and any running tree keeps going. Give `error` to the LLM. |
| **400** | `{"ok": false, "error": "empty body; send BT XML"}` | Bad request (empty body, JSON without `xml`). A bug in the agent, not the LLM. |
| **401** | `{"ok": false, "error": "missing or wrong token"}` | The token is missing or wrong. Nothing ran. |

To check a tree without running it, send the same body to **`POST /validate`**: 200 `{"ok": true}` or 422 with `error`.

## 4. Follow it: `GET /status/<run_id>`

`GET /status` (no id) returns the latest run. An unknown id, **and `/status` before any tree has been sent**, both give 404 `{"ok": false, "error": "no such run"}`.

While running, the reply has the same fields as a finished one **except `finished_at`**, and:
- `trace` and `last_leaf_failure` **build up live**, so you can follow progress;
- `notes` stays **empty until the run finishes** (the reasons are collected at the end);
- `elapsed_s` counts up.

```json
{
  "run_id": "run-1",
  "state": "running",
  "running_leaves": ["look"],
  "elapsed_s": 2.01,
  "notes": [],
  "last_leaf_failure": null,
  "trace": [ ... so far ... ],
  "trace_truncated": false,
  "started_at": 1789712930.12
}
```

Finished (real output, looking for an object that doesn't exist):
```json
{
  "run_id": "run-1",
  "state": "failure",
  "elapsed_s": 1.06,
  "running_leaves": [],
  "last_leaf_failure": {"node": "VisualizeObject::2", "type": "VisualizeObject", "from": "RUNNING", "to": "FAILURE", "t": 1.06},
  "notes": [
    {"node": "VisualizeObject::2", "message": "'bottle' not found in view"}
  ],
  "trace": [
    {"t": 0.003, "node": "Sequence::1", "type": "Sequence", "from": "IDLE", "to": "RUNNING"},
    {"t": 0.003, "node": "VisualizeObject::2", "type": "VisualizeObject", "from": "IDLE", "to": "RUNNING"},
    {"t": 1.06, "node": "VisualizeObject::2", "type": "VisualizeObject", "from": "RUNNING", "to": "FAILURE"},
    {"t": 1.06, "node": "Sequence::1", "type": "Sequence", "from": "RUNNING", "to": "FAILURE"}
  ],
  "trace_truncated": false,
  "started_at": 1789653030.08,
  "finished_at": 1789653031.14
}
```

| Field | Use |
|---|---|
| `state` | `running` · `success` · `failure` (the tree returned FAILURE) · `canceled` (stopped by `/cancel` or a newer tree) · `error` (exception while running, see `error`) |
| `running_leaves` | What the robot is doing now, handy for showing progress in a UI |
| `notes` | **Why** leaves failed, in words. The most useful thing to give the LLM when it replans. |
| `last_leaf_failure` | The last action/condition that failed |
| `trace` | Status changes, last 40 by default; `?trace=full` returns up to 1000 |
| `error` | **Only present when `state` is `error`**: the exception text, as a string |

**`state: "error"`** means the tree threw while running (a bug in a node, not a robot failure). The robot is halted. `error` holds the text; `notes` and `last_leaf_failure` may well be empty, because nothing failed normally:

```json
{
  "run_id": "run-1",
  "state": "error",
  "error": "Exception in node 'Script::1' [Script]: Error in script [a:=b+1]\nVariable not found: b",
  "notes": [],
  "last_leaf_failure": null,
  "trace": [],
  "elapsed_s": 0.02
}
```

**Object names are free text.** Against the mock, any name works: `blue bottle`, `red cup`, whatever
the user asked for — the fake robot invents a position for an unknown object (the same name always
lands in the same place). With the real VLM, only objects it can actually see will be found, and the
failure message says so.

A condition that's simply false (e.g. `IsObjectFound` inside a `Fallback`) shows up in the trace but has no `notes` entry; that's normal control flow.

## 5. Other calls

| Call | Result |
|---|---|
| `POST /cancel` | Stops the running tree and cancels robot goals → 200 `{"ok": true, "was_running": true}`. With nothing running it is **not an error**: 200 with `"was_running": false`. |
| `GET /runs` | Last 20 runs: `[{run_id, state, started_at}, ...]` |
| `GET /health` | `{"ok": true}` |

CORS is open (`Access-Control-Allow-Origin: *`), so a browser UI can call the API directly.

## Errors, and more than one client

- **Every reply is JSON**, including ones you didn't expect: an unknown path or method gives 404 `{"ok": false, "error": "no such endpoint"}`, and an unexpected failure inside the engine gives **500** `{"ok": false, "error": "<what went wrong>"}`. A 500 means a bug in the engine, not a robot failure; the run it was handling may be in any state, so read `/status` before deciding what to do.
- **401 on everything:** the token is missing or wrong — not a problem with the tree.
- **Engine not running:** the connection is refused or times out. There is no "not ready" status: the port only opens once the engine is up, so `GET /health` returning `{"ok": true}` means it's ready.
- **Two `/execute` calls at once** are handled one after another: each valid tree stops the one before it, and the second reply has `"preempted_previous": true`. Nothing is ever run in parallel.
- **Polling `/status`** is read-only and safe from as many clients as you like (a UI and the agent together, for instance). Each reply is a snapshot, so two clients can see slightly different `elapsed_s` values.

## What to send back to the LLM on failure

A short message is better than the whole trace:

```
Your tree failed.
Failed node: VisualizeObject::2 (VisualizeObject)
Reasons:
- VisualizeObject::2: 'bottle' not found in view
Write a corrected tree.
```

For a 422, send the `error` string and your original XML.

## Reference client

[`scripts/bt_exec.py`](../scripts/bt_exec.py) implements the whole loop (standard library only):

```bash
./scripts/bt_exec.py run tree.xml       # execute + follow + print final status
./scripts/bt_exec.py validate tree.xml
./scripts/bt_exec.py nodes --custom
BT_ENGINE_URL=http://<mini-pc-ip>:8080 ./scripts/bt_exec.py status
```
