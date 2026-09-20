from pathlib import Path

import yaml

from agent.app.location_grounding import build_location_grounding
from agent.app.location_policy import apply_location_grounding_policy, validate_location_navigation_provenance
from agent.app.rag import planner_context_for_prompt
from agent.app.schemas import TaskPlanIR

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = yaml.safe_load((ROOT / "defaults" / "skill_registry.yaml").read_text(encoding="utf-8"))


def _location(*, calibrated=True, map_version="demo_meeting_room_v1"):
    meta = {
        "location_id": "marker_station",
        "type": "STATIC_LOCATION",
        "frame_id": "map",
        "pose": {"x": 1.2, "y": -0.8, "yaw": 1.57},
        "yaw_unit": "rad",
        "map_version": map_version,
        "description": "calibrated marker station",
        "calibrated": calibrated,
    }
    return {
        "location_id": "marker_station",
        "frame_id": "map",
        "x": 1.2,
        "y": -0.8,
        "yaw": 1.57,
        "location_type": "STATIC_LOCATION",
        "map_version": map_version,
        "metadata": meta,
    }


def _rag_context(grounding):
    return {
        "allowed_skill_ids": [
            "VisualizeObject", "IsObjectFound", "RotateInPlace",
            "NavigateToDetectedObject", "NavigateToPoint", "SetGripper",
        ],
        "locations": {"marker_station": _location(calibrated=True)},
        "location_grounding": grounding,
        "retrieved_skills": [],
        "patterns": [],
        "scene": [],
        "experiences": [],
        "ground_rules": [],
    }


def _latest_location_plan():
    return TaskPlanIR.model_validate({
        "mission_status": "EXECUTABLE",
        "goal": {"description": "place marker at station", "success_conditions": ["released"]},
        "termination_policy": {"success_conditions": ["released"]},
        "phases": [
            {
                "id": "step_2_navigate",
                "objective": "Navigate to marker station",
                "nominal_action": {"skill": "NavigateToPoint", "arguments": {"x": 1.2, "y": -0.8, "yaw_deg": "90", "speed": "normal"}},
                "verification": [],
                "failure_modes": [],
            },
            {
                "id": "step_3_open",
                "objective": "Release marker",
                "nominal_action": {"skill": "SetGripper", "arguments": {"position": 0}},
                "failure_modes": [],
            },
        ],
    })


def test_calibrated_static_location_binds_navigate_to_point_and_converts_yaw():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=True)},
        registry=REGISTRY,
        world_state={"map": {"version": "demo_meeting_room_v1"}},
    )
    info = grounding["locations"]["marker_station"]
    assert info["actionable"] is True
    assert grounding["expanded_skill_ids"] == ["NavigateToPoint"]
    candidate = info["navigation_candidates"][0]
    assert candidate["skill_id"] == "NavigateToPoint"
    assert candidate["arguments"]["x"] == 1.2
    assert candidate["arguments"]["y"] == -0.8
    assert 89.9 < float(candidate["arguments"]["yaw_deg"]) < 90.1


def test_integration_mode_allows_uncalibrated_location_for_pipeline_testing():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=False)},
        registry=REGISTRY,
    )
    info = grounding["locations"]["marker_station"]
    assert info["actionable"] is True
    assert grounding["expanded_skill_ids"] == ["NavigateToPoint"]
    assert info["trust_mode"] == "integration"
    assert any("uncalibrated" in w for w in info["warnings"])


def test_integration_mode_reports_but_allows_map_version_mismatch():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=True, map_version="map_v1")},
        registry=REGISTRY,
        world_state={"map": {"version": "map_v2"}},
    )
    info = grounding["locations"]["marker_station"]
    assert info["actionable"] is True
    assert any("map version mismatch" in w for w in info["warnings"])


def test_latest_location_plan_needs_no_legacy_object_navigation_rewrite():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=True)},
        registry=REGISTRY,
        world_state={"map": {"version": "demo_meeting_room_v1"}},
    )
    result = apply_location_grounding_policy(_latest_location_plan(), _rag_context(grounding))
    assert result["changed"] is False
    assert result["removed_visual_grounding_phases"] == []
    phases = result["ir"].phases
    assert [p.nominal_action.skill for p in phases] == ["NavigateToPoint", "SetGripper"]
    assert phases[0].nominal_action.arguments["x"] == 1.2
    assert phases[0].verification == []
    assert result["bindings"] == []


def test_navigate_to_point_must_match_an_actionable_rag_candidate():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=True)},
        registry=REGISTRY,
        world_state={"map": {"version": "demo_meeting_room_v1"}},
    )
    context = _rag_context(grounding)
    plan = _latest_location_plan()
    candidate = grounding["locations"]["marker_station"]["navigation_candidates"][0]
    plan.phases[0].nominal_action.arguments = {**candidate["arguments"], "speed": "slow"}

    valid = validate_location_navigation_provenance(plan, context)
    assert valid["valid"] is True
    assert len(valid["matches"]) == 1

    plan.phases[0].nominal_action.arguments["x"] = 99.0
    invalid = validate_location_navigation_provenance(plan, context)
    assert invalid["valid"] is False
    assert "do not match any actionable Location-RAG navigation_candidate" in invalid["errors"][0]


def test_prompt_exposes_only_bound_candidate_for_actionable_location():
    grounding = build_location_grounding(
        {"marker_station": _location(calibrated=True)},
        registry=REGISTRY,
        world_state={"map": {"version": "demo_meeting_room_v1"}},
    )
    text = planner_context_for_prompt(_rag_context(grounding))
    assert "NavigateToPoint" in text
    assert "navigation_candidates" in text
    assert "yaw_deg" in text


def test_full_rag_second_pass_adds_navigate_to_point_for_calibrated_marker_station():
    """Regression for the v6.1 bug: requirements may say navigate_to_object first,
    but Scene RAG must still be able to add a deterministic map-navigation skill.
    """
    from agent.app.bootstrap import bootstrap_runtime
    from agent.app.rag import retrieve_context, sync_rag_knowledge
    from agent.app.settings import settings

    bootstrap_runtime()
    location_path = settings.rag_knowledge_root / "locations" / "meeting_room.yaml"
    original = location_path.read_text(encoding="utf-8")
    try:
        location_doc = yaml.safe_load(original)
        marker = location_doc["locations"]["marker_station"]
        marker["calibrated"] = True
        marker["map_version"] = "demo_meeting_room_v1"
        location_path.write_text(yaml.safe_dump(location_doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        sync_rag_knowledge()

        requirements = {
            "request_kind": "MISSION",
            "status_hint": "READY",
            "success_conditions": [
                "Marker is placed next to the marker station",
                "Robot is no longer holding the marker",
            ],
            "required_capabilities": ["navigate_to_object", "release_object"],
            "required_user_information": [],
            "assumptions": [],
            "retrieval_sketch": {
                "semantic_concepts": ["object placement", "marker station", "holding object"],
                "target_descriptions": ["marker", "marker station"],
                "skill_queries": ["navigate to specific object type", "release held object"],
                "pattern_queries": ["place object at location"],
                "scene_queries": ["marker station location whiteboard marker"],
                "experience_queries": [],
            },
        }
        ctx = retrieve_context(
            "你現在已經抓著 marker 了，請你把它放到 marker station 旁邊",
            requirements,
            world_state={"map": {"version": "demo_meeting_room_v1"}},
        )

        assert "marker_station" in ctx["locations"]
        assert ctx["location_grounding"]["locations"]["marker_station"]["actionable"] is True
        assert "NavigateToPoint" in ctx["allowed_skill_ids"]
        assert "NavigateToPoint" in ctx["location_skill_expansion"]["added_skill_ids"]
        candidate = ctx["location_grounding"]["locations"]["marker_station"]["navigation_candidates"][0]
        assert candidate["skill_id"] == "NavigateToPoint"
        assert candidate["arguments"]["x"] == 1.2
        assert candidate["arguments"]["y"] == -0.8
    finally:
        location_path.write_text(original, encoding="utf-8")
        sync_rag_knowledge()
