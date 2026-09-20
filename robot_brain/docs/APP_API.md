# App API

Base URL on Manta: `http://<manta-host>:8000` (or the reverse-proxy URL used by the UI).

FastAPI also publishes interactive OpenAPI documentation at `/docs` and the machine-readable schema at `/openapi.json`.

## 1. Send a task / continue a conversation

`POST /api/chat`

Request content type: `application/json`. The normal App integration should send:

```json
{
  "message": "找到杯子，靠近它，然後用 60% 夾爪閉合",
  "session_id": null,
  "pipeline_mode": "hybrid",
  "world_state": {}
}
```

- `message`: required non-empty string; it may be a new task, normal chat, or an answer to a clarification.
- Omit `session_id` or send `null` on the first turn. Save the returned `session_id` and reuse it for later turns.
- `pipeline_mode` defaults to `hybrid`. The App should use `hybrid`; `direct` and `compare` are development modes.
- `world_state` is an optional JSON object containing current trusted runtime facts. Send `{}` when there are none.
- `options.allow_vision` is an optional reserved compatibility field and currently has no effect.
- `options.auto_execute` defaults to `true`. Normal App requests should omit it or send `true`. Automated planning/profile tests send `false` so they cannot move the robot.
- Unknown fields are rejected with HTTP 422.

For `hybrid`, HTTP 200 has this stable envelope. Additional diagnostics may appear inside `candidate`:

```json
{
  "session_id": "7bf...",
  "pipeline_mode": "hybrid",
  "candidate": {
    "pipeline": "hybrid",
    "status": "SUCCESS",
    "message": "TaskPlanIR → BTGenBot-2 produced a BehaviorTree that passed deterministic validation.",
    "questions": [],
    "mission_id": "5d1...",
    "bt_xml": "<root BTCPP_format=\"4\" ...>...</root>",
    "bt_generation": {
      "succeeded": true,
      "validated": true,
      "message": "BehaviorTree XML generated and passed deterministic validation."
    },
    "auto_execution": {
      "enabled": true,
      "attempted": true,
      "started": true,
      "status": "STARTED",
      "mission_id": "5d1...",
      "run_id": "engine-run-id",
      "engine_state": "running",
      "preempted_previous": false,
      "poll_interval_s": 0.3,
      "reason": null,
      "error": null
    },
    "bt_validation": {"valid": true, "errors": []}
  },
  "candidates": null,
  "conversation_grounding": {}
}
```

The App should distinguish generation from execution using:

- `candidate.bt_generation.succeeded`: XML exists, was saved, and passed deterministic Agent validation.
- `candidate.bt_generation.message`: explicit human-readable BT generation result.
- `candidate.auto_execution.status`: `STARTED`, `FAILED`, or `SKIPPED`.

After successful generation, the Agent immediately submits the XML to the BT Engine. `STARTED` means the engine accepted it and returned `run_id`; it does not mean physical execution has finished. If submission fails, BT generation remains visible as successful while `auto_execution.status` is `FAILED` and `auto_execution.error` contains the integration error.

No XML is executed for `NEED_MORE_INFO`, `PLANNING_FAILURE`, `UNSUPPORTED`, `UNSAFE`, or `INVALID_REQUEST`. A clarification supplies `candidate.questions`; send the answer with the same session ID.

`compare` returns `candidates.direct` and `candidates.hybrid` instead of `candidate`. It deliberately executes neither tree, preventing an architecture comparison from starting two robot missions.

Minimal App-side handling:

```javascript
const response = await fetch(`${baseUrl}/api/chat`, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({message, session_id: sessionId, pipeline_mode: "hybrid", world_state: {}})
});
if (!response.ok) throw new Error(await response.text());
const data = await response.json();
sessionId = data.session_id;

const result = data.candidate;
if (!result.bt_generation.succeeded) {
  // Show result.message/questions; no robot command was sent.
} else if (result.auto_execution.status === "STARTED") {
  const {mission_id, run_id} = result.auto_execution;
  // Subscribe to /ws/{sessionId} and/or poll engine/status for this mission.
} else {
  // XML exists, but engine dispatch was skipped or failed.
  console.error(result.auto_execution.reason, result.auto_execution.error);
}
```

`POST /api/chat` represents a new task submission and is not an idempotent retry endpoint. Because a successful request starts physical execution, the App should disable duplicate submission while the request is pending and must not blindly retry after an ambiguous network timeout.

## 2. Reset / start a new session

`POST /api/sessions/reset`

```json
{"previous_session_id": "7bf..."}
```

Response:

```json
{
  "session_id": "new-uuid",
  "previous_session_id": "7bf...",
  "history_carried_over": false
}
```

The old history is preserved for audit; the returned ID starts a clean conversation.

## 3. Submit post-execution feedback to Experience RAG

Call this only after the mission's BT Engine state is `success`, `failure`, `error`, or `canceled`.

`POST /api/missions/{mission_id}/feedback`

```json
{
  "rating": 4,
  "comment": "杯子有夾住，但可以再輕一點；靠近杯子時也應該慢一點。",
  "parameters": {
    "set_gripper_position": 55,
    "navigate_to_detected_object_speed": "slow",
    "navigate_to_point_speed": "normal"
  }
}
```

Validation:

- `rating`: required integer, 1–5.
- `comment`: optional free text, at most 4000 characters.
- `set_gripper_position`: optional integer, 0–100 (`0` fully open, `100` fully closed).
- Both speed fields are optional and accept only `slow`, `normal`, or `fast`.

The API saves/upserts a JSON file at `agent-runtime/state/experience-feedback/{mission_id}.json` and indexes a readable summary in Experience RAG. Later similar tasks can retrieve it as advisory parameter experience. Re-submitting for the same mission updates that mission's feedback instead of creating duplicates.

Read it back with `GET /api/missions/{mission_id}/feedback`.

## Execution and monitoring endpoints

```text
POST /api/missions/{mission_id}/engine/validate
POST /api/missions/{mission_id}/engine/execute
GET  /api/missions/{mission_id}/engine/status?refresh=true
POST /api/missions/{mission_id}/engine/cancel
GET  /api/missions/{mission_id}
GET  /api/missions/{mission_id}/bt.xml
```

Normal App flow must not call `engine/validate` or `engine/execute`; `/api/chat` now validates and starts successful missions automatically. Those endpoints remain for diagnostics and backward compatibility. Calling `engine/execute` again may start or preempt a run, so it must not be used as a retry for `/api/chat`.

For live status, connect a WebSocket to `/ws/{session_id}`. Messages include `execution_event`, `bt_engine_terminal`, and `bt_engine_monitor_error`. Also call `GET /api/missions/{mission_id}/engine/status?refresh=true` after receiving the chat response; this recovers state if a short run finished before the WebSocket connected.

Typical HTTP errors are `404` unknown mission, `409` feedback before terminal execution, and `422` invalid rating/parameter values.
