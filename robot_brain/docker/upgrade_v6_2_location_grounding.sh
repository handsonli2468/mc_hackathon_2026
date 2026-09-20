#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
bootstrap_runtime

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${RUNTIME_ROOT}/config-backups/v6.2-${STAMP}"
mkdir -p "${BACKUP}/prompts" "${BACKUP}/rag-knowledge/scene"

[[ -f "${CONFIG_ROOT}/bt_engine_semantic_overlay.yaml" ]] && cp "${CONFIG_ROOT}/bt_engine_semantic_overlay.yaml" "${BACKUP}/bt_engine_semantic_overlay.yaml"
[[ -f "${CONFIG_ROOT}/settings.env" ]] && cp "${CONFIG_ROOT}/settings.env" "${BACKUP}/settings.env"
for f in requirement planner compact_planner; do
  [[ -f "${CONFIG_ROOT}/prompts/${f}.md" ]] && cp "${CONFIG_ROOT}/prompts/${f}.md" "${BACKUP}/prompts/${f}.md"
done
if [[ -d "${RAG_KNOWLEDGE_ROOT}/scene" ]]; then
  cp -a "${RAG_KNOWLEDGE_ROOT}/scene/." "${BACKUP}/rag-knowledge/scene/" 2>/dev/null || true
fi

append_if_missing() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "${CONFIG_ROOT}/settings.env"; then
    printf '\n%s=%s\n' "$key" "$value" >> "${CONFIG_ROOT}/settings.env"
  fi
}
append_if_missing LOCATION_GROUNDING_ENABLED 1
append_if_missing LOCATION_REQUIRE_CALIBRATED 1
append_if_missing LOCATION_REQUIRE_ACTIVE_MAP_VERSION 0
append_if_missing ROBOT_MAP_VERSION ""
append_if_missing LOCATION_REWRITE_VISUAL_PREAMBLE 1

# Install the v6.2 planner contracts. These are versioned code artifacts; local scene
# coordinates and calibration flags are deliberately NOT overwritten.
for f in requirement planner compact_planner; do
  cp "${PROJECT_ROOT}/agent/defaults/prompts/${f}.md" "${CONFIG_ROOT}/prompts/${f}.md"
done

PROJECT_ROOT="${PROJECT_ROOT}" RUNTIME_ROOT="${RUNTIME_ROOT}" RAG_KNOWLEDGE_ROOT="${RAG_KNOWLEDGE_ROOT}" PYTHONPATH="${PROJECT_ROOT}" \
python3 - <<'PY'
from pathlib import Path
import os
import yaml

runtime = Path(os.environ["RUNTIME_ROOT"])
config = runtime / "config"
overlay_path = config / "bt_engine_semantic_overlay.yaml"
default_overlay_path = Path(os.environ["PROJECT_ROOT"]) / "agent/defaults/bt_engine_semantic_overlay.yaml"

def deep_fill(dst, src):
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
        elif isinstance(dst[key], dict) and isinstance(value, dict):
            deep_fill(dst[key], value)

existing = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {"version": "1.0", "nodes": {}}
defaults = yaml.safe_load(default_overlay_path.read_text(encoding="utf-8")) or {}
existing.setdefault("nodes", {})
nav_defaults = ((defaults.get("nodes") or {}).get("NavigateToPoint") or {})
if nav_defaults:
    node = existing["nodes"].setdefault("NavigateToPoint", {})
    # Preserve operator-written text/semantics while filling the v6.2 deterministic
    # binding contract and any missing policy fields.
    deep_fill(node, nav_defaults)
    node.setdefault("planner_enabled", True)
overlay_path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8")

# Existing v6 scene files used `yaw` but did not explicitly record the unit. Preserve
# every pose/calibration flag and annotate those legacy values as radians.
scene_root = Path(os.environ["RAG_KNOWLEDGE_ROOT"]) / "scene"
default_scene_root = Path(os.environ["PROJECT_ROOT"]) / "agent/defaults/rag/scene"
for path in sorted(scene_root.glob("*.yaml")):
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults_scene = default_scene_root / path.name
    default_data = yaml.safe_load(defaults_scene.read_text(encoding="utf-8")) if defaults_scene.exists() else {}
    default_locations = (default_data or {}).get("locations") or {}
    changed = False
    for location_id, spec in (data.get("locations") or {}).items():
        if not isinstance(spec, dict):
            continue
        if isinstance(spec.get("pose"), dict) and spec["pose"].get("yaw") is not None and not spec.get("yaw_unit"):
            spec["yaw_unit"] = "rad"
            changed = True
        default_spec = default_locations.get(location_id) or {}
        if not spec.get("aliases") and default_spec.get("aliases"):
            spec["aliases"] = list(default_spec["aliases"])
            changed = True
    if changed:
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

print(f"[v6.2] semantic overlay updated safely: {overlay_path}")
print("[v6.2] existing scene coordinates/calibrated flags preserved; legacy yaw values annotated as rad")
PY

cat <<EOF
[v6.2] upgrade prepared.
[v6.2] backup: ${BACKUP}
[v6.2] restart Agent so startup /nodes sync rebuilds formal+merged registries and Skill RAG:
  bash docker/restart_agent.sh
[v6.2] NOTE: a scene location remains advisory until its own YAML has calibrated: true.
EOF
