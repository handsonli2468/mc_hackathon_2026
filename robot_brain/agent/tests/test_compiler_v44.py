from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import extract_bt_xml, validate_bt_xml
from agent.app.phase_compiler import assemble_phase_subtrees, validate_phase_candidate
from agent.app.registry import skill_registry_as_bt_nodes
from agent.tests.test_compiler_v43 import make_ir

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def test_extract_bt_xml_uses_last_complete_tree_like_official_notebook():
    text = '<root><BehaviorTree ID="A"><Success/></BehaviorTree></root> noise <root><BehaviorTree ID="B"><Success/></BehaviorTree></root>'
    out = extract_bt_xml(text)
    assert 'ID="B"' in out


def test_phase_candidate_rejects_hallucinated_leaf_and_repetition():
    ir = make_ir()
    phase = ir.phases[0]
    bad = '''<root main_tree_to_execute="MainTree"><BehaviorTree ID="MainTree"><Sequence>
      <Action ID="FindObject"/><Action ID="GetObjectPose"/><Action ID="isObjectLocated"/><Action ID="isObjectLocated"/>
    </Sequence></BehaviorTree></root>'''
    result = validate_phase_candidate(bad, phase, BUILTIN)
    assert not result["valid"]
    assert any("GetObjectPose" in e for e in result["errors"])
    assert any("appears 2 times" in e for e in result["errors"])


def test_phase_candidate_accepts_official_style_action_wrappers_and_case_normalization():
    ir = make_ir()
    phase = ir.phases[0]
    raw = '''<root BTCPP_format="4" main_tree_to_execute="BehaviorTree"><BehaviorTree ID="BehaviorTree1">
      <Sequence><Action ID="FindObject"/><Action ID="isObjectLocated"/></Sequence>
    </BehaviorTree></root>'''
    result = validate_phase_candidate(raw, phase, BUILTIN)
    assert result["valid"], result
    assert result["subtree_xml"] is not None


def test_phase_assembly_adds_policy_wrappers_deterministically_and_validates():
    ir = make_ir()
    phase_subtrees = {
        "locate_bottle": '<Sequence><Action ID="FindObject"/><Action ID="IsObjectLocated"/></Sequence>',
        "navigate_to_bottle": '<Sequence><Action ID="NavigateToObject"/><Action ID="IsNearObject"/></Sequence>',
        "pick_bottle": '<Sequence><Action ID="PickObject"/><Action ID="IsObjectHeld"/></Sequence>',
    }
    assembled = assemble_phase_subtrees(ir, phase_subtrees, DEMO)
    normalized = normalize_compiled_bt_xml(assembled, ir, DEMO, BUILTIN)
    assert normalized["error"] is None
    xml = normalized["xml"]
    root = SafeET.fromstring(xml)
    retries = list(root.iter("RetryUntilSuccessful"))
    timeouts = list(root.iter("Timeout"))
    assert len(retries) == 3
    assert [r.attrib["num_attempts"] for r in retries] == ["2", "2", "2"]
    assert len(timeouts) == 3

    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(xml, ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic
