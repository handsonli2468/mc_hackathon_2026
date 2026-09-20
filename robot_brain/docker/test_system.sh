#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:8000}"

echo "[1/7] health"
curl -fsS "${BASE}/health" | jq .

echo "[2/7] info"
curl -fsS "${BASE}/api/info" | jq .

echo "[3/7] RAG status"
OUT=$(curl -fsS "${BASE}/api/rag/status")
echo "$OUT" | jq .
echo "$OUT" | jq -e '.enabled == true and (.documents.skill // 0) > 0' >/dev/null

echo "[4/7] skills"
curl -fsS "${BASE}/api/skills" | jq '.version, (.skills | length)'

echo "[5/7] frontend"
curl -fsS "${BASE}/" | grep -q 'Robot Brain Comparator'

echo "[6/7] deterministic rejection of unknown node"
OUT=$(curl -fsS -X POST "${BASE}/api/validate-bt" -H 'Content-Type: application/json' -d '{"xml":"<root main_tree_to_execute=\"Main\"><BehaviorTree ID=\"Main\"><FlyToMoon name=\"x\"/></BehaviorTree></root>"}')
echo "$OUT" | jq .
echo "$OUT" | jq -e '.valid == false' >/dev/null

echo "[7/7] current-contract valid tree"
OUT=$(curl -fsS -X POST "${BASE}/api/validate-bt" -H 'Content-Type: application/json' -d '{"xml":"<root BTCPP_format=\"4\" main_tree_to_execute=\"Main\"><BehaviorTree ID=\"Main\"><SetGripper name=\"open_gripper\" position=\"0\"/></BehaviorTree></root>"}')
echo "$OUT" | jq .
echo "$OUT" | jq -e '.valid == true' >/dev/null

echo "System smoke tests: PASS"
