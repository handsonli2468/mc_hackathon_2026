#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$PROJECT_ROOT/agent-runtime}"
CFG="$RUNTIME_ROOT/config"
RAG="$RUNTIME_ROOT/rag-knowledge"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP="$RUNTIME_ROOT/config-backups/v6.3-$STAMP"
mkdir -p "$BACKUP" "$CFG/prompts" "$RAG/locations"

for p in "$CFG/bt_engine_semantic_overlay.yaml" "$CFG/bt_skill_policy.yaml" "$CFG/settings.env"; do
  [[ -f "$p" ]] && cp -a "$p" "$BACKUP/$(basename "$p")"
done
[[ -d "$RAG" ]] && cp -a "$RAG" "$BACKUP/rag-knowledge"

echo "backup: $BACKUP"

# Generic prompts only: overwrite planner policy text, never site knowledge.
cp -a "$PROJECT_ROOT/agent/defaults/prompts/." "$CFG/prompts/"
cp -a "$PROJECT_ROOT/agent/defaults/bt_skill_policy.yaml" "$CFG/bt_skill_policy.yaml"

python3 - "$CFG/bt_engine_semantic_overlay.yaml" "$PROJECT_ROOT/agent/defaults/bt_engine_semantic_overlay.yaml" <<'PY'
from pathlib import Path
import sys, yaml
active=Path(sys.argv[1]); default=Path(sys.argv[2])
base=yaml.safe_load(active.read_text(encoding='utf-8')) if active.exists() else yaml.safe_load(default.read_text(encoding='utf-8'))
base=base or {}; base.setdefault('version','1.0'); nodes=base.setdefault('nodes',{})
for node_id in ('GraspObject','CloseGripper'):
    node=nodes.setdefault(node_id,{})
    node['planner_enabled']=False
    node['deprecated_reason']='Disabled by current team gripper ABI; Planner/RAG must not use this node.'
open_node=nodes.setdefault('OpenGripper',{})
open_node.setdefault('planner_enabled',True)
prov=list(open_node.get('provides') or [])
for cap in ('release_object','open_gripper','gripper_open'):
    if cap not in prov: prov.append(cap)
open_node['provides']=prov
open_node.setdefault('description','Open/release the robot gripper according to the current BT Engine contract.')
# Preserve/upgrade generic location binding metadata without changing live formal ports.
nav=nodes.get('NavigateToPoint')
if isinstance(nav,dict):
    binding=nav.get('location_binding')
    if isinstance(binding,dict):
        kinds=list(binding.get('location_types') or [])
        for kind in ('STATIC_LOCATION','STATIC_REGION','SEMI_STATIC_LOCATION'):
            if kind not in kinds: kinds.append(kind)
        binding['location_types']=kinds
if 'SetGripper' not in nodes:
    nodes['SetGripper']={
      'planner_enabled': False,
      'description':'Current team gripper-setting action. Complete semantic review from live /nodes before enabling.',
      'provides':[], 'preconditions':[], 'success_semantics':[], 'recommended_verification':[],
      'possible_failures':[], 'resources':['GRIPPER'], 'operator_setup_required':True,
      'operator_note':'Run docker/inspect_gripper_contract.py and complete this overlay from the actual formal contract.'
    }
active.write_text(yaml.safe_dump(base,sort_keys=False,allow_unicode=True),encoding='utf-8')
PY

# Migrate v6.2 scene-embedded locations into first-class Location RAG. Dedicated
# location files win on key conflicts; scene docs keep only contextual knowledge.
python3 - "$RAG" <<'PY'
from pathlib import Path
import sys, yaml
root=Path(sys.argv[1]); scene_dir=root/'scene'; loc_dir=root/'locations'; loc_dir.mkdir(parents=True,exist_ok=True)
if scene_dir.exists():
  for scene_path in sorted(scene_dir.glob('*.yaml')):
    data=yaml.safe_load(scene_path.read_text(encoding='utf-8')) or {}
    embedded=data.pop('locations',None)
    if not isinstance(embedded,dict) or not embedded:
      continue
    loc_path=loc_dir/scene_path.name
    loc_data=yaml.safe_load(loc_path.read_text(encoding='utf-8')) if loc_path.exists() else {}
    loc_data=loc_data or {}; dest=loc_data.setdefault('locations',{})
    for loc_id,spec in embedded.items():
      if not isinstance(spec,dict): continue
      merged=dict(spec); merged.setdefault('enabled',True)
      if loc_id in dest and isinstance(dest[loc_id],dict):
        # Existing dedicated data is newer/more explicit; only fill missing keys.
        current=dict(dest[loc_id])
        for k,v in merged.items(): current.setdefault(k,v)
        current.setdefault('enabled',True); dest[loc_id]=current
      else:
        dest[loc_id]=merged
    loc_path.write_text(yaml.safe_dump(loc_data,sort_keys=False,allow_unicode=True),encoding='utf-8')
    scene_path.write_text(yaml.safe_dump(data,sort_keys=False,allow_unicode=True),encoding='utf-8')
# Mark all current structured location records enabled for integration-flow testing.
for loc_path in sorted(loc_dir.glob('*.yaml')):
  data=yaml.safe_load(loc_path.read_text(encoding='utf-8')) or {}
  raw=data.get('locations',data)
  if isinstance(raw,dict):
    for spec in raw.values():
      if isinstance(spec,dict): spec['enabled']=True
  loc_path.write_text(yaml.safe_dump(data,sort_keys=False,allow_unicode=True),encoding='utf-8')
PY

SETTINGS="$CFG/settings.env"
touch "$SETTINGS"
set_kv() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$SETTINGS"; then
    sed -i -E "s|^${key}=.*|${key}=${value}|" "$SETTINGS"
  else
    printf '%s=%s\n' "$key" "$value" >> "$SETTINGS"
  fi
}
set_kv RAG_LOCATION_ENABLED 1
set_kv RAG_LOCATION_MAX_DOCS 4
set_kv RAG_SCENE_OVERFETCH_FACTOR 4
set_kv RAG_SEARCH_PRIOR_MIN_CONFIDENCE 0.70
set_kv LOCATION_KNOWLEDGE_MODE integration

echo
echo "v6.3 typed-RAG upgrade prepared."
echo "1) Restart Agent: bash docker/restart_agent.sh"
echo "2) Inspect current gripper ABI: python3 docker/inspect_gripper_contract.py"
echo "3) Check sync status: curl -s http://127.0.0.1:8000/api/bt-engine/sync-status | jq ."
echo "4) Check RAG: curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq ."
