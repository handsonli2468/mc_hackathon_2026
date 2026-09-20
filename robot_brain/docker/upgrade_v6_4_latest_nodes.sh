#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$PROJECT_ROOT/agent-runtime}"
CFG="$RUNTIME_ROOT/config"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP="$RUNTIME_ROOT/config-backups/v6.4-$STAMP"
mkdir -p "$BACKUP" "$CFG/prompts" "$RUNTIME_ROOT/rag-knowledge/patterns" "$BACKUP/rag-patterns" "$BACKUP/prompts"

for name in bt_engine_nodes.xml bt_engine_registry.generated.yaml builtin_bt_nodes.yaml bt_engine_semantic_overlay.yaml bt_skill_policy.yaml skill_registry.yaml; do
  [[ -f "$CFG/$name" ]] && cp -a "$CFG/$name" "$BACKUP/$name"
done
[[ -f "$CFG/settings.env" ]] && cp -a "$CFG/settings.env" "$BACKUP/settings.env"
[[ -d "$CFG/prompts" ]] && cp -a "$CFG/prompts/." "$BACKUP/prompts/"

SETTINGS="$CFG/settings.env"
touch "$SETTINGS"
if ! grep -qE '^BT_ENGINE_AUTO_EXECUTE=' "$SETTINGS"; then
  printf '\nBT_ENGINE_AUTO_EXECUTE=1\n' >> "$SETTINGS"
fi

cp -a "$PROJECT_ROOT/agent/defaults/bt_engine_nodes.xml" "$CFG/bt_engine_nodes.xml"
cp -a "$PROJECT_ROOT/agent/defaults/bt_engine_registry.generated.yaml" "$CFG/bt_engine_registry.generated.yaml"
cp -a "$PROJECT_ROOT/agent/defaults/builtin_bt_nodes.yaml" "$CFG/builtin_bt_nodes.yaml"
cp -a "$PROJECT_ROOT/agent/defaults/bt_engine_semantic_overlay.yaml" "$CFG/bt_engine_semantic_overlay.yaml"
cp -a "$PROJECT_ROOT/agent/defaults/bt_skill_policy.yaml" "$CFG/bt_skill_policy.yaml"
cp -a "$PROJECT_ROOT/agent/defaults/skill_registry.yaml" "$CFG/skill_registry.yaml"
cp -a "$PROJECT_ROOT/agent/defaults/prompts/." "$CFG/prompts/"
for name in bounded_visual_search.md ground_before_navigation.md; do
  [[ -f "$RUNTIME_ROOT/rag-knowledge/patterns/$name" ]] && cp -a "$RUNTIME_ROOT/rag-knowledge/patterns/$name" "$BACKUP/rag-patterns/$name"
  cp -a "$PROJECT_ROOT/agent/defaults/rag/patterns/$name" "$RUNTIME_ROOT/rag-knowledge/patterns/$name"
done

echo "v6.4 latest-node contract and continuous-search policy installed."
echo "backup: $BACKUP"
echo "Next: bash docker/restart_agent.sh"
echo "Then verify: curl -s http://127.0.0.1:8000/api/bt-engine/sync-status | jq ."
echo "Then test: python3 docker/test_continuous_search.py --engine-validate"
