from pathlib import Path

import yaml
from defusedxml import ElementTree as SafeET

from agent.app.bt_normalizer import normalize_compiled_bt_xml
from agent.app.bt_semantic import validate_bt_semantics
from agent.app.bt_xml import validate_bt_xml
from agent.app.compiler_adapter import ensure_state_verifications
from agent.app.ir_normalizer import compiler_capability_contract
from agent.app.phase_compiler import assemble_phase_subtrees, deterministic_phase_subtree
from agent.app.registry import skill_registry_as_bt_nodes
from agent.tests.test_compiler_v43 import make_ir

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))
BUILTIN = yaml.safe_load((ROOT / "defaults" / "builtin_bt_nodes.yaml").read_text(encoding="utf-8"))


def test_status_aware_fallback_ignores_failure_only_recovery_when_proving_success_outputs():
    xml = '''<root BTCPP_format="4" main_tree_to_execute="Main"><BehaviorTree ID="Main">
    <Sequence name="mission">
      <RetryUntilSuccessful name="search_retry" num_attempts="8">
        <Fallback name="search_if_else">
          <Sequence name="search_nominal">
            <FindObject name="find" query="bottle" object="{target}"/>
            <IsObjectLocated name="located" object="{target}"/>
          </Sequence>
          <ForceFailure name="retry_after_rotate">
            <RotateInPlace name="rotate" degrees="45"/>
          </ForceFailure>
        </Fallback>
      </RetryUntilSuccessful>
      <NavigateToObject name="navigate" object="{target}"/>
    </Sequence>
    </BehaviorTree></root>'''
    result = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    assert result["valid"], result


def test_status_aware_fallback_rejects_success_branch_that_does_not_produce_required_output():
    xml = '''<root BTCPP_format="4" main_tree_to_execute="Main"><BehaviorTree ID="Main">
    <Sequence name="mission">
      <Fallback name="unsafe_selector">
        <FindObject name="find" query="bottle" object="{target}"/>
        <ForceSuccess name="pretend_success"><RotateInPlace name="rotate" degrees="45"/></ForceSuccess>
      </Fallback>
      <NavigateToObject name="navigate" object="{target}"/>
    </Sequence>
    </BehaviorTree></root>'''
    result = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    assert not result["valid"]
    assert any("before a guaranteed producer" in e for e in result["errors"])


def test_ensure_state_precheck_is_only_added_when_inputs_exist_before_phase_action():
    ir = make_ir()
    assert ensure_state_verifications(ir, 0, DEMO) == []  # IsObjectLocated needs FindObject's current-phase output.
    assert [x.condition for x in ensure_state_verifications(ir, 1, DEMO)] == ["IsNearObject"]
    assert [x.condition for x in ensure_state_verifications(ir, 2, DEMO)] == ["IsObjectHeld"]


def test_v5_1_compiles_observable_if_else_guards_without_simplifying_nominal_xml():
    ir = make_ir()
    phase_subtrees = {phase.id: deterministic_phase_subtree(phase) for phase in ir.phases}
    assembled = assemble_phase_subtrees(ir, phase_subtrees, DEMO, BUILTIN)
    normalized = normalize_compiled_bt_xml(assembled, ir, DEMO, BUILTIN)
    assert normalized["error"] is None, normalized
    xml = normalized["xml"]

    assert "locate_bottle_ensure_state" not in xml
    assert "navigate_to_bottle_ensure_state" in xml
    assert "pick_bottle_ensure_state" in xml
    assert "navigate_to_bottle_ensure_precheck_1_isnearobject" in xml
    assert "pick_bottle_ensure_precheck_1_isobjectheld" in xml

    root = SafeET.fromstring(xml)
    nav_guard = next(e for e in root.iter("Fallback") if e.attrib.get("name") == "navigate_to_bottle_ensure_state")
    assert list(nav_guard)[0].tag == "IsNearObject"
    assert any(e.tag == "NavigateToObject" for e in list(nav_guard)[1].iter())

    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(xml, ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic


def test_compiler_contract_declares_observable_ensure_state_guard():
    caps = compiler_capability_contract()
    assert caps["contract_version"] == "1.4"
    assert caps["observable_search_recovery"] is True
    assert caps["observable_ensure_state_guard"] is True
    assert caps["failure_specific_recovery"] is False


def test_full_bottle_tree_combines_search_recovery_with_downstream_ensure_guards_and_validates():
    raw = make_ir().model_dump(mode="json")
    locate_failure = raw["phases"][0]["failure_modes"][0]
    locate_failure["failure"] = "OBJECT_NOT_FOUND"
    locate_failure["classification"] = "STALE_INFORMATION"
    locate_failure["max_attempts"] = 8
    locate_failure["recovery"] = [
        {
            "strategy": "REFRESH_INFORMATION",
            "skill": "RotateInPlace",
            "arguments": {"degrees": 45},
        }
    ]
    ir = type(make_ir()).model_validate(raw)

    phase_subtrees = {phase.id: deterministic_phase_subtree(phase) for phase in ir.phases}
    assembled = assemble_phase_subtrees(ir, phase_subtrees, DEMO, BUILTIN)
    normalized = normalize_compiled_bt_xml(assembled, ir, DEMO, BUILTIN)
    assert normalized["error"] is None, normalized
    xml = normalized["xml"]

    assert "locate_bottle_observation_fallback" in xml
    assert "locate_bottle_retry_after_recovery" in xml
    assert "navigate_to_bottle_ensure_state" in xml
    assert "pick_bottle_ensure_state" in xml

    structural = validate_bt_xml(xml, skill_registry_as_bt_nodes(DEMO), BUILTIN)
    semantic = validate_bt_semantics(xml, ir, DEMO)
    assert structural["valid"], structural
    assert semantic["valid"], semantic
