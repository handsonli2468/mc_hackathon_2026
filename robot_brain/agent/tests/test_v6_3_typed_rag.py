from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET

from agent.app.bootstrap import bootstrap_runtime
from agent.app.bt_engine_contract import build_merged_skill_registry, parse_bt_engine_tree_nodes_model
from agent.app.location_grounding import build_location_grounding
from agent.app.location_policy import apply_location_grounding_policy
from agent.app.mission_semantics import normalize_mission_semantics, validate_goal_effect_closure
from agent.app.rag import retrieve_context, sync_rag_knowledge
from agent.app.schemas import MissionRequirements, TaskPlanIR
from agent.app.settings import settings

ROOT = Path(__file__).resolve().parents[1]


def _write_synthetic_knowledge():
    bootstrap_runtime()
    loc_dir = settings.rag_knowledge_root / "locations"
    scene_dir = settings.rag_knowledge_root / "scene"
    loc_dir.mkdir(parents=True, exist_ok=True)
    scene_dir.mkdir(parents=True, exist_ok=True)
    loc_path = loc_dir / "v63_test_locations.yaml"
    scene_path = scene_dir / "v63_test_scene.yaml"
    loc_path.write_text(yaml.safe_dump({
        "locations": {
            "alpha_depot": {
                "enabled": True,
                "type": "STATIC_LOCATION",
                "frame_id": "map",
                "pose": {"x": 9.0, "y": 8.0, "yaw": 0.0},
                "yaw_unit": "rad",
                "map_version": "test_map",
                "calibrated": False,
                "aliases": ["alpha depot", "alpha drop point"],
                "description": "Synthetic named location for typed-RAG tests.",
            },
            "supply_zone": {
                "enabled": True,
                "type": "STATIC_REGION",
                "frame_id": "map",
                "entry_pose": {"x": 4.0, "y": 5.0, "yaw": 1.57079632679},
                "yaw_unit": "rad",
                "map_version": "test_map",
                "calibrated": False,
                "aliases": ["supply zone"],
                "description": "Synthetic search region for typed-RAG tests.",
            },
        }
    }, sort_keys=False), encoding="utf-8")
    scene_path.write_text(yaml.safe_dump({
        "documents": [{
            "id": "widget_storage_prior",
            "title": "Widget storage prior",
            "knowledge_type": "LOCATION_PRIOR",
            "content": "Widgets are usually stored in the supply zone. Search there before a broad local scan.",
            "target_terms": ["widget"],
            "tags": ["widget", "storage"],
            "location_ids": ["supply_zone"],
            "confidence": 0.95,
            "requires_visual_verification": True,
        }]
    }, sort_keys=False), encoding="utf-8")
    sync_rag_knowledge()
    return loc_path, scene_path


def _cleanup(paths):
    for path in paths:
        path.unlink(missing_ok=True)
    sync_rag_knowledge()


def test_named_location_is_retrieved_even_when_scene_rag_has_no_match():
    paths = _write_synthetic_knowledge()
    try:
        req = MissionRequirements.model_validate({
            "request_kind": "MISSION",
            "success_conditions": ["robot reaches destination"],
            "required_capabilities": ["navigate_to_location"],
            "task_semantics": {"operation": "NAVIGATE", "destination_targets": ["alpha depot"]},
            "retrieval_sketch": {
                "target_descriptions": ["alpha depot"],
                "skill_queries": ["navigate to a known map destination"],
                "scene_queries": ["completely unrelated environmental context"],
                "location_queries": ["alpha depot"],
            },
        }).model_dump(mode="json")
        ctx = retrieve_context("please go to alpha depot", req)
        assert "alpha_depot" in ctx["locations"]
        assert ctx["location_retrieval"]["alpha_depot"]["method"] == "exact_alias"
        assert ctx["location_grounding"]["locations"]["alpha_depot"]["actionable"] is True
        assert "NavigateToPoint" in ctx["allowed_skill_ids"]
    finally:
        _cleanup(paths)


def test_scene_prior_discovers_search_region_then_location_policy_moves_before_visual_search():
    paths = _write_synthetic_knowledge()
    try:
        req = MissionRequirements.model_validate({
            "request_kind": "MISSION",
            "success_conditions": ["widget found"],
            "required_capabilities": ["locate_object", "navigate_to_object"],
            "task_semantics": {"operation": "FIND_OBJECT", "object_targets": ["widget"]},
            "retrieval_sketch": {
                "target_descriptions": ["widget"],
                "skill_queries": ["locate a visual object"],
                "scene_queries": ["widget storage"],
                "location_queries": [],
            },
        }).model_dump(mode="json")
        ctx = retrieve_context("find a widget", req)
        assert any(x["location_id"] == "supply_zone" for x in ctx["search_priors"])
        plan = TaskPlanIR.model_validate({
            "mission_status": "EXECUTABLE",
            "goal": {"description": "find widget", "success_conditions": ["widget found"]},
            "termination_policy": {"success_conditions": ["widget found"]},
            "phases": [{
                "id": "look_widget",
                "objective": "locate widget",
                "nominal_action": {"skill": "VisualizeObject", "arguments": {"object_name": "widget"}},
                "verification": [{"condition": "IsObjectFound", "arguments": {"object_name": "widget"}}],
                "failure_modes": [],
            }],
        })
        result = apply_location_grounding_policy(plan, ctx)
        assert result["changed"] is True
        assert [p.nominal_action.skill for p in result["ir"].phases][:2] == ["NavigateToPoint", "VisualizeObject"]
        binding = next(x for x in result["bindings"] if x["binding_type"] == "scene_search_prior")
        assert binding["location_id"] == "supply_zone"
    finally:
        _cleanup(paths)


def test_relocate_semantics_completes_acquire_and_release_capabilities():
    req = MissionRequirements.model_validate({
        "request_kind": "MISSION",
        "success_conditions": ["object relocated"],
        "required_capabilities": ["locate_object"],
        "task_semantics": {
            "operation": "RELOCATE_OBJECT",
            "object_targets": ["object"],
            "destination_targets": ["destination"],
        },
    })
    result = normalize_mission_semantics(req)
    caps = set(result["requirements"].required_capabilities)
    assert {"locate_object", "navigate_to_object", "acquire_object", "release_object"} <= caps


def test_goal_closure_rejects_relocation_without_release_and_accepts_generic_capabilities():
    registry = {"skills": [
        {"id": "AcquireX", "kind": "ACTION", "provides": ["acquire_object"]},
        {"id": "MoveX", "kind": "ACTION", "provides": ["navigate_to_location"]},
        {"id": "ReleaseX", "kind": "ACTION", "provides": ["release_object"]},
    ]}
    req = MissionRequirements.model_validate({
        "request_kind": "MISSION",
        "success_conditions": ["object relocated"],
        "task_semantics": {"operation": "RELOCATE_OBJECT"},
    })
    def plan(actions):
        return TaskPlanIR.model_validate({
            "mission_status": "EXECUTABLE",
            "goal": {"description": "relocate", "success_conditions": ["done"]},
            "termination_policy": {"success_conditions": ["done"]},
            "phases": [
                {"id": f"p{i}", "objective": sid, "nominal_action": {"skill": sid, "arguments": {}}, "failure_modes": []}
                for i, sid in enumerate(actions)
            ],
        })
    bad = validate_goal_effect_closure(plan(["AcquireX", "MoveX"]), req, registry)
    assert bad["valid"] is False
    assert any("release" in x.lower() for x in bad["issues"])
    good = validate_goal_effect_closure(plan(["AcquireX", "MoveX", "ReleaseX"]), req, registry)
    assert good["valid"] is True


def test_latest_gripper_policy_exposes_reviewed_setgripper_only():
    default_xml = (ROOT / "defaults" / "bt_engine_nodes.xml").read_text(encoding="utf-8")
    engine = parse_bt_engine_tree_nodes_model(default_xml)
    overlay = yaml.safe_load((ROOT / "defaults" / "bt_engine_semantic_overlay.yaml").read_text(encoding="utf-8"))
    overlay = deepcopy(overlay)
    registry, report = build_merged_skill_registry(engine, overlay)
    ids = {x["id"] for x in registry["skills"]}
    assert "SetGripper" in ids
    assert registry["skills"]
    assert {"SetGripper", "VisualizeObject", "IsObjectFound"} <= ids
    assert {"OpenGripper", "GraspObject", "CloseGripper"}.isdisjoint(ids)
    assert report["semantic_complete"] is True


def test_validated_mode_rejects_uncalibrated_location(monkeypatch):
    import agent.app.location_grounding as lg
    strict = replace(settings, location_knowledge_mode="validated")
    monkeypatch.setattr(lg, "settings", strict)
    registry = yaml.safe_load((ROOT / "defaults" / "skill_registry.yaml").read_text(encoding="utf-8"))
    grounding = lg.build_location_grounding({
        "alpha": {
            "location_id": "alpha",
            "frame_id": "map",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "location_type": "STATIC_LOCATION",
            "map_version": "map_v1",
            "metadata": {"calibrated": False, "aliases": ["alpha"]},
        }
    }, registry=registry)
    assert grounding["locations"]["alpha"]["actionable"] is False
    assert "not calibrated" in grounding["locations"]["alpha"]["reason"]


def test_prompts_do_not_embed_default_site_specific_knowledge():
    prompt_dir = ROOT / "defaults" / "prompts"
    joined = "\n".join(p.read_text(encoding="utf-8").casefold() for p in prompt_dir.glob("*.md"))
    for forbidden in ("marker station", "presentation area", "meeting room", "whiteboard marker", "charging station"):
        assert forbidden not in joined


def test_cjk_location_alias_is_found_inside_full_user_sentence():
    bootstrap_runtime()
    loc_dir = settings.rag_knowledge_root / "locations"
    loc_dir.mkdir(parents=True, exist_ok=True)
    loc_path = loc_dir / "v63_cjk_location.yaml"
    loc_path.write_text(yaml.safe_dump({
        "locations": {
            "test_zone": {
                "enabled": True,
                "type": "STATIC_LOCATION",
                "frame_id": "map",
                "pose": {"x": 6.0, "y": 7.0, "yaw": 0.0},
                "yaw_unit": "rad",
                "calibrated": False,
                "aliases": ["測試區域"],
                "description": "Synthetic CJK location alias test.",
            }
        }
    }, sort_keys=False, allow_unicode=True), encoding="utf-8")
    sync_rag_knowledge()
    try:
        req = MissionRequirements.model_validate({
            "request_kind": "MISSION",
            "success_conditions": ["robot reaches destination"],
            "required_capabilities": ["navigate_to_location"],
            "task_semantics": {"operation": "NAVIGATE", "destination_targets": ["測試區域"]},
            "retrieval_sketch": {
                "target_descriptions": ["測試區域"],
                "location_queries": ["測試區域"],
                "skill_queries": ["navigate to a known map destination"],
            },
        }).model_dump(mode="json")
        ctx = retrieve_context("請你直接去測試區域等我", req)
        assert "test_zone" in ctx["locations"]
        assert ctx["location_retrieval"]["test_zone"]["method"] == "exact_alias"
        assert ctx["location_retrieval"]["test_zone"]["query_source"] == "user_message"
    finally:
        loc_path.unlink(missing_ok=True)
        sync_rag_knowledge()


def test_registered_scene_target_term_bypasses_fts_ranking_and_yields_search_prior():
    bootstrap_runtime()
    loc_dir = settings.rag_knowledge_root / "locations"
    scene_dir = settings.rag_knowledge_root / "scene"
    loc_dir.mkdir(parents=True, exist_ok=True)
    scene_dir.mkdir(parents=True, exist_ok=True)
    loc_path = loc_dir / "v63_direct_scene_location.yaml"
    scene_path = scene_dir / "v63_direct_scene_prior.yaml"
    loc_path.write_text(yaml.safe_dump({
        "locations": {
            "search_zone": {
                "enabled": True,
                "type": "STATIC_LOCATION",
                "frame_id": "map",
                "pose": {"x": 2.0, "y": 3.0, "yaw": 0.0},
                "yaw_unit": "rad",
                "calibrated": False,
                "aliases": ["搜尋區"],
            }
        }
    }, sort_keys=False, allow_unicode=True), encoding="utf-8")
    scene_path.write_text(yaml.safe_dump({
        "documents": [{
            "id": "cjk_item_prior",
            "enabled": True,
            "title": "Synthetic category prior",
            "knowledge_type": "LOCATION_PRIOR",
            "content": "A synthetic item category is usually found in a registered search zone.",
            "target_terms": ["測試物品"],
            "location_ids": ["search_zone"],
            "confidence": 0.95,
            "requires_visual_verification": True,
        }]
    }, sort_keys=False, allow_unicode=True), encoding="utf-8")
    sync_rag_knowledge()
    try:
        req = MissionRequirements.model_validate({
            "request_kind": "MISSION",
            "success_conditions": ["target found"],
            "required_capabilities": ["locate_object", "navigate_to_object"],
            "task_semantics": {"operation": "FIND_OBJECT", "object_targets": ["測試物品"]},
            "retrieval_sketch": {
                "target_descriptions": ["測試物品"],
                # Intentionally unrelated semantic scene query: the registered
                # target-term channel should still retrieve the prior.
                "scene_queries": ["unrelated room context"],
                "skill_queries": ["locate a visual object"],
            },
        }).model_dump(mode="json")
        ctx = retrieve_context("請幫我找一個測試物品", req)
        ids = {x["id"] for x in ctx["scene"]}
        assert "scene:cjk_item_prior" in ids
        doc = next(x for x in ctx["scene"] if x["id"] == "scene:cjk_item_prior")
        assert doc["direct_scene_match"]["method"] == "registered_target_term"
        assert any(x["location_id"] == "search_zone" for x in ctx["search_priors"])
    finally:
        loc_path.unlink(missing_ok=True)
        scene_path.unlink(missing_ok=True)
        sync_rag_knowledge()
