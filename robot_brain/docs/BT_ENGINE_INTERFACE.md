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

The Agent performs this loop automatically after a successful normal `/api/chat` planning response; the App does not call `/validate` or `/execute` itself.

```
0. every request carries  X-BT-Token: <token>   (see Authentication)
1. GET  /nodes?builtin=0       → Agent startup syncs the formal node contract and rebuilds the semantic Skill Registry/RAG
2. LLM writes XML
3. POST /execute   (XML)       → 422? send "error" back to the LLM, go to 2
                               → 202  {run_id}
4. GET  /status/<run_id>       → poll every ~0.3 s until state != "running"
5. state == "success"          → done
   state == "failure"/"error"  → send notes + last_leaf_failure to the LLM, go to 2
```

## 1. Get the node list: `GET /nodes`

Returns XML (`TreeNodesModel`) describing the live engine nodes and their formal ports. v6.3 does **not** paste the raw engine model into a site-specific system prompt. Startup sync stores the formal contract, merges reviewed local semantic overlays, applies `bt_skill_policy.yaml`, and then indexes only planner-enabled skills into Skill RAG.

| Query | Returns |
|---|---|
| `GET /nodes` | robot nodes **and** BT.CPP built-ins (`Sequence`, `Fallback`, `RetryUntilSuccessful`, ...) |
| `GET /` | the same list as a **web page**, for humans (filter, built-ins toggle, copy for the prompt) |
| `GET /nodes?builtin=0` | robot nodes only (shorter prompt) |

```xml
<root BTCPP_format="4">
  <TreeNodesModel>
    <Action ID="SomeLiveNode">
      <input_port name="some_port" type="std::string">Formal engine description</input_port>
    </Action>
    ...
  </TreeNodesModel>
</root>
```

The exact current `SetGripper` interface must come from this live response; the Agent intentionally does not guess its ports or command values.

## 2. What the XML must look like

```xml
<root BTCPP_format="4" main_tree_to_execute="main">
  <BehaviorTree ID="main">
    <Sequence name="find_and_go">
      <VisualizeObject name="find_cup" object_name="cup"/>
      <Timeout name="find_cup_timeout" msec="60000">
        <ReactiveFallback name="find_cup_monitor">
          <IsObjectFound name="confirm_cup" object_name="cup"/>
          <Patrol name="patrol_while_searching"/>
        </ReactiveFallback>
      </Timeout>
      <NavigateToDetectedObject name="go_to_cup" speed="normal" standoff="0.05"/>
    </Sequence>
  </BehaviorTree>
</root>
```

`NavigateToDetectedObject` follows the pose selected by the immediately preceding perception flow. Its current live inputs are `speed`, `replan_distance`, `arrive_tolerance`, and `standoff`; pose timeout and replanning cadence are engine-owned in this ABI. `SetGripper.position` is 0–100, where 0 fully opens the jaws.

`VisualizeObject` triggers continuous camera search. `ReactiveFallback` polls `IsObjectFound` first on every tick while `Patrol` runs as its second branch. Search must finish successfully before `NavigateToDetectedObject` follows the latest published pose.

Rules (checked before anything runs):
- Only nodes from `/nodes`. Parameter names must match exactly (`object_name`, not `object`).
- Required parameters (no default in `/nodes`) must be given.
- Decorators (`Timeout`, `RetryUntilSuccessful`, `ForceFailure`, etc.) require exactly one child.
- With more than one `<BehaviorTree>`, `main_tree_to_execute` must name the one to run.
- **Give every node a `name=`.** Without it, results refer to nodes as `Type::N` (e.g. `VisualizeObject::2`), which is much harder to read.

Full example: [`examples/find_and_grab_cup.xml`](../examples/find_and_grab_cup.xml).

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

Finished failure payloads have this general shape. With continuous search, final leaf/notes may identify `IsObjectFound` or `Patrol`; a search `Timeout` then propagates FAILURE through the mission Sequence:
```json
{
  "run_id": "run-1",
  "state": "failure",
  "elapsed_s": 1.06,
  "running_leaves": [],
  "last_leaf_failure": {"node": "confirm_bottle", "type": "IsObjectFound", "from": "IDLE", "to": "FAILURE", "t": 60.0},
  "notes": [
    {"node": "confirm_bottle", "message": "'bottle' not found before search timeout"}
  ],
  "trace": [
    {"t": 0.003, "node": "Sequence::1", "type": "Sequence", "from": "IDLE", "to": "RUNNING"},
    {"t": 0.003, "node": "find_bottle", "type": "VisualizeObject", "from": "IDLE", "to": "SUCCESS"},
    {"t": 0.004, "node": "patrol_while_searching", "type": "Patrol", "from": "IDLE", "to": "RUNNING"},
    {"t": 60.0, "node": "mission_sequence", "type": "Sequence", "from": "RUNNING", "to": "FAILURE"}
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
Failed node: confirm_bottle (IsObjectFound)
Reasons:
- confirm_bottle: 'bottle' not found before search timeout
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

---

## Robot Brain v6.1 synchronization behavior

The BT Engine API contract above is unchanged. Robot Brain v6.1 consumes it more strictly:

1. Every Agent process start/restart requests `GET /nodes?builtin=0` before planning.
2. Every returned custom node is persisted in `config/bt_engine_registry.generated.yaml` with its formal ID/kind/ports/types/defaults.
3. `config/bt_engine_semantic_overlay.yaml` supplies planner semantics that `/nodes` cannot provide.
4. Only Action/Condition nodes with a valid, planner-enabled overlay are merged into `config/skill_registry.yaml` and Skill RAG.
5. Unknown or invalid semantic nodes are never silently discarded: they remain in the formal registry and are listed in the sync report/review scaffold.
6. A manual refresh is available at `POST /api/bt-engine/sync-nodes`; the latest report is at `GET /api/bt-engine/sync-status`.

The engine therefore remains the source of truth for executable node contracts, while semantic usage policy remains explicit and reviewable on the Agent side.
