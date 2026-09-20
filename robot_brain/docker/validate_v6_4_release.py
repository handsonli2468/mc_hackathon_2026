#!/usr/bin/env python3
"""Dependency-light release checks for the v6.4 contract and shipped data."""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "agent" / "defaults"
EXPECTED_CUSTOM = {
    "IsObjectFound", "NavigateToDetectedObject", "NavigateToPoint", "Patrol",
    "RotateInPlace", "SetGripper", "VisualizeObject",
}
EXPECTED_BUILTIN = {
    "Delay", "Fallback", "ForceFailure", "ForceSuccess", "Inverter",
    "KeepRunningUntilFailure", "ReactiveFallback", "ReactiveSequence", "Repeat",
    "RetryUntilSuccessful", "Sequence", "SubTree", "Timeout",
}
EXPECTED_PORTS = {
    "IsObjectFound": {"object_name"},
    "NavigateToDetectedObject": {
        "speed", "replan_distance", "arrive_tolerance", "standoff",
    },
    "NavigateToPoint": {"yaw_deg", "speed", "y", "x"},
    "Patrol": {"speed"},
    "RotateInPlace": {"angle_deg"},
    "SetGripper": {"position"},
    "VisualizeObject": {"object_name"},
}


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> None:
    # Validate shipped source data only. Manta keeps generated evaluation XML and
    # operator-owned runtime data under agent-runtime; a failed mission may
    # legitimately leave an empty diagnostic .xml and must not break release-data
    # validation on the next run.
    yaml_paths = list(DEFAULTS.rglob("*.yaml")) + list((ROOT / "docs").glob("*.yaml"))
    xml_paths = [DEFAULTS / "bt_engine_nodes.xml", *(ROOT / "examples").glob("*.xml")]
    for path in yaml_paths:
        load_yaml(path)
    for path in xml_paths:
        ET.fromstring(path.read_text(encoding="utf-8"))

    model = ET.fromstring((DEFAULTS / "bt_engine_nodes.xml").read_text(encoding="utf-8"))
    declared = {node.attrib["ID"] for node in model.findall(".//TreeNodesModel/*")}
    assert declared == EXPECTED_CUSTOM, (declared, EXPECTED_CUSTOM)

    custom_model_ports = {}
    for node in model.findall(".//TreeNodesModel/*"):
        node_id = node.attrib["ID"]
        if node_id in EXPECTED_CUSTOM:
            custom_model_ports[node_id] = {
                port.attrib["name"] for port in node if port.tag.endswith("_port")
            }
    assert custom_model_ports == EXPECTED_PORTS, custom_model_ports

    registry = load_yaml(DEFAULTS / "skill_registry.yaml")
    skills = {item["id"]: item for item in registry["skills"]}
    assert set(skills) == EXPECTED_CUSTOM, set(skills)
    assert {node_id: set(spec.get("inputs", {})) for node_id, spec in skills.items()} == EXPECTED_PORTS
    gripper = skills["SetGripper"]
    assert gripper["inputs"]["position"]["min"] == 0
    assert gripper["inputs"]["position"]["max"] == 100
    assert gripper["capability_rules"]["release_object"]["equals"] == 0
    assert gripper["recommended_verification"] == []
    assert skills["NavigateToDetectedObject"]["inputs"]["standoff"]["default"] == 0.05

    builtins = load_yaml(DEFAULTS / "builtin_bt_nodes.yaml")
    assert {item["id"] for item in builtins["nodes"]} == EXPECTED_BUILTIN
    assert all(str(item.get("description") or "").strip() for item in builtins["nodes"])
    overlay = load_yaml(DEFAULTS / "bt_engine_semantic_overlay.yaml")
    assert set(overlay["nodes"]) == EXPECTED_CUSTOM
    assert overlay["nodes"]["VisualizeObject"]["continuous_search_trigger"] is True
    assert overlay["nodes"]["VisualizeObject"]["search_policy"] == {
        "condition": "IsObjectFound",
        "activity": "Patrol",
        "control": "ReactiveFallback",
        "timeout_sec": 60,
        "timeout_outcome": "MISSION_FAILURE",
    }
    assert overlay["nodes"]["Patrol"]["search_policy_role"] == "continuous_patrol"
    policy = load_yaml(DEFAULTS / "bt_skill_policy.yaml")
    assert set(policy["required_planner_nodes"]) == EXPECTED_CUSTOM
    assert not policy["disabled_nodes"]

    formal = load_yaml(DEFAULTS / "bt_engine_registry.generated.yaml")
    assert {item["id"] for item in formal["nodes"]} == EXPECTED_CUSTOM

    allowed_xml_tags = EXPECTED_CUSTOM | EXPECTED_BUILTIN | {"root", "BehaviorTree", "TreeNodesModel"}
    for path in (ROOT / "examples").glob("*.xml"):
        example = ET.fromstring(path.read_text(encoding="utf-8"))
        unexpected = {el.tag for el in example.iter()} - allowed_xml_tags
        assert not unexpected, f"{path}: obsolete/unknown node tags {sorted(unexpected)}"

    search_example = ET.fromstring((ROOT / "examples" / "formal_bottle_bt.xml").read_text(encoding="utf-8"))
    monitor = search_example.find(".//Timeout/ReactiveFallback")
    assert monitor is not None
    assert [child.tag for child in list(monitor)] == ["IsObjectFound", "Patrol"]
    assert search_example.find(".//RetryUntilSuccessful") is None
    assert search_example.find(".//ForceFailure") is None

    source = (ROOT / "agent" / "app" / "main.py").read_text(encoding="utf-8")
    assert '"/api/chat"' in source
    assert '"/api/sessions/reset"' in source
    assert '"/api/missions/{mission_id}/feedback"' in source
    assert "response_model=ChatResponse" in source
    assert "_attach_auto_execution" in source
    assert 'os.getenv("BT_ENGINE_AUTO_EXECUTE", "1")' in (ROOT / "agent" / "app" / "settings.py").read_text(encoding="utf-8")
    assert "BT_ENGINE_AUTO_EXECUTE=1" in (DEFAULTS / "settings.env").read_text(encoding="utf-8")
    assert '"options": {"auto_execute": False}' in (ROOT / "agent" / "tests" / "run_timing_profile.py").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "engineExecuteBtn" not in frontend
    assert "engineValidateBtn" not in frontend
    direct_prompt = (DEFAULTS / "prompts" / "direct_bt.md").read_text(encoding="utf-8")
    assert "Timeout(ReactiveFallback(IsObjectFound, Patrol))" in direct_prompt
    assert "KNOWN BEHAVIORTREE.CPP NATIVE SEMANTICS" in direct_prompt
    assert "start at position=50" in direct_prompt
    assert "Location-RAG navigation_candidate" in direct_prompt
    location_policy = (ROOT / "agent" / "app" / "location_policy.py").read_text(encoding="utf-8")
    assert "validate_location_navigation_provenance" in location_policy
    print("v6.4 release data validation passed")


if __name__ == "__main__":
    main()
