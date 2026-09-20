from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET
from xml.etree import ElementTree as ET

from agent.app.bt_engine_contract import (
    build_merged_skill_registry,
    engine_model_as_registry,
    parse_bt_engine_tree_nodes_model,
)
from agent.app.phase_compiler import prepare_phase_candidate_xml, validate_phase_candidate
from agent.app.rag import _filter_patterns_by_intent, _filter_scene_by_anchor, _scene_anchor_terms
from agent.app.schemas import Phase


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XML = (ROOT / "defaults" / "bt_engine_nodes.xml").read_text(encoding="utf-8")
OVERLAY = yaml.safe_load((ROOT / "defaults" / "bt_engine_semantic_overlay.yaml").read_text(encoding="utf-8"))
BUILTINS = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def _with_navigate_xy(xml: str) -> str:
    root = SafeET.fromstring(xml)
    model = root if root.tag == "TreeNodesModel" else root.find(".//TreeNodesModel")
    node = ET.SubElement(model, "Action", {"ID": "NavigateToXY"})
    for name in ("x", "y", "yaw"):
        ET.SubElement(node, "input_port", {"name": name, "type": "double"})
    return ET.tostring(root, encoding="unicode")


def _navigate_overlay():
    overlay = yaml.safe_load(yaml.safe_dump(OVERLAY))
    overlay["nodes"]["NavigateToXY"] = {
        "planner_enabled": True,
        "description": "Navigate to a calibrated map pose.",
        "provides": ["navigate_to_location", "navigate_to_map_pose"],
        "preconditions": [],
        "success_semantics": ["robot_at_map_pose"],
        "recommended_verification": [],
        "suitable_for": ["calibrated_static_scene_location"],
        "requires_visual_grounding": False,
    }
    return overlay


def _phase() -> Phase:
    return Phase.model_validate({
        "id": "locate",
        "objective": "find bottle",
        "desired_state": ["object_visible"],
        "preconditions": [],
        "nominal_action": {"skill": "VisualizeObject", "arguments": {"object_name": "bottle"}},
        "verification": [{"condition": "IsObjectFound", "arguments": {"object_name": "bottle"}}],
        "failure_modes": [],
        "stale_dependencies": [],
        "side_effects": [],
        "resource_requirements": ["CAMERA"],
        "cleanup": [],
    })


def test_every_live_node_is_formally_ingested_even_without_semantics():
    engine = parse_bt_engine_tree_nodes_model(_with_navigate_xy(DEFAULT_XML))
    registry, report = build_merged_skill_registry(engine, OVERLAY)
    formal = engine_model_as_registry(engine)

    assert report["all_engine_nodes_formally_ingested"] is True
    assert report["engine_node_count"] == len(engine) == 8
    assert len(formal["nodes"]) == 8
    assert "NavigateToXY" in report["formal_ingested_node_ids"]
    assert "NavigateToXY" in report["missing_semantic_overlay"]
    assert "NavigateToXY" not in {s["id"] for s in registry["skills"]}


def test_registered_subtree_manifests_are_not_treated_as_node_types():
    xml = '''<root BTCPP_format="4"><TreeNodesModel>
      <Action ID="VisualizeObject"><input_port name="object_name" type="std::string"/></Action>
      <SubTree ID="GoHome"/>
      <SubTree ID="TourTheCorners"/>
    </TreeNodesModel></root>'''
    engine = parse_bt_engine_tree_nodes_model(xml)

    assert set(engine) == {"VisualizeObject"}


def test_reviewed_semantic_overlay_promotes_new_live_node_with_formal_ports():
    engine = parse_bt_engine_tree_nodes_model(_with_navigate_xy(DEFAULT_XML))
    registry, report = build_merged_skill_registry(engine, _navigate_overlay())
    skills = {s["id"]: s for s in registry["skills"]}

    assert report["semantic_complete"] is True
    assert "NavigateToXY" in skills
    assert set(skills["NavigateToXY"]["inputs"]) == {"x", "y", "yaw"}
    assert all(spec["type"] == "double" for spec in skills["NavigateToXY"]["inputs"].values())
    assert all(spec["required"] is True for spec in skills["NavigateToXY"]["inputs"].values())
    assert skills["NavigateToXY"]["requires_visual_grounding"] is False


def test_invalid_overlay_port_is_quarantined_not_silently_accepted():
    engine = parse_bt_engine_tree_nodes_model(_with_navigate_xy(DEFAULT_XML))
    overlay = _navigate_overlay()
    overlay["nodes"]["NavigateToXY"]["inputs"] = {"map_x": {"literal_only": True}}
    registry, report = build_merged_skill_registry(engine, overlay)

    assert "NavigateToXY" not in {s["id"] for s in registry["skills"]}
    assert "NavigateToXY" in report["invalid_semantic_overlay"]
    assert "unknown input ports" in " ".join(report["invalid_semantic_overlay"]["NavigateToXY"])


def test_verification_dependency_is_quarantined_transitively():
    engine = parse_bt_engine_tree_nodes_model(DEFAULT_XML)
    engine.pop("IsObjectFound")
    registry, report = build_merged_skill_registry(engine, OVERLAY)
    ids = {s["id"] for s in registry["skills"]}

    assert "VisualizeObject" not in ids
    assert "VisualizeObject" in report["invalid_semantic_overlay"]


def test_scene_rag_user_entity_anchor_cannot_be_self_overridden_by_llm_expansion():
    scene = [
        {"id": "scene:presentation_remote_prior", "title": "presentation remote prior", "content": "remote presentation area", "metadata": {}},
        {"id": "scene:cup_prior", "title": "cup prior", "content": "cups are often near the coffee table", "metadata": {}},
    ]
    hard = _scene_anchor_terms("find the cup")
    soft = _scene_anchor_terms("presentation remote presentation area cup")
    filtered = _filter_scene_by_anchor(scene, hard, soft)

    assert [x["id"] for x in filtered] == ["scene:cup_prior"]
    assert filtered[0]["anchor_source"] == "user_message"


def test_human_clarification_pattern_requires_real_missing_information():
    docs = [
        {"id": "pattern:human_clarification"},
        {"id": "pattern:bounded_visual_search"},
    ]
    without_need = _filter_patterns_by_intent(docs, ["search_object"], {}, ["bottle"], needs_clarification=False)
    with_need = _filter_patterns_by_intent(docs, ["search_object"], {}, ["bottle"], needs_clarification=True)

    assert "pattern:human_clarification" not in {d["id"] for d in without_need}
    assert "pattern:human_clarification" in {d["id"] for d in with_need}


def test_phase_candidate_sanitizer_strips_model_owned_policy_without_inventing_leaves():
    raw = '''```xml
<root BTCPP_format="4" main_tree_to_execute="MainTree">
  <BehaviorTree ID="MainTree">
    <RetryUntilSuccessful num_attempts="3">
      <Sequence>
        <VisualizeObject object_name="bottle"/>
        <IsObjectFound object_name="bottle"/>
      </Sequence>
    </RetryUntilSuccessful>
  </BehaviorTree>
</root>
```'''
    prepared = prepare_phase_candidate_xml(raw, _phase())
    assert prepared["error"] is None
    assert any("RetryUntilSuccessful" in x for x in prepared["changes"])
    result = validate_phase_candidate(prepared["xml"], _phase(), BUILTINS)
    assert result["valid"], result["errors"]
    assert "RetryUntilSuccessful" not in result["subtree_xml"]
