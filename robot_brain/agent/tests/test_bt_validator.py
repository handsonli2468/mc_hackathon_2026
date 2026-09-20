from agent.app.bt_xml import validate_bt_xml

LEAF={"blackboard":{"externally_provided":{}},"nodes":[
 {"id":"FindObject","kind":"Action","children":{"min":0,"max":0},"ports":{"input":{"query":{"type":"string","required":True}},"output":{"object":{"type":"TrackedObject","required":True}}}},
 {"id":"PickObject","kind":"Action","children":{"min":0,"max":0},"ports":{"input":{"object":{"type":"TrackedObject","required":True}},"output":{}}}
]}
BUILTIN={"nodes":[
 {"id":"Sequence","kind":"Control","children":{"min":1}},
 {"id":"RetryUntilSuccessful","kind":"Decorator","children":{"min":1,"max":1},"ports":{"input":{"num_attempts":{"type":"int","required":True,"min":1,"max":10}}}},
 {"id":"AlwaysSuccess","kind":"Action","children":{"min":0,"max":0}}
]}

def test_unknown_node():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><Fly name="x"/></BehaviorTree></root>'
    assert not validate_bt_xml(x,LEAF,BUILTIN)["valid"]

def test_retry_requires_bound():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><RetryUntilSuccessful name="r"><AlwaysSuccess name="ok"/></RetryUntilSuccessful></BehaviorTree></root>'
    assert not validate_bt_xml(x,LEAF,BUILTIN)["valid"]

def test_blackboard_dataflow():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><Sequence name="s"><FindObject name="f" query="cup" object="{target}"/><PickObject name="p" object="{target}"/></Sequence></BehaviorTree></root>'
    assert validate_bt_xml(x,LEAF,BUILTIN)["valid"]

def test_missing_blackboard_producer():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><PickObject name="p" object="{target}"/></BehaviorTree></root>'
    assert not validate_bt_xml(x,LEAF,BUILTIN)["valid"]

def test_output_must_be_blackboard_binding():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><FindObject name="f" query="cup" object="target"/></BehaviorTree></root>'
    result=validate_bt_xml(x,LEAF,BUILTIN)
    assert not result["valid"]
    assert any("output 'object' must bind" in e for e in result["errors"])

def test_opaque_input_cannot_be_literal():
    x='<root main_tree_to_execute="Main"><BehaviorTree ID="Main"><PickObject name="p" object="target"/></BehaviorTree></root>'
    result=validate_bt_xml(x,LEAF,BUILTIN)
    assert not result["valid"]
    assert any("opaque type" in e for e in result["errors"])
