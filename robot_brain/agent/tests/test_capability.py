from agent.app.registry import check_capabilities

def test_exact_capability_matching():
    reg={"skills":[{"id":"FindObject","provides":["locate_object"]}]}
    assert check_capabilities(["locate_object"],reg)["ok"]
    result=check_capabilities(["open_refrigerator"],reg)
    assert not result["ok"] and result["missing"]==["open_refrigerator"]
