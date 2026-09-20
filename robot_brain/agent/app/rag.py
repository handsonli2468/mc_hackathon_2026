from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from defusedxml import ElementTree as DET

from .registry import load_skill_registry, skill_map
from .settings import settings
from .location_grounding import build_location_grounding, LOCATION_NAV_CAPABILITIES, find_location_id_for_target

_STOP = {
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "at", "with", "from", "is", "are",
    "robot", "task", "mission", "use", "using", "need", "needed", "required", "capability", "skill",
}

_SCENE_ANCHOR_STOP = {
    "find", "locate", "search", "look", "grab", "grasp", "pick", "pickup", "approach", "navigate",
    "move", "bring", "return", "put", "place", "visual", "visually", "ground", "grounding", "object",
    "target", "area", "region", "workspace", "environment", "indoor", "room", "surface", "handling",
    "me", "it", "this", "that", "near", "around", "station",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    settings.state_root.mkdir(parents=True, exist_ok=True)
    return settings.state_root / "rag.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_rag_db() -> None:
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
              id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_documents_fts USING fts5(
              id UNINDEXED,
              source_type UNINDEXED,
              title,
              content,
              tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS rag_locations (
              location_id TEXT PRIMARY KEY,
              frame_id TEXT NOT NULL,
              x REAL NOT NULL,
              y REAL NOT NULL,
              yaw REAL,
              location_type TEXT NOT NULL,
              map_version TEXT,
              metadata_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rag_experiences (
              experience_id TEXT PRIMARY KEY,
              mission_id TEXT,
              run_id TEXT,
              mission TEXT NOT NULL,
              outcome TEXT NOT NULL,
              summary TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_experiences_fts USING fts5(
              experience_id UNINDEXED,
              mission,
              summary,
              tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS rag_retrieval_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mission_id TEXT,
              query_text TEXT NOT NULL,
              source_type TEXT NOT NULL,
              retrieved_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")


def _terms(text: str) -> list[str]:
    # ASCII capability/query terms are intentionally favored. Chinese/original mission
    # text is kept in documents but the query expander is prompted to emit concise
    # English semantic queries for stable FTS5 retrieval.
    # Keep both ASCII tokens and CJK runs. Requirement expansion is encouraged to
    # emit concise English cues, but retrieval must not silently fail when a target or
    # alias remains in the user's original language.
    raw = re.findall(r"[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", str(text).casefold())
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip("_-")
        if not token or token in _STOP or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out[:24]


def _fts_query(text: str) -> str:
    terms = _terms(text)
    return " OR ".join(f'"{x.replace(chr(34), "")}"' for x in terms)


def capability_ontology_for_prompt(registry: dict[str, Any] | None = None) -> str:
    """Small capability vocabulary for the first LLM call, without exposing skill IDs/contracts."""
    registry = registry or load_skill_registry()
    caps: list[str] = []
    seen: set[str] = set()
    for skill in registry.get("skills", []) or []:
        for cap in skill.get("provides", []) or []:
            s = str(cap).strip()
            if s and s not in seen:
                seen.add(s)
                caps.append(s)
    return yaml.safe_dump({"capability_vocabulary": sorted(caps)}, sort_keys=False, allow_unicode=True)


def _skill_document(skill: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    sid = str(skill.get("id", ""))
    payload = {
        "id": sid,
        "kind": skill.get("kind"),
        "description": skill.get("description", ""),
        "provides": skill.get("provides", []),
        "inputs": skill.get("inputs", {}),
        "preconditions": skill.get("preconditions", []),
        "success_semantics": skill.get("success_semantics", []),
        "guarantees": skill.get("guarantees", []),
        "does_not_guarantee": skill.get("does_not_guarantee", []),
        "recommended_verification": skill.get("recommended_verification", []),
        "possible_failures": skill.get("possible_failures", []),
        "engine_failure_codes": skill.get("engine_failure_codes", []),
        "engine_failure_codes_known": skill.get("engine_failure_codes_known"),
        "stale_information": skill.get("stale_information", []),
        "object_name_semantics": skill.get("object_name_semantics", {}),
        "requires_grounded_target": skill.get("requires_grounded_target", False),
        "consumes_latest_detected_object": skill.get("consumes_latest_detected_object", False),
        "requires_visual_grounding": skill.get("requires_visual_grounding", False),
        "suitable_for": skill.get("suitable_for", []),
        "location_binding": skill.get("location_binding", {}),
        "distance_semantics": skill.get("distance_semantics", {}),
        "capability_rules": skill.get("capability_rules", {}),
        "object_identity_input": skill.get("object_identity_input"),
        "timeout": skill.get("timeout", {}),
        "recovery_profile": skill.get("recovery_profile", {}),
        "continuous_search_trigger": skill.get("continuous_search_trigger", False),
        "search_policy": skill.get("search_policy", {}),
        "search_policy_role": skill.get("search_policy_role"),
        "expected_running_while_active": skill.get("expected_running_while_active"),
        "ensure_state_precheck_allowed": skill.get("ensure_state_precheck_allowed"),
    }
    return sid, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), payload


def _replace_documents(source_type: str, docs: Iterable[tuple[str, str, str, dict[str, Any]]]) -> int:
    docs = list(docs)
    with _db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM rag_documents WHERE source_type=?", (source_type,)).fetchall()]
        if ids:
            q = ",".join("?" for _ in ids)
            c.execute(f"DELETE FROM rag_documents_fts WHERE id IN ({q})", ids)
        c.execute("DELETE FROM rag_documents WHERE source_type=?", (source_type,))
        for doc_id, title, content, meta in docs:
            c.execute(
                "INSERT OR REPLACE INTO rag_documents(id,source_type,title,content,metadata_json,updated_at) VALUES(?,?,?,?,?,?)",
                (doc_id, source_type, title, content, json.dumps(meta, ensure_ascii=False), _now()),
            )
            c.execute(
                "INSERT INTO rag_documents_fts(id,source_type,title,content) VALUES(?,?,?,?)",
                (doc_id, source_type, title, content),
            )
    return len(docs)


def _load_pattern_docs(root: Path) -> list[tuple[str, str, str, dict[str, Any]]]:
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    for p in sorted((root / "patterns").glob("*.md")) if (root / "patterns").exists() else []:
        text = p.read_text(encoding="utf-8").strip()
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), p.stem)
        out.append((f"pattern:{p.stem}", title, text, {"path": str(p), "pattern_id": p.stem}))
    return out


def _load_scene_docs(root: Path) -> tuple[list[tuple[str, str, str, dict[str, Any]]], list[dict[str, Any]]]:
    """Load Scene RAG documents plus legacy embedded locations.

    v6.3 treats Location as a first-class knowledge type under ``rag/locations``.
    Embedded ``scene/*.yaml:locations`` is still read for in-place upgrades, but new
    knowledge should be stored in the dedicated Location RAG directory.
    """
    docs: list[tuple[str, str, str, dict[str, Any]]] = []
    legacy_locations: list[dict[str, Any]] = []
    scene_dir = root / "scene"
    if not scene_dir.exists():
        return docs, legacy_locations
    for p in sorted(scene_dir.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for item in data.get("documents", []) or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item.get("enabled", True) is False:
                continue
            content = yaml.safe_dump(item, sort_keys=False, allow_unicode=True)
            docs.append((f"scene:{item['id']}", str(item.get("title") or item["id"]), content, dict(item)))
        for loc_id, spec in (data.get("locations", {}) or {}).items():
            if not isinstance(spec, dict):
                continue
            legacy_locations.append({"location_id": str(loc_id), "_legacy_scene_location": True, **spec})
    for p in sorted(scene_dir.glob("*.md")):
        text = p.read_text(encoding="utf-8").strip()
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), p.stem)
        docs.append((f"scene:{p.stem}", title, text, {"path": str(p)}))
    return docs, legacy_locations


def _location_doc(location_id: str, spec: dict[str, Any], *, path: str | None = None) -> tuple[str, str, str, dict[str, Any]]:
    meta = {"location_id": str(location_id), **dict(spec)}
    if path:
        meta["path"] = path
    aliases = [str(x).strip() for x in (meta.get("aliases") or []) if str(x).strip()]
    aliases = list(dict.fromkeys([str(location_id), str(location_id).replace("_", " "), *aliases]))
    meta["aliases"] = aliases
    title = str(meta.get("title") or meta.get("description") or location_id)
    # The indexed content intentionally includes identity/aliases/description but the
    # executable pose remains structured metadata.  Retrieval never fabricates pose.
    index_payload = {
        "location_id": str(location_id),
        "aliases": aliases,
        "description": str(meta.get("description") or ""),
        "location_type": str(meta.get("type") or meta.get("location_type") or "STATIC_LOCATION"),
        "tags": [str(x) for x in (meta.get("tags") or [])],
    }
    return f"location:{location_id}", title, yaml.safe_dump(index_payload, sort_keys=False, allow_unicode=True), meta


def _load_location_docs(root: Path, legacy_locations: list[dict[str, Any]] | None = None) -> tuple[list[tuple[str, str, str, dict[str, Any]]], list[dict[str, Any]]]:
    docs: list[tuple[str, str, str, dict[str, Any]]] = []
    locations_by_id: dict[str, dict[str, Any]] = {}
    location_dir = root / "locations"
    if location_dir.exists():
        for p in sorted(location_dir.glob("*.yaml")):
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            raw = data.get("locations", data)
            if not isinstance(raw, dict):
                continue
            for loc_id, spec in raw.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("enabled", True) is False:
                    continue
                locations_by_id[str(loc_id)] = {"location_id": str(loc_id), **spec}
        for p in sorted(location_dir.glob("*.md")):
            # Markdown locations are advisory-only retrieval docs unless a structured
            # YAML record with the same ID supplies executable pose metadata.
            text = p.read_text(encoding="utf-8").strip()
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), p.stem)
            docs.append((f"location:{p.stem}", title, text, {"location_id": p.stem, "path": str(p), "advisory_only": True}))

    # Dedicated Location RAG wins over legacy scene-embedded records.
    for loc in legacy_locations or []:
        loc_id = str(loc.get("location_id") or "").strip()
        if loc_id and loc_id not in locations_by_id:
            locations_by_id[loc_id] = dict(loc)

    for loc_id, loc in sorted(locations_by_id.items()):
        docs.append(_location_doc(loc_id, loc))
    return docs, list(locations_by_id.values())


def sync_rag_knowledge() -> dict[str, Any]:
    init_rag_db()
    registry = load_skill_registry()
    skill_docs = []
    for skill in registry.get("skills", []) or []:
        sid, content, meta = _skill_document(skill)
        skill_docs.append((f"skill:{sid}", sid, content, meta))
    root = settings.rag_knowledge_root
    pattern_docs = _load_pattern_docs(root)
    scene_docs, legacy_locations = _load_scene_docs(root)
    location_docs, locations = _load_location_docs(root, legacy_locations)
    counts = {
        "skills": _replace_documents("skill", skill_docs),
        "patterns": _replace_documents("pattern", pattern_docs),
        "scene": _replace_documents("scene", scene_docs),
        "location": _replace_documents("location", location_docs),
    }
    with _db() as c:
        c.execute("DELETE FROM rag_locations")
        for loc in locations:
            pose = (loc.get("entry_pose") if isinstance(loc.get("entry_pose"), dict) else None) or (loc.get("pose", {}) or {})
            if "x" not in pose or "y" not in pose:
                continue
            c.execute(
                """INSERT OR REPLACE INTO rag_locations
                (location_id,frame_id,x,y,yaw,location_type,map_version,metadata_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    loc["location_id"], str(loc.get("frame_id", "map")), float(pose["x"]), float(pose["y"]),
                    None if pose.get("yaw") is None else float(pose.get("yaw")), str(loc.get("type", "STATIC_LOCATION")),
                    loc.get("map_version"), json.dumps(loc, ensure_ascii=False), _now(),
                ),
            )
    counts["locations"] = len(locations)
    return counts

def _search_documents(query: str, source_type: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    fts = _fts_query(query)
    rows: list[sqlite3.Row] = []
    with _db() as c:
        if fts:
            try:
                rows = c.execute(
                    """SELECT d.*, bm25(rag_documents_fts) AS rank
                    FROM rag_documents_fts f JOIN rag_documents d ON d.id=f.id
                    WHERE rag_documents_fts MATCH ? AND d.source_type=?
                    ORDER BY rank LIMIT ?""",
                    (fts, source_type, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows and not fts:
            rows = c.execute(
                "SELECT *, 999.0 AS rank FROM rag_documents WHERE source_type=? ORDER BY title LIMIT ?",
                (source_type, int(limit)),
            ).fetchall()
    qterms = set(_terms(query))
    out: list[dict[str, Any]] = []
    for rank_i, row in enumerate(rows):
        content_terms = set(_terms(str(row["title"]) + " " + str(row["content"])))
        overlap = len(qterms & content_terms)
        meta = json.loads(row["metadata_json"] or "{}")
        out.append({
            "id": row["id"], "source_type": row["source_type"], "title": row["title"], "content": row["content"],
            "metadata": meta, "score": round((overlap + 1.0) / (rank_i + 1.0), 4),
        })
    return out



def _normalize_alias(text: str) -> str:
    value = str(text or "").strip().casefold().replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _merge_ranked_docs(groups: list[tuple[str, list[dict[str, Any]]]], limit: int) -> list[dict[str, Any]]:
    """Merge per-query retrieval without diluting one target with unrelated terms."""
    merged: dict[str, dict[str, Any]] = {}
    for query, docs in groups:
        for rank, doc in enumerate(docs):
            did = str(doc.get("id"))
            score = float(doc.get("score") or 0.0) + 1.0 / (rank + 1.0)
            if did not in merged:
                copy = dict(doc)
                copy["retrieved_for"] = [query]
                copy["fusion_score"] = score
                merged[did] = copy
            else:
                merged[did]["fusion_score"] = float(merged[did].get("fusion_score") or 0.0) + score
                if query not in merged[did]["retrieved_for"]:
                    merged[did]["retrieved_for"].append(query)
    return sorted(merged.values(), key=lambda d: (-float(d.get("fusion_score") or 0.0), str(d.get("id"))))[: max(0, int(limit))]


def _fanout_search_documents(queries: list[str], source_type: str, final_limit: int, *, overfetch_factor: int = 4) -> list[dict[str, Any]]:
    clean = list(dict.fromkeys(str(q).strip() for q in queries if str(q).strip()))
    if not clean or final_limit <= 0:
        return []
    overfetch = max(final_limit * max(1, overfetch_factor), 8)
    groups = [(q, _search_documents(q, source_type, overfetch)) for q in clean]
    return _merge_ranked_docs(groups, max(final_limit * max(1, overfetch_factor), final_limit))


def _all_location_rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _db() as c:
        for row in c.execute("SELECT * FROM rag_locations ORDER BY location_id").fetchall():
            meta = json.loads(row["metadata_json"] or "{}")
            out.append({
                "location_id": row["location_id"],
                "frame_id": row["frame_id"],
                "x": row["x"],
                "y": row["y"],
                "yaw": row["yaw"],
                "location_type": row["location_type"],
                "map_version": row["map_version"],
                "metadata": meta,
            })
    return out


def _location_by_id(location_id: str) -> dict[str, Any] | None:
    with _db() as c:
        row = c.execute("SELECT * FROM rag_locations WHERE location_id=?", (str(location_id),)).fetchone()
    if not row:
        return None
    return {
        "location_id": row["location_id"],
        "frame_id": row["frame_id"],
        "x": row["x"],
        "y": row["y"],
        "yaw": row["yaw"],
        "location_type": row["location_type"],
        "map_version": row["map_version"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _registered_phrase_match(source_norm: str, registered_norm: str) -> bool:
    """Match a registered data-driven phrase inside a source without site hard-coding.

    Space-boundary matching is appropriate for Latin aliases, while CJK text normally
    has no spaces between words.  For CJK aliases we therefore allow literal substring
    containment once the registered phrase is at least two characters long.
    """
    if not source_norm or not registered_norm:
        return False
    if source_norm == registered_norm:
        return True
    if _contains_cjk(registered_norm):
        compact_alias = registered_norm.replace(" ", "")
        compact_source = source_norm.replace(" ", "")
        return len(compact_alias) >= 2 and compact_alias in compact_source
    return f" {registered_norm} " in f" {source_norm} "


def _direct_location_matches(message: str, targets: list[str], location_queries: list[str]) -> list[dict[str, Any]]:
    """Resolve registered IDs/aliases directly from Location RAG knowledge.

    This is data-driven deterministic retrieval, not a hard-coded place-name list.
    Exact identity lookup runs before probabilistic lexical/semantic retrieval so a
    named location cannot disappear merely because an unrelated Scene query ranks
    differently.
    """
    rows = _all_location_rows()
    sources = [("user_message", message)] + [("target", x) for x in targets] + [("location_query", x) for x in location_queries]
    normalized_sources = [(kind, text, _normalize_alias(text)) for kind, text in sources if _normalize_alias(text)]
    matches: list[dict[str, Any]] = []
    for loc in rows:
        meta = loc.get("metadata") or {}
        aliases = [str(loc.get("location_id") or ""), str(loc.get("location_id") or "").replace("_", " "), *[str(x) for x in (meta.get("aliases") or [])]]
        normalized_aliases = [(a, _normalize_alias(a)) for a in aliases if _normalize_alias(a)]
        best = None
        for source_kind, source_text, source_norm in normalized_sources:
            for alias, alias_norm in normalized_aliases:
                if _registered_phrase_match(source_norm, alias_norm):
                    candidate = {
                        "method": "exact_alias",
                        "query_source": source_kind,
                        "query": source_text,
                        "matched_alias": alias,
                        "confidence": 1.0,
                    }
                    if best is None or len(alias_norm) > len(_normalize_alias(best.get("matched_alias") or "")):
                        best = candidate
        if best:
            matches.append({"location": loc, "retrieval": best})
    return matches


def _all_scene_documents() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM rag_documents WHERE source_type='scene' ORDER BY id"
        ).fetchall()
    for row in rows:
        out.append({
            "id": row["id"],
            "source_type": row["source_type"],
            "title": row["title"],
            "content": row["content"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "score": 1.0,
        })
    return out


def _direct_scene_target_matches(targets: list[str]) -> list[dict[str, Any]]:
    """Deterministically retrieve Scene priors that register a target/category term.

    Scene RAG remains data-driven: target terms/categories live in RAG documents, not
    prompts or Python.  This exact registered-term channel complements FTS so a known
    object->region prior cannot disappear because of ranking/query dilution.
    """
    normalized_targets = [(t, _normalize_alias(t)) for t in targets if _normalize_alias(t)]
    if not normalized_targets:
        return []
    out: list[dict[str, Any]] = []
    for doc in _all_scene_documents():
        meta = doc.get("metadata") or {}
        registered = [
            *[str(x) for x in (meta.get("target_terms") or [])],
            *[str(x) for x in (meta.get("object_categories") or [])],
        ]
        best = None
        for target, target_norm in normalized_targets:
            for term in registered:
                term_norm = _normalize_alias(term)
                if term_norm and (_registered_phrase_match(target_norm, term_norm) or _registered_phrase_match(term_norm, target_norm)):
                    candidate = {
                        "target": target,
                        "matched_term": term,
                        "method": "registered_target_term",
                    }
                    if best is None or len(term_norm) > len(_normalize_alias(best.get("matched_term") or "")):
                        best = candidate
        if best:
            copy = dict(doc)
            copy["retrieved_for"] = [best["target"]]
            copy["direct_scene_match"] = best
            copy["fusion_score"] = 1000.0
            out.append(copy)
    return out


def _semantic_location_matches(queries: list[str], *, limit: int) -> list[dict[str, Any]]:
    docs = _fanout_search_documents(queries, "location", max(limit, 1), overfetch_factor=3)
    out: list[dict[str, Any]] = []
    for doc in docs[:limit]:
        loc_id = str((doc.get("metadata") or {}).get("location_id") or str(doc.get("id", "")).removeprefix("location:"))
        loc = _location_by_id(loc_id)
        if loc:
            out.append({
                "location": loc,
                "retrieval": {
                    "method": "semantic_fts",
                    "query": list(doc.get("retrieved_for") or []),
                    "score": float(doc.get("fusion_score") or doc.get("score") or 0.0),
                },
            })
    return out


def _merge_location_matches(*groups: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    locations: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    priority = {"exact_alias": 4, "scene_reference": 3, "semantic_fts": 2, "legacy": 1}
    for group in groups:
        for item in group:
            loc = item.get("location") or {}
            loc_id = str(loc.get("location_id") or "")
            if not loc_id:
                continue
            retrieval = dict(item.get("retrieval") or {})
            method = str(retrieval.get("method") or "legacy")
            old_method = str((provenance.get(loc_id) or {}).get("method") or "legacy")
            if loc_id not in locations or priority.get(method, 0) >= priority.get(old_method, 0):
                locations[loc_id] = loc
                provenance[loc_id] = retrieval
    return locations, provenance

def _experience_search(query: str, limit: int) -> list[dict[str, Any]]:
    fts = _fts_query(query)
    rows: list[sqlite3.Row] = []
    with _db() as c:
        if fts:
            try:
                rows = c.execute(
                    """SELECT e.*, bm25(rag_experiences_fts) AS rank
                    FROM rag_experiences_fts f JOIN rag_experiences e ON e.experience_id=f.experience_id
                    WHERE rag_experiences_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (fts, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
    out = []
    active_skills = set(skill_map(load_skill_registry()))
    for i, row in enumerate(rows):
        metadata = json.loads(row["metadata_json"] or "{}")
        plan_skill_ids = [str(x) for x in (metadata.get("plan_skill_ids") or []) if str(x)]
        if not plan_skill_ids:
            # Backward-compatible extraction from pre-v6.3 stored phase strings.
            for phase in metadata.get("plan_phases", []) or []:
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", str(phase))
                if m:
                    plan_skill_ids.append(m.group(1))
        incompatible = sorted(set(plan_skill_ids) - active_skills)
        if incompatible:
            # Do not feed obsolete action demonstrations into the current planner
            # after an ABI/semantic-policy change. The episode remains stored and
            # inspectable; it is simply not used as a planning exemplar.
            continue
        metadata["experience_compatibility"] = {
            "compatible_with_active_skill_registry": True,
            "active_version": settings.version,
        }
        out.append({
            "id": row["experience_id"], "source_type": "experience", "title": row["mission"],
            "content": row["summary"], "metadata": metadata,
            "score": round(1.0 / (i + 1.0), 4),
        })
        if len(out) >= limit:
            break
    return out


def _skill_subset_registry(skill_ids: list[str], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_skill_registry()
    wanted = set(skill_ids)
    return {
        "version": registry.get("version"),
        "source_model": registry.get("source_model"),
        "blackboard": registry.get("blackboard", {}),
        "skills": [s for s in registry.get("skills", []) if str(s.get("id")) in wanted],
    }


def _capability_coverage(required: list[str], skill_ids: list[str], registry: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    skills = skill_map(registry)
    selected = set(skill_ids)
    coverage: dict[str, list[str]] = {}
    missing: list[str] = []
    for cap in required:
        key = _norm(cap)
        matches = []
        for sid in selected:
            spec = skills.get(sid, {})
            vals = [sid, *(spec.get("provides", []) or [])]
            if any(_norm(str(v)) == key for v in vals):
                matches.append(sid)
        if matches:
            coverage[cap] = sorted(matches)
        else:
            missing.append(cap)
    return coverage, missing


def _dependency_expand(skill_ids: list[str], registry: dict[str, Any]) -> list[str]:
    skills = skill_map(registry)
    out = list(dict.fromkeys(skill_ids))
    selected = set(out)
    # Verification conditions are deterministic compiler dependencies and must be
    # available even if the coarse LLM did not ask for them explicitly.
    for sid in list(out):
        for dep in skills.get(sid, {}).get("recommended_verification", []) or []:
            dep = str(dep)
            if dep in skills and dep not in selected:
                selected.add(dep); out.append(dep)
    # Object-relative navigation has a hard grounding invariant. Include the
    # perception primitives needed for deterministic grounding even if the first
    # semantic expansion only asked for navigation.
    if any(bool(skills.get(sid, {}).get("requires_grounded_target")) for sid in selected):
        grounding_actions = [
            sid for sid, spec in skills.items()
            if str(spec.get("kind", "")).upper() == "ACTION"
            and set(map(str, spec.get("provides", []) or [])).intersection({"search_object", "locate_object"})
        ]
        if len(grounding_actions) == 1:
            dep = grounding_actions[0]
            if dep not in selected:
                selected.add(dep); out.append(dep)
            for verify in skills.get(dep, {}).get("recommended_verification", []) or []:
                verify = str(verify)
                if verify in skills and verify not in selected:
                    selected.add(verify); out.append(verify)
    # Current visual search needs the engine-owned continuous Patrol activity. Old
    # registries may still expose a viewpoint-change recovery profile.
    if any(set(map(str, skills.get(sid, {}).get("provides", []) or [])).intersection({"search_object", "locate_object"}) for sid in selected):
        continuous = [
            dep_id for dep_id, spec in skills.items()
            if str(spec.get("search_policy_role", "")) == "continuous_patrol"
            or "continuous_search_motion" in set(map(str, spec.get("provides", []) or []))
        ]
        for dep_id in continuous:
            if dep_id not in selected:
                selected.add(dep_id); out.append(dep_id)
        if continuous:
            return out
        for dep_id, spec in skills.items():
            if (spec.get("recovery_profile", {}) or {}).get("role") == "viewpoint_change" and dep_id not in selected:
                selected.add(dep_id); out.append(dep_id)
    return out


def _select_skill_ids(required: list[str], skill_queries: list[str], max_skills: int) -> tuple[list[str], dict[str, list[str]], list[str], list[dict[str, Any]]]:
    registry = load_skill_registry()
    skills = skill_map(registry)
    query = " ".join([*required, *skill_queries])
    docs = _search_documents(query, "skill", max(max_skills * 2, 8))
    # Candidate order: exact capability providers first, then FTS ranking.
    candidate_ids: list[str] = []
    for cap in required:
        key = _norm(cap)
        for sid, spec in skills.items():
            vals = [sid, *(spec.get("provides", []) or [])]
            if any(_norm(str(v)) == key for v in vals) and sid not in candidate_ids:
                candidate_ids.append(sid)
    for doc in docs:
        sid = str((doc.get("metadata") or {}).get("id") or str(doc.get("id", "")).removeprefix("skill:"))
        if sid in skills and sid not in candidate_ids:
            candidate_ids.append(sid)

    # Greedy set-cover keeps the context small while satisfying requested capabilities.
    uncovered = {_norm(c): c for c in required}
    selected: list[str] = []
    while uncovered and len(selected) < max_skills:
        best_sid = None; best_keys: list[str] = []
        for sid in candidate_ids:
            if sid in selected:
                continue
            vals = {_norm(str(x)) for x in [sid, *(skills[sid].get("provides", []) or [])]}
            keys = [k for k in uncovered if k in vals]
            if len(keys) > len(best_keys):
                best_sid, best_keys = sid, keys
        if not best_sid:
            break
        selected.append(best_sid)
        for k in best_keys:
            uncovered.pop(k, None)
    # If no explicit capability was requested (rare but possible for a coarse
    # mission), keep a small retrieval-ranked seed rather than exposing the whole
    # registry. Otherwise preserve the minimal capability-covering set.
    if not selected and candidate_ids:
        selected = candidate_ids[: min(4, max_skills)]
    selected = _dependency_expand(selected, registry)
    # Hard cap applies before dependencies; deterministic dependencies may add 1-3
    # nodes and are intentionally retained for compile/verification correctness.
    coverage, missing = _capability_coverage(required, selected, registry)
    selected_docs = []
    by_id = {str((d.get("metadata") or {}).get("id")): d for d in docs}
    for sid in selected:
        if sid in by_id:
            selected_docs.append(by_id[sid])
        else:
            spec = skills[sid]
            _, content, meta = _skill_document(spec)
            selected_docs.append({"id": f"skill:{sid}", "source_type": "skill", "title": sid, "content": content, "metadata": meta, "score": 1.0})
    return selected, coverage, missing, selected_docs


def _queries_from_sketch(sketch: dict[str, Any] | None, key: str) -> list[str]:
    if not sketch:
        return []
    vals = sketch.get(key, []) or []
    return [str(x).strip() for x in vals if str(x).strip()]


def _scene_anchor_terms(text: str) -> set[str]:
    terms = set(_terms(text))
    return {t for t in terms if t not in _SCENE_ANCHOR_STOP}


def _filter_patterns_by_intent(
    patterns: list[dict[str, Any]],
    required: list[str],
    sketch: dict[str, Any],
    targets: list[str],
    *,
    needs_clarification: bool = False,
) -> list[dict[str, Any]]:
    required_norm = {_norm(x) for x in required}
    intent_terms = set(_terms(" ".join([
        *targets,
        *[str(x) for x in (sketch.get("semantic_concepts") or [])],
        *[str(x) for x in (sketch.get("pattern_queries") or [])],
    ])))
    wants_tracking = bool(
        required_norm.intersection({"track_object", "refresh_object_tracking"})
        or intent_terms.intersection({"track", "tracking", "moving", "dynamic", "person", "people", "human", "recipient"})
    )
    out = []
    for doc in patterns:
        pid = str(doc.get("id") or "")
        if pid == "pattern:track_dynamic_target" and not wants_tracking:
            continue
        if pid == "pattern:human_clarification" and not needs_clarification:
            continue
        out.append(doc)
    return out


def _filter_scene_by_anchor(
    scene: list[dict[str, Any]],
    hard_anchors: set[str],
    soft_anchors: set[str],
) -> list[dict[str, Any]]:
    """Ground scene priors in the user's explicit entity whenever possible.

    v6.0.1 mixed LLM-expanded target descriptions into the same anchor set used to
    *guard* scene retrieval. That allowed an expansion such as "presentation area"
    to validate its own unrelated presentation prior for a request about a cup.

    v6.1 separates trust levels:
      * hard anchors = discriminative English terms literally present in user input;
      * soft anchors = LLM-expanded target descriptions, used only when the user text
        contains no usable English entity term (important for non-English missions).
    """
    anchors = hard_anchors or soft_anchors
    anchor_source = "user_message" if hard_anchors else ("llm_target_fallback" if soft_anchors else "none")
    if not anchors:
        return []
    out: list[dict[str, Any]] = []
    for doc in scene:
        meta = doc.get("metadata") or {}
        haystack = " ".join([
            str(doc.get("title") or ""),
            str(doc.get("content") or ""),
            " ".join(str(x) for x in (meta.get("tags") or [])),
            " ".join(str(x) for x in (meta.get("location_ids") or [])),
        ])
        doc_terms = set(_terms(haystack))
        overlap = anchors & doc_terms
        if overlap:
            copy = dict(doc)
            copy["anchor_terms"] = sorted(overlap)
            copy["anchor_source"] = anchor_source
            copy["hard_anchor_terms"] = sorted(hard_anchors & doc_terms)
            copy["soft_anchor_terms"] = sorted(soft_anchors & doc_terms)
            out.append(copy)
    return out


def analyze_plan_rag_utilization(
    plan: Any,
    context: dict[str, Any] | None,
    *,
    location_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report enforceable RAG use and deterministic location-grounding provenance."""
    ctx = context or {}
    phases = list(getattr(plan, "phases", []) or [])
    active_skills = skill_map(load_skill_registry())
    used_skills: list[str] = []
    plan_targets: list[str] = []
    search_activity = False
    track_used = False
    has_verification = False
    for phase in phases:
        sid = str(phase.nominal_action.skill)
        used_skills.append(sid)
        args = phase.nominal_action.arguments or {}
        if args.get("object_name"):
            plan_targets.append(str(args.get("object_name")))
        phase_provides = {str(x) for x in (active_skills.get(sid, {}).get("provides", []) or [])}
        if phase_provides.intersection({"track_object", "refresh_object_tracking"}):
            track_used = True
        if phase.verification:
            has_verification = True
        for v in phase.verification:
            used_skills.append(str(v.condition))
        for fm in phase.failure_modes:
            for step in fm.recovery:
                if step.skill:
                    used_skills.append(str(step.skill))
                    step_spec = active_skills.get(str(step.skill), {})
                    if (
                        str(step_spec.get("search_policy_role", "")) == "continuous_patrol"
                        or "continuous_search_motion" in set(map(str, step_spec.get("provides", []) or []))
                        or str(step.skill) == "RotateInPlace"
                    ):
                        search_activity = True
    used_skills = list(dict.fromkeys(used_skills))
    allowed = list(ctx.get("allowed_skill_ids", []) or [])

    policy_bindings = list((location_policy or {}).get("bindings", []) or [])
    bindings_by_location = {str(x.get("location_id")): x for x in policy_bindings if x.get("location_id")}
    grounded_locations = ((ctx.get("location_grounding") or {}).get("locations") or {})

    locations_usage: dict[str, Any] = {}
    for loc_id, raw_loc in (ctx.get("locations", {}) or {}).items():
        g = grounded_locations.get(loc_id) or {}
        referenced_by_name = any(
            find_location_id_for_target(t, ctx.get("location_grounding")) == str(loc_id)
            for t in plan_targets if t
        )
        binding = bindings_by_location.get(str(loc_id))
        referenced = bool(referenced_by_name or binding)
        candidates = list(g.get("navigation_candidates", []) or [])
        nav_ids = [str(x.get("skill_id")) for x in candidates if x.get("skill_id")]
        locations_usage[str(loc_id)] = {
            "retrieved": True,
            "referenced_in_plan": referenced,
            "calibrated": bool(g.get("calibrated", (raw_loc.get("metadata") or {}).get("calibrated", False))),
            "map_version": g.get("map_version", raw_loc.get("map_version")),
            "frame_id": g.get("frame_id", raw_loc.get("frame_id")),
            "location_navigation_skill_ids": nav_ids,
            "actionable": bool(g.get("actionable", False)),
            "reason": g.get("reason", "location grounding unavailable"),
            "used_navigation_skill": (binding or {}).get("skill"),
            "bound_arguments": (binding or {}).get("arguments"),
            "causal_use_verified": bool(binding),
        }

    pattern_matches: dict[str, bool] = {}
    for doc in ctx.get("patterns", []) or []:
        pid = str(doc.get("id", ""))
        key = pid.removeprefix("pattern:")
        matched = False
        if key == "bounded_visual_search":
            matched = search_activity
        elif key == "track_dynamic_target":
            matched = track_used
        elif key == "action_then_verification":
            matched = has_verification
        elif key == "scene_prior_then_local_search":
            matched = any(v.get("actionable") and v.get("referenced_in_plan") for v in locations_usage.values())
        elif key == "ground_before_navigation":
            grounded_seen: set[str] = set()
            for phase in phases:
                sid = str(phase.nominal_action.skill)
                obj = str((phase.nominal_action.arguments or {}).get("object_name") or "").strip().lower()
                provides = {str(x) for x in (active_skills.get(sid, {}).get("provides", []) or [])}
                if provides.intersection({"search_object", "locate_object", "track_object"}) and obj:
                    grounded_seen.add(obj)
                if active_skills.get(sid, {}).get("requires_grounded_target") and grounded_seen:
                    matched = True
                    break
        pattern_matches[pid] = matched

    scene_docs = []
    for doc in ctx.get("scene", []) or []:
        meta = doc.get("metadata") or {}
        loc_ids = [str(x) for x in (meta.get("location_ids") or [])]
        causal = any((locations_usage.get(x) or {}).get("causal_use_verified") for x in loc_ids)
        scene_docs.append({
            "id": doc.get("id"),
            "retrieved": True,
            "anchor_terms": doc.get("anchor_terms", []),
            "location_ids": loc_ids,
            "actionable_location_ids": [x for x in loc_ids if (locations_usage.get(x) or {}).get("actionable")],
            "causal_use_verified": causal,
        })

    deterministic_scene_use = any(v.get("causal_use_verified") for v in locations_usage.values())
    return {
        "skill": {
            "retrieved_allowed_skill_ids": allowed,
            "used_skill_ids": used_skills,
            "used_from_retrieved": [x for x in used_skills if x in set(allowed)],
            "enforcement": "closed_set_validator",
            "causal_use_verified": True,
        },
        "patterns": {
            "retrieved_ids": [d.get("id") for d in ctx.get("patterns", []) or []],
            "observable_plan_matches": pattern_matches,
            "causal_use_verified": False,
        },
        "scene": {
            "documents": scene_docs,
            "locations": locations_usage,
            "causal_use_verified": deterministic_scene_use,
        },
        "location_grounding_policy": {
            "changed": bool((location_policy or {}).get("changed")),
            "bindings": policy_bindings,
            "removed_visual_grounding_phases": list((location_policy or {}).get("removed_visual_grounding_phases", []) or []),
            "causal_use_verified": bool(policy_bindings),
        },
        "experience": {
            "retrieved_ids": [d.get("id") for d in ctx.get("experiences", []) or []],
            "injected_into_planner_prompt": bool(ctx.get("experiences")),
            "causal_use_verified": False,
        },
        "interpretation": [
            "Skill RAG is a hard constraint because the plan/XML closed-set validators enforce allowed_skill_ids.",
            "Retrieved Location-RAG records become executable only after deterministic trust/frame/map/binding checks.",
            "A location policy binding is causal evidence because deterministic code rewrote/bound the plan from that retrieved location.",
            "Scene, pattern and experience retrieval remain advisory unless a deterministic grounding/policy binding proves use.",
        ],
    }


def _skill_docs_for_ids(selected_ids: list[str], existing_docs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    registry = load_skill_registry()
    skills = skill_map(registry)
    existing_docs = existing_docs or []
    by_id = {
        str((d.get("metadata") or {}).get("id") or str(d.get("id", "")).removeprefix("skill:")): d
        for d in existing_docs
    }
    out: list[dict[str, Any]] = []
    for sid in selected_ids:
        if sid in by_id:
            out.append(by_id[sid])
            continue
        spec = skills.get(sid)
        if not spec:
            continue
        _, content, meta = _skill_document(spec)
        out.append({
            "id": f"skill:{sid}",
            "source_type": "skill",
            "title": sid,
            "content": content,
            "metadata": meta,
            "score": 1.0,
        })
    return out


def _scene_search_priors(
    scene: list[dict[str, Any]],
    targets: list[str],
    location_grounding: dict[str, Any],
) -> list[dict[str, Any]]:
    grounded = location_grounding.get("locations", {}) or {}
    priors: list[dict[str, Any]] = []
    for doc in scene:
        meta = doc.get("metadata") or {}
        loc_ids = [str(x) for x in (meta.get("location_ids") or meta.get("related_locations") or [])]
        if not loc_ids:
            continue
        doc_text = " ".join([
            str(doc.get("title") or ""),
            str(doc.get("content") or ""),
            " ".join(str(x) for x in (meta.get("tags") or [])),
            " ".join(str(x) for x in (meta.get("object_categories") or [])),
            " ".join(str(x) for x in (meta.get("target_terms") or [])),
        ])
        doc_terms = set(_terms(doc_text))
        confidence = float(meta.get("confidence", 0.5) or 0.5)
        normalized_doc = _normalize_alias(doc_text)
        for target in targets:
            target_terms = set(_terms(target))
            normalized_target = _normalize_alias(target)
            lexical_match = bool(target_terms and (target_terms & doc_terms))
            phrase_match = bool(normalized_target and normalized_target in normalized_doc)
            if not lexical_match and not phrase_match:
                continue
            for loc_id in loc_ids:
                info = grounded.get(loc_id) or {}
                prior = {
                    "target": target,
                    "source_scene_id": doc.get("id"),
                    "location_id": loc_id,
                    "confidence": confidence,
                    "actionable": bool(info.get("actionable")),
                    "reason": info.get("reason", "location grounding unavailable"),
                    "navigation_candidates": list(info.get("navigation_candidates") or []) if info.get("actionable") else [],
                }
                priors.append(prior)
    # Stable ordering: high confidence, actionable first, then identity.
    priors.sort(key=lambda x: (-float(x.get("confidence") or 0.0), not bool(x.get("actionable")), str(x.get("target")), str(x.get("location_id"))))
    return priors


def retrieve_context(
    message: str,
    requirements: dict[str, Any],
    *,
    mission_id: str | None = None,
    world_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_rag_db()
    sketch = dict(requirements.get("retrieval_sketch") or {})
    task_semantics = dict(requirements.get("task_semantics") or {})
    required = [str(x) for x in requirements.get("required_capabilities", []) or []]
    skill_queries = _queries_from_sketch(sketch, "skill_queries")
    semantic = [str(x) for x in sketch.get("semantic_concepts", []) or []]
    targets = [str(x) for x in sketch.get("target_descriptions", []) or []]
    location_queries = _queries_from_sketch(sketch, "location_queries")
    for x in task_semantics.get("destination_targets", []) or []:
        if str(x).strip() and str(x).strip() not in location_queries:
            location_queries.append(str(x).strip())
    base_query = " ".join([message, *semantic, *targets])

    # Pass 1: minimal capability cover. Location/scene retrieval may deterministically
    # expand the closed set after executable grounding is known.
    selected_ids, coverage, missing, initial_skill_docs = _select_skill_ids(
        required, skill_queries, settings.rag_skill_max_docs
    )
    initial_selected_ids = list(selected_ids)

    pattern_hints = _queries_from_sketch(sketch, "pattern_queries")
    scene_hints = _queries_from_sketch(sketch, "scene_queries")
    experience_hints = _queries_from_sketch(sketch, "experience_queries")
    pattern_query = " ".join([*pattern_hints, *required])
    exp_query = " ".join([*experience_hints, *targets, message])

    patterns = _search_documents(pattern_query, "pattern", settings.rag_pattern_max_docs) if settings.rag_pattern_enabled else []
    needs_clarification = bool(requirements.get("required_user_information")) or str(requirements.get("status_hint", "")).upper().endswith("NEED_MORE_INFO")
    patterns = _filter_patterns_by_intent(patterns, required, sketch, targets, needs_clarification=needs_clarification)

    # Scene RAG fan-out: every target/query is searched independently, then fused.
    # This prevents an unrelated long query from diluting a discriminative target.
    scene_queries = list(dict.fromkeys([*targets, *scene_hints]))
    scene_candidates = _fanout_search_documents(
        scene_queries,
        "scene",
        settings.rag_scene_max_docs,
        overfetch_factor=getattr(settings, "rag_scene_overfetch_factor", 4),
    ) if settings.rag_scene_enabled else []
    hard_scene_anchors = _scene_anchor_terms(message)
    soft_scene_anchors = _scene_anchor_terms(" ".join(targets))
    filtered_fts_scene = _filter_scene_by_anchor(scene_candidates, hard_scene_anchors, soft_scene_anchors)
    # Registered target/category terms are a deterministic typed-RAG identity channel
    # and therefore do not depend on FTS ranking or language tokenization.  Merge them
    # before the final top-K; FTS still supplies broader contextual Scene knowledge.
    direct_scene = _direct_scene_target_matches(targets) if settings.rag_scene_enabled else []
    scene_by_id: dict[str, dict[str, Any]] = {}
    for doc in [*direct_scene, *filtered_fts_scene]:
        did = str(doc.get("id") or "")
        if not did:
            continue
        if did not in scene_by_id or doc.get("direct_scene_match"):
            scene_by_id[did] = doc
    scene = sorted(
        scene_by_id.values(),
        key=lambda d: (0 if d.get("direct_scene_match") else 1, -float(d.get("fusion_score") or d.get("score") or 0.0), str(d.get("id"))),
    )[: settings.rag_scene_max_docs]
    procedural_terms = {"setup", "prepare", "preparation", "cleanup", "clean", "tidy", "organize", "organise"}
    if not (set(_terms(" ".join(scene_queries))) & procedural_terms):
        scene = [d for d in scene if str((d.get("metadata") or {}).get("knowledge_type", "")).upper() != "PROCEDURAL_SCENE"]

    experiences = _experience_search(exp_query, settings.rag_experience_max_docs) if settings.rag_experience_enabled else []

    # Location RAG is first-class. Named locations are resolved from registered
    # aliases/IDs directly, independently of whether Scene RAG found anything.
    direct_matches = _direct_location_matches(message, targets, location_queries) if getattr(settings, "rag_location_enabled", True) else []
    # Only semantic-search location queries that were not already resolved by exact
    # registered identity/alias. This avoids generic words such as "station" pulling
    # unrelated locations beside a perfect exact hit.
    directly_resolved_queries = {
        _normalize_alias((m.get("retrieval") or {}).get("query"))
        for m in direct_matches
        if (m.get("retrieval") or {}).get("query_source") == "location_query"
    }
    semantic_location_queries = [q for q in location_queries if _normalize_alias(q) not in directly_resolved_queries]
    semantic_matches = _semantic_location_matches(
        semantic_location_queries,
        limit=getattr(settings, "rag_location_max_docs", 4),
    ) if getattr(settings, "rag_location_enabled", True) and semantic_location_queries else []

    # Scene knowledge may discover a useful search region even when the user did not
    # name that region. Scene->Location uses exact location_id references, not another
    # probabilistic search.
    scene_matches: list[dict[str, Any]] = []
    for doc in scene:
        meta = doc.get("metadata") or {}
        for loc_id in meta.get("location_ids", []) or meta.get("related_locations", []) or []:
            loc = _location_by_id(str(loc_id))
            if loc:
                scene_matches.append({
                    "location": loc,
                    "retrieval": {
                        "method": "scene_reference",
                        "source_scene_id": doc.get("id"),
                        "confidence": float(meta.get("confidence", 0.5) or 0.5),
                    },
                })

    locations, location_retrieval = _merge_location_matches(direct_matches, scene_matches, semantic_matches)

    location_grounding = build_location_grounding(
        locations, registry=load_skill_registry(), world_state=world_state
    ) if getattr(settings, "location_grounding_enabled", True) else {
        "enabled": False, "locations": {}, "expanded_skill_ids": [], "actionable_location_ids": []
    }

    search_priors = _scene_search_priors(scene, targets, location_grounding)
    location_expanded = [str(x) for x in (location_grounding.get("expanded_skill_ids") or [])]
    if location_expanded:
        selected_ids = list(dict.fromkeys([*selected_ids, *location_expanded]))
        selected_ids = _dependency_expand(selected_ids, load_skill_registry())
        coverage, missing = _capability_coverage(required, selected_ids, load_skill_registry())

    skill_docs = _skill_docs_for_ids(selected_ids, initial_skill_docs)
    context = {
        "enabled": True,
        "strategy": "typed_rag_capability_coverage_scene_location_v6_3",
        "retrieval_sketch": sketch,
        "required_capabilities": required,
        "skill_coverage": coverage,
        "missing_capabilities": missing,
        "allowed_skill_ids": selected_ids,
        "retrieved_skills": skill_docs,
        "location_skill_expansion": {
            "added_skill_ids": [x for x in location_expanded if x not in set(initial_selected_ids)],
            "candidate_skill_ids": location_expanded,
            "reason": "retrieved locations were deterministically bound to active location-navigation skills" if location_expanded else "no executable retrieved location required location-skill expansion",
        },
        "patterns": patterns,
        "scene": scene,
        "scene_retrieval": {"queries": scene_queries, "fanout": True, "filter_before_final_limit": True},
        "scene_anchor_policy": {
            "hard_user_message_terms": sorted(hard_scene_anchors),
            "soft_llm_target_terms": sorted(soft_scene_anchors),
            "active_source": "user_message" if hard_scene_anchors else ("llm_target_fallback" if soft_scene_anchors else "none"),
        },
        "locations": locations,
        "location_retrieval": location_retrieval,
        "location_grounding": location_grounding,
        "search_priors": search_priors,
        "experiences": experiences,
        "counts": {
            "skills": len(skill_docs),
            "patterns": len(patterns),
            "scene": len(scene),
            "locations": len(locations),
            "search_priors": len(search_priors),
            "experiences": len(experiences),
        },
        "ground_rules": [
            "All environment/place facts in this planning pass come from typed RAG knowledge or explicit world_state; system prompts contain no site-specific map facts.",
            "Location RAG is independently retrievable by registered ID/alias; Scene RAG is contextual knowledge and search prior, not the sole gateway to locations.",
            "Live perception and explicit world_state override retrieved scene/experience priors for movable objects and people.",
            "Only allowed_skill_ids may appear in planner TaskPlanIR robot skill fields.",
            "For an exact named destination with an actionable location, use its deterministic navigation_candidate instead of treating the place name as a visual object.",
            "For an unresolved visual target with a high-confidence actionable search_prior, navigate to that search location before local visual search.",
            "Copy supplied navigation_candidate coordinates/yaw exactly; an optional speed port may be selected from its allowed values using relevant high-rated experience.",
        ],
    }
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO rag_retrieval_log(mission_id,query_text,source_type,retrieved_json,created_at) VALUES(?,?,?,?,?)",
                (mission_id, base_query, "bundle", json.dumps({
                    "allowed_skill_ids": selected_ids,
                    "counts": context["counts"],
                    "missing": missing,
                    "actionable_location_ids": location_grounding.get("actionable_location_ids", []),
                    "location_retrieval": location_retrieval,
                }, ensure_ascii=False), _now()),
            )
    except Exception:
        pass
    return context

def planner_context_for_prompt(context: dict[str, Any] | None) -> str:
    if not context:
        return "RAG context unavailable."
    skill_payload = []
    for doc in context.get("retrieved_skills", []) or []:
        meta = dict(doc.get("metadata") or {})
        keep = {k: meta.get(k) for k in (
            "id", "kind", "description", "provides", "inputs", "preconditions", "success_semantics", "guarantees", "recommended_verification",
            "possible_failures", "object_name_semantics", "requires_grounded_target", "requires_visual_grounding",
            "distance_semantics", "timeout", "recovery_profile", "suitable_for", "location_binding",
            "capability_rules", "object_identity_input", "consumes_latest_detected_object", "does_not_guarantee",
            "engine_failure_codes", "engine_failure_codes_known", "continuous_search_trigger", "search_policy",
            "search_policy_role", "expected_running_while_active", "ensure_state_precheck_allowed"
        ) if meta.get(k) not in (None, [], {}, "")}
        skill_payload.append(keep)
    patterns = [{"id": d["id"], "title": d["title"], "content": d["content"][:1800]} for d in context.get("patterns", []) or []]
    scene = [{"id": d["id"], "title": d["title"], "content": d["content"][:1500]} for d in context.get("scene", []) or []]
    exp = [{"id": d["id"], "summary": d["content"][:1000]} for d in context.get("experiences", []) or []]

    grounded = (context.get("location_grounding") or {}).get("locations", {}) or {}
    known_locations: dict[str, Any] = {}
    for key, value in (context.get("locations", {}) or {}).items():
        meta = value.get("metadata") or {}
        g = grounded.get(key) or {}
        item: dict[str, Any] = {
            "location_id": value.get("location_id"),
            "aliases": g.get("aliases", []),
            "location_type": value.get("location_type"),
            "frame_id": value.get("frame_id"),
            "map_version": value.get("map_version"),
            "calibrated": bool(g.get("calibrated", meta.get("calibrated", False))),
            "trust_mode": g.get("trust_mode"),
            "actionable": bool(g.get("actionable", False)),
            "reason": g.get("reason", ""),
            "description": meta.get("description", ""),
        }
        if g.get("actionable"):
            # Expose only deterministic bound candidates. The LLM never has to infer
            # coordinate units or translate yaw radians to BT-engine degrees itself.
            item["navigation_candidates"] = g.get("navigation_candidates", [])
        known_locations[key] = item

    payload = {
        "allowed_skill_ids": context.get("allowed_skill_ids", []),
        "skill_coverage": context.get("skill_coverage", {}),
        "location_skill_expansion": context.get("location_skill_expansion", {}),
        "skills": skill_payload,
        "patterns": patterns,
        "scene_priors": scene,
        "known_locations": known_locations,
        "location_retrieval": context.get("location_retrieval", {}),
        "search_priors": context.get("search_priors", []),
        "experiences": exp,
        "rules": context.get("ground_rules", []),
    }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return text[: settings.rag_prompt_max_chars]


def validate_plan_closed_set(plan: Any, context: dict[str, Any] | None) -> dict[str, Any]:
    allowed = set((context or {}).get("allowed_skill_ids", []) or [])
    if not allowed:
        return {"valid": not settings.rag_required, "errors": [] if not settings.rag_required else ["RAG produced no allowed skill set."], "used_skill_ids": []}
    used: list[str] = []
    for phase in getattr(plan, "phases", []) or []:
        used.append(str(phase.nominal_action.skill))
        used.extend(str(v.condition) for v in phase.verification)
        for fm in phase.failure_modes:
            used.extend(str(r.skill) for r in fm.recovery if r.skill)
        used.extend(str(a.skill) for a in phase.cleanup)
    used = list(dict.fromkeys(used))
    errors = [f"TaskPlanIR uses skill '{sid}' outside the RAG-retrieved closed skill set." for sid in used if sid not in allowed]
    return {"valid": not errors, "errors": errors, "used_skill_ids": used, "allowed_skill_ids": sorted(allowed)}



def validate_bt_xml_closed_set(xml: str, context: dict[str, Any] | None) -> dict[str, Any]:
    allowed = set((context or {}).get("allowed_skill_ids", []) or [])
    if not allowed:
        return {"valid": not settings.rag_required, "errors": [] if not settings.rag_required else ["RAG produced no allowed skill set for XML validation."], "used_skill_ids": []}
    all_skill_ids = set(skill_map(load_skill_registry()))
    try:
        root = DET.fromstring(xml)
    except Exception as exc:
        return {"valid": False, "errors": [f"Could not parse XML for RAG closed-set validation: {exc}"], "used_skill_ids": []}
    used: list[str] = []
    for el in root.iter():
        sid = None
        if el.tag in all_skill_ids:
            sid = el.tag
        elif el.tag in {"Action", "Condition"} and el.attrib.get("ID") in all_skill_ids:
            sid = el.attrib.get("ID")
        if sid and sid not in used:
            used.append(sid)
    errors = [f"BehaviorTree uses skill '{sid}' outside the RAG-retrieved closed skill set." for sid in used if sid not in allowed]
    return {"valid": not errors, "errors": errors, "used_skill_ids": used, "allowed_skill_ids": sorted(allowed)}

def status() -> dict[str, Any]:
    init_rag_db()
    with _db() as c:
        docs = {r["source_type"]: r["n"] for r in c.execute("SELECT source_type,COUNT(*) n FROM rag_documents GROUP BY source_type").fetchall()}
        exp_n = c.execute("SELECT COUNT(*) n FROM rag_experiences").fetchone()["n"]
        loc_n = c.execute("SELECT COUNT(*) n FROM rag_locations").fetchone()["n"]
    return {"enabled": settings.rag_enabled, "required": settings.rag_required, "documents": docs, "experiences": exp_n, "locations": loc_n, "db": str(_db_path())}


def record_execution_experience(mission: dict[str, Any], engine_payload: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.rag_experience_enabled:
        return None
    init_rag_db()
    artifacts = mission.get("artifacts") or {}
    plan = artifacts.get("task_plan_ir") or {}
    state = str(engine_payload.get("state", "unknown"))
    run_id = str(engine_payload.get("run_id") or "")
    exp_id = f"exp:{mission.get('mission_id')}:{run_id or state}"
    phases = []
    for p in plan.get("phases", []) or []:
        act = p.get("nominal_action", {}) or {}
        phases.append(f"{act.get('skill')}({json.dumps(act.get('arguments', {}), ensure_ascii=False, sort_keys=True)})")
    failed = engine_payload.get("last_leaf_failure") or {}
    notes = engine_payload.get("notes") or []
    reason_parts = []
    for n in notes:
        if isinstance(n, dict) and n.get("message"):
            reason_parts.append(str(n["message"]))
        elif isinstance(n, str):
            reason_parts.append(n)
    summary = (
        f"Mission outcome: {state}. Plan phases: {' -> '.join(phases) or 'unknown'}. "
        f"Last failed leaf: {failed.get('node') or 'none'} ({failed.get('type') or 'n/a'}). "
        f"Reasons: {'; '.join(reason_parts) if reason_parts else 'none reported'}. "
        f"Elapsed: {engine_payload.get('elapsed_s', 'unknown')} s."
    )
    plan_skill_ids = [
        str((p.get("nominal_action") or {}).get("skill"))
        for p in (plan.get("phases", []) or [])
        if str((p.get("nominal_action") or {}).get("skill") or "").strip()
    ]
    metadata = {
        "mission_id": mission.get("mission_id"), "run_id": run_id, "outcome": state,
        "planner_version": settings.version,
        "plan_phases": phases, "plan_skill_ids": plan_skill_ids,
        "last_leaf_failure": failed, "notes": notes,
        "rag_context": {
            "allowed_skill_ids": (artifacts.get("rag_context") or {}).get("allowed_skill_ids", []),
            "patterns": [x.get("id") for x in (artifacts.get("rag_context") or {}).get("patterns", [])],
            "scene": [x.get("id") for x in (artifacts.get("rag_context") or {}).get("scene", [])],
        },
        "engine_payload": engine_payload,
    }
    with _db() as c:
        old = c.execute("SELECT experience_id FROM rag_experiences WHERE experience_id=?", (exp_id,)).fetchone()
        if old:
            c.execute("DELETE FROM rag_experiences_fts WHERE experience_id=?", (exp_id,))
        c.execute(
            "INSERT OR REPLACE INTO rag_experiences(experience_id,mission_id,run_id,mission,outcome,summary,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (exp_id, mission.get("mission_id"), run_id, mission.get("user_request", ""), state, summary, json.dumps(metadata, ensure_ascii=False), _now()),
        )
        c.execute("INSERT INTO rag_experiences_fts(experience_id,mission,summary) VALUES(?,?,?)", (exp_id, mission.get("user_request", ""), summary))
    return {"experience_id": exp_id, "summary": summary, "outcome": state}


def _feedback_path(mission_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(mission_id)).strip("._")
    if not safe_id:
        raise ValueError("mission_id cannot be represented as a feedback filename")
    return settings.state_root / "experience-feedback" / f"{safe_id}.json"


def get_user_feedback(mission_id: str) -> dict[str, Any] | None:
    path = _feedback_path(mission_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def record_user_feedback_experience(
    mission: dict[str, Any],
    execution: dict[str, Any],
    feedback: dict[str, Any],
) -> dict[str, Any]:
    """Persist structured human feedback as JSON and index it in Experience RAG."""
    init_rag_db()
    mission_id = str(mission.get("mission_id") or "")
    if not mission_id:
        raise ValueError("Mission record has no mission_id")
    now = _now()
    previous = get_user_feedback(mission_id) or {}
    params = dict(feedback.get("parameters") or {})
    record = {
        "schema_version": "1.0",
        "feedback_id": f"feedback:{mission_id}",
        "mission_id": mission_id,
        "session_id": mission.get("session_id"),
        "run_id": execution.get("run_id"),
        "execution_state": execution.get("state"),
        "mission": mission.get("user_request", ""),
        "rating": int(feedback["rating"]),
        "comment": str(feedback.get("comment") or ""),
        "parameters": params,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    path = _feedback_path(mission_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)

    recommendations: list[str] = []
    if params.get("set_gripper_position") is not None:
        recommendations.append(f"SetGripper.position={params['set_gripper_position']}")
    if params.get("navigate_to_detected_object_speed"):
        recommendations.append(
            f"NavigateToDetectedObject.speed={params['navigate_to_detected_object_speed']}"
        )
    if params.get("navigate_to_point_speed"):
        recommendations.append(f"NavigateToPoint.speed={params['navigate_to_point_speed']}")
    summary = (
        f"Human rating: {record['rating']}/5. "
        f"Recommended parameters: {', '.join(recommendations) if recommendations else 'none supplied'}. "
        f"User observation: {record['comment'] or 'none supplied'}. "
        f"Execution state: {record['execution_state']}."
    )
    artifacts = mission.get("artifacts") or {}
    plan = artifacts.get("task_plan_ir") or {}
    plan_skill_ids = [
        str((phase.get("nominal_action") or {}).get("skill"))
        for phase in plan.get("phases", []) or []
        if str((phase.get("nominal_action") or {}).get("skill") or "").strip()
    ]
    metadata = {
        **record,
        "plan_skill_ids": plan_skill_ids,
        "source": "human_post_execution_feedback",
        "json_path": str(path),
    }
    exp_id = str(record["feedback_id"])
    with _db() as c:
        c.execute("DELETE FROM rag_experiences_fts WHERE experience_id=?", (exp_id,))
        c.execute(
            "INSERT OR REPLACE INTO rag_experiences(experience_id,mission_id,run_id,mission,outcome,summary,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                exp_id,
                mission_id,
                execution.get("run_id"),
                mission.get("user_request", ""),
                f"user_rating_{record['rating']}",
                summary,
                json.dumps(metadata, ensure_ascii=False),
                record["created_at"],
            ),
        )
        c.execute(
            "INSERT INTO rag_experiences_fts(experience_id,mission,summary) VALUES(?,?,?)",
            (exp_id, mission.get("user_request", ""), summary),
        )
    return {"feedback": record, "experience_id": exp_id, "summary": summary, "json_path": str(path)}
