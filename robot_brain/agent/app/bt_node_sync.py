from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .bt_engine_contract import (
    build_merged_skill_registry,
    engine_model_as_registry,
    parse_bt_engine_tree_nodes_model,
    validate_runtime_contract,
)
from .registry import load_bt_engine_semantic_overlay, load_builtin_registry
from .settings import settings


@dataclass
class BtNodeSyncFailure(RuntimeError):
    message: str
    status_code: int = 500
    detail: Any = None

    def __str__(self) -> str:
        return self.message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _yaml_text(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _backup_files(paths: list[Path]) -> str | None:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = settings.runtime_root / "config-backups" / f"bt-node-sync-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src in existing:
        shutil.copy2(src, backup_dir / src.name)
    return str(backup_dir)


def _overlay_review_scaffold(
    engine: dict[str, dict[str, Any]],
    overlay: dict[str, Any],
    missing_ids: list[str],
    invalid: dict[str, list[str]],
) -> dict[str, Any]:
    """Generate a review file for every leaf that cannot safely enter Planner/RAG."""
    current = overlay.get("nodes", {}) or {}
    nodes: dict[str, Any] = {}
    for node_id in sorted(set(missing_ids) | set(invalid)):
        spec = engine[node_id]
        existing = current.get(node_id) if isinstance(current.get(node_id), dict) else None
        if existing is not None:
            node = dict(existing)
            node["planner_enabled"] = False
        else:
            node = {
                "planner_enabled": False,
                "description": spec.get("description") or f"TODO: describe when Planner should use {node_id}.",
                "provides": [],
                "preconditions": [],
                "success_semantics": [],
                "recommended_verification": [],
                "possible_failures": [],
            }
            semantic_ports: dict[str, Any] = {}
            for name in [*(spec.get("inputs") or {}).keys(), *(spec.get("inouts") or {}).keys()]:
                semantic_ports[name] = {}
            if semantic_ports:
                node["inputs"] = semantic_ports
        if node_id in invalid:
            node["_sync_issues"] = invalid[node_id]
        nodes[node_id] = node
    return {
        "version": "1.0",
        "generated_at": _utc_now(),
        "instructions": (
            "Review each entry, copy/fix it in config/bt_engine_semantic_overlay.yaml, and set planner_enabled=true. "
            "Do not add formal kind/type/default/required fields here; those always come from live BT Engine /nodes."
        ),
        "nodes": nodes,
    }


def _restore_backup(backup_dir: str | None, targets: list[Path]) -> None:
    if not backup_dir:
        return
    root = Path(backup_dir)
    for target in targets:
        src = root / target.name
        if src.exists():
            _atomic_write_text(target, src.read_text(encoding="utf-8"))


def _read_last_report() -> dict[str, Any] | None:
    path = settings.state_root / "bt_node_sync.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_bt_node_sync_status() -> dict[str, Any]:
    report = _read_last_report()
    if report is None:
        return {
            "attempted": False,
            "ok": False,
            "message": "No BT node synchronization attempt has been recorded yet.",
        }
    return report


def persist_bt_node_sync_report(report: dict[str, Any]) -> dict[str, Any]:
    _atomic_write_text(
        settings.state_root / "bt_node_sync.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    return report


def record_sync_failure(error: Exception, *, trigger: str, source: str = "GET /nodes?builtin=0") -> dict[str, Any]:
    previous = _read_last_report() or {}
    report = {
        "attempted": True,
        "ok": False,
        "trigger": trigger,
        "source": source,
        "attempted_at": _utc_now(),
        "error": str(error),
        "last_success_at": previous.get("last_success_at"),
        "using_last_known_good": bool((settings.config_root / "bt_engine_nodes.xml").exists()),
    }
    persist_bt_node_sync_report(report)
    return report


def synchronize_bt_nodes_from_xml(
    xml_text: str,
    *,
    trigger: str,
    source: str = "GET /nodes?builtin=0",
) -> dict[str, Any]:
    """Atomically ingest the live BT engine model and rebuild planner/RAG contracts.

    This function intentionally does not invent semantic capabilities. The formal
    model is always synchronized; Action/Condition nodes are promoted into the
    planner registry only when a local semantic overlay exists and is enabled.
    """
    xml_text = str(xml_text or "").strip()
    if not xml_text:
        raise BtNodeSyncFailure("BT engine returned an empty node model.", status_code=502)

    try:
        engine = parse_bt_engine_tree_nodes_model(xml_text)
    except Exception as exc:
        raise BtNodeSyncFailure(f"Invalid BT engine TreeNodesModel: {exc}", status_code=502) from exc

    min_nodes = max(0, int(getattr(settings, "bt_engine_sync_min_custom_nodes", 1)))
    if len(engine) < min_nodes:
        raise BtNodeSyncFailure(
            f"Refusing to replace the active BT contract with only {len(engine)} custom nodes; minimum is {min_nodes}.",
            status_code=409,
        )

    overlay = load_bt_engine_semantic_overlay()
    merged_registry, merge_report = build_merged_skill_registry(engine, overlay)
    formal_registry = engine_model_as_registry(engine)
    builtin_ids = {
        str(item.get("id"))
        for item in load_builtin_registry().get("nodes", [])
        if isinstance(item, dict) and item.get("id")
    }
    merge_report.update({
        "engine_exported_node_count": len(engine),
        "builtin_node_count": len(builtin_ids),
        "effective_node_count": len(set(engine) | builtin_ids),
    })

    # Validate the candidate before touching active files. Use a temporary engine XML
    # because validate_runtime_contract reads a path just like normal runtime code.
    candidate_path = settings.config_root / ".bt_engine_nodes.candidate.xml"
    candidate_skill_path = settings.config_root / ".skill_registry.candidate.yaml"
    try:
        _atomic_write_text(candidate_path, xml_text + ("\n" if not xml_text.endswith("\n") else ""))
        _atomic_write_text(candidate_skill_path, _yaml_text(merged_registry))
        # validate_runtime_contract accepts the candidate registry directly, so the
        # temporary skill path exists only for operator inspection if a crash occurs.
        contract = validate_runtime_contract(
            skill_registry=merged_registry,
            engine_model_path=candidate_path,
            semantic_overlay=overlay,
        )
        if not contract.get("valid"):
            raise BtNodeSyncFailure(
                "Candidate live BT-engine contract is internally inconsistent.",
                status_code=409,
                detail=contract,
            )
    finally:
        candidate_path.unlink(missing_ok=True)
        candidate_skill_path.unlink(missing_ok=True)

    target_xml = settings.config_root / "bt_engine_nodes.xml"
    target_formal = settings.config_root / "bt_engine_registry.generated.yaml"
    target_skill = settings.config_root / "skill_registry.yaml"
    missing_path = settings.state_root / "bt_engine_semantic_overlay.missing.yaml"

    old_xml = target_xml.read_text(encoding="utf-8") if target_xml.exists() else ""
    old_skill = target_skill.read_text(encoding="utf-8") if target_skill.exists() else ""
    new_xml = xml_text + ("\n" if not xml_text.endswith("\n") else "")
    new_skill = _yaml_text(merged_registry)
    new_formal = _yaml_text(formal_registry)
    changed = {
        "bt_engine_nodes.xml": old_xml != new_xml,
        "skill_registry.yaml": old_skill != new_skill,
        "bt_engine_registry.generated.yaml": (not target_formal.exists()) or target_formal.read_text(encoding="utf-8") != new_formal,
    }

    backup_dir = _backup_files([target_xml, target_formal, target_skill]) if any(changed.values()) else None

    _atomic_write_text(target_xml, new_xml)
    _atomic_write_text(target_formal, new_formal)
    _atomic_write_text(target_skill, new_skill)

    missing_ids = list(merge_report.get("missing_semantic_overlay", []) or [])
    invalid_overlays = dict(merge_report.get("invalid_semantic_overlay", {}) or {})
    required_issues = dict(merge_report.get("required_planner_node_issues", {}) or {})
    for node_id, issue in required_issues.items():
        if node_id in engine:
            invalid_overlays.setdefault(node_id, []).append(str(issue))
    review_ids = sorted(set(missing_ids) | set(invalid_overlays))
    if review_ids:
        _atomic_write_text(
            missing_path,
            _yaml_text(_overlay_review_scaffold(engine, overlay, missing_ids, invalid_overlays)),
        )
    else:
        missing_path.unlink(missing_ok=True)

    try:
        final_contract = validate_runtime_contract()
        if not final_contract.get("valid"):
            raise BtNodeSyncFailure(
                "Post-write BT contract validation unexpectedly failed; active files were rolled back.",
                status_code=500,
                detail={"contract": final_contract, "backup_dir": backup_dir},
            )
    except Exception:
        _restore_backup(backup_dir, [target_xml, target_formal, target_skill])
        raise

    report = {
        "attempted": True,
        "ok": True,
        "trigger": trigger,
        "source": source,
        "attempted_at": _utc_now(),
        "last_success_at": _utc_now(),
        "engine_model_sha256": _sha256(new_xml),
        "changed": changed,
        "backup_dir": backup_dir,
        "paths": {
            "engine_xml": str(target_xml),
            "formal_registry": str(target_formal),
            "semantic_overlay": str(settings.config_root / "bt_engine_semantic_overlay.yaml"),
            "merged_skill_registry": str(target_skill),
            "semantic_overlay_review": str(missing_path) if review_ids else None,
        },
        **merge_report,
        "contract": final_contract,
    }
    persist_bt_node_sync_report(report)
    return report
