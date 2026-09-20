#!/usr/bin/env bash
set -euo pipefail
BASE="${ROBOT_BRAIN_URL:-http://127.0.0.1:8000}"
FIRST="$(curl -s -X POST "${BASE}/api/chat" -H 'Content-Type: application/json' -d '{"pipeline_mode":"hybrid","message":"bring the baseball to me","options":{"auto_execute":false}}')"
SID="$(jq -r '.session_id' <<<"${FIRST}")"
echo '=== TURN 1 ==='
jq '{session_id,status:.candidate.status,message:.candidate.message,questions:.candidate.questions}' <<<"${FIRST}"
SECOND="$(curl -s -X POST "${BASE}/api/chat" -H 'Content-Type: application/json' -d "$(jq -nc --arg sid "$SID" '{pipeline_mode:"hybrid",session_id:$sid,message:"I am beside the table",options:{auto_execute:false}}')")"
echo '=== TURN 2 ==='
jq '{status:.candidate.status,conversation_grounding:.candidate.conversation_grounding,compact_planner_used:.candidate.compact_planner_used,planner_escalated:.candidate.planner_escalated,requirement_normalization:.candidate.requirement_normalization,grounding_policy:.candidate.grounding_policy,total_sec:.candidate.timing.total_elapsed_sec,mission_id:.candidate.mission_id}' <<<"${SECOND}"
echo '=== XML recipient nodes ==='
jq -r '.candidate.bt_xml // ""' <<<"${SECOND}" | grep -o 'object_name="[^"]*"' | sort -u || true
