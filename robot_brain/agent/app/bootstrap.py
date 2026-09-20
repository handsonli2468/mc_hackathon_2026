from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from .settings import settings


_FORMAL_SKILL_KEYS = {"id", "kind"}
_FORMAL_PORT_KEYS = {"type", "default", "required"}


def _semantic_overlay_from_legacy_skill_registry(path: Path) -> dict[str, Any] | None:
    """Migrate v6.0.x skill_registry semantics into the v6.1 local overlay.

    The old registry mixed BT-engine facts (ID/kind/port type/default) with planner
    semantics.  Preserve the semantic half exactly enough for an in-place upgrade,
    while letting the next live /nodes sync replace all formal facts.
    """
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    skills = data.get("skills", []) if isinstance(data, dict) else []
    if not isinstance(skills, list) or not skills:
        return None

    nodes: dict[str, Any] = {}
    for skill in skills:
        if not isinstance(skill, dict) or not skill.get("id"):
            continue
        node_id = str(skill["id"])
        semantic: dict[str, Any] = {"planner_enabled": True}
        for key, value in skill.items():
            if key in _FORMAL_SKILL_KEYS or key in {"inputs", "outputs", "inouts"}:
                continue
            semantic[key] = value
        for direction in ("inputs", "outputs", "inouts"):
            ports = skill.get(direction, {}) or {}
            if not isinstance(ports, dict):
                continue
            semantic_ports: dict[str, Any] = {}
            for port_name, port_spec in ports.items():
                if not isinstance(port_spec, dict):
                    continue
                extras = {k: v for k, v in port_spec.items() if k not in _FORMAL_PORT_KEYS}
                if extras:
                    semantic_ports[str(port_name)] = extras
            if semantic_ports:
                semantic[direction] = semantic_ports
        nodes[node_id] = semantic

    if not nodes:
        return None
    return {
        "version": "1.0",
        "description": "Auto-migrated v6.0.x planner semantics. Formal node IDs/kinds/ports are owned by live BT Engine /nodes.",
        "nodes": nodes,
    }


def bootstrap_runtime() -> None:
    cfg = settings.config_root
    prompts = cfg / "prompts"
    for path in (
        cfg,
        prompts,
        settings.state_root,
        settings.runtime_root / "logs",
        settings.runtime_root / "pids",
        settings.runtime_root / "models",
        settings.runtime_root / "evaluations",
        settings.rag_knowledge_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    defaults = settings.project_root / "agent" / "defaults"
    # Remember whether a legacy registry existed *before* copying defaults. v6.1
    # checked after the copy, which made a truly fresh runtime look like a legacy
    # runtime and accidentally migrated the default merged registry instead of
    # installing the curated semantic overlay.
    skill_registry_preexisting = (cfg / "skill_registry.yaml").exists()
    copies = {
        defaults / "settings.env": cfg / "settings.env",
        defaults / "skill_registry.yaml": cfg / "skill_registry.yaml",
        defaults / "skill_registry.demo.yaml": cfg / "skill_registry.demo.yaml",
        defaults / "builtin_bt_nodes.yaml": cfg / "builtin_bt_nodes.yaml",
        defaults / "bt_engine_nodes.xml": cfg / "bt_engine_nodes.xml",
        defaults / "bt_engine_registry.generated.yaml": cfg / "bt_engine_registry.generated.yaml",
        defaults / "bt_skill_policy.yaml": cfg / "bt_skill_policy.yaml",
    }
    for src, dst in copies.items():
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)

    # Upgrade existing v6.0.x runtimes without discarding hand-maintained skill
    # semantics. Fresh runtimes get the curated default overlay.
    overlay_path = cfg / "bt_engine_semantic_overlay.yaml"
    if not overlay_path.exists():
        migrated = (
            _semantic_overlay_from_legacy_skill_registry(cfg / "skill_registry.yaml")
            if skill_registry_preexisting
            else None
        )
        if migrated is not None:
            overlay_path.write_text(
                yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        else:
            default_overlay = defaults / "bt_engine_semantic_overlay.yaml"
            if default_overlay.exists():
                shutil.copy2(default_overlay, overlay_path)

    default_prompts = defaults / "prompts"
    if default_prompts.exists():
        for src in default_prompts.glob("*.md"):
            dst = prompts / src.name
            if not dst.exists():
                shutil.copy2(src, dst)

    default_rag = defaults / "rag"
    if default_rag.exists():
        for src in default_rag.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(default_rag)
            dst = settings.rag_knowledge_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
