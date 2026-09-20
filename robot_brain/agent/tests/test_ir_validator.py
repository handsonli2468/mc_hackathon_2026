from agent.app.schemas import TaskPlanIR
from agent.app.ir_validator import validate_task_plan_ir

REG={"skills":[
 {"id":"PickObject","kind":"ACTION","provides":["grasp_object"],"inputs":{"object":{"required":True,"type":"TrackedObject"}},"recommended_verification":["IsObjectHeld"],"verification_required":True},
 {"id":"IsObjectHeld","kind":"CONDITION","provides":["verify_object_held"],"inputs":{"object":{"required":True,"type":"TrackedObject"}}}
]}

def make_ir(verify=True):
    return TaskPlanIR.model_validate({
        "schema_version":"1.0",
        "mission_status":"EXECUTABLE",
        "goal":{"description":"hold object","success_conditions":["object held"]},
        "required_capabilities":["grasp_object","verify_object_held"],
        "missing_capabilities":[],
        "required_user_information":[],
        "assumptions":[],
        "global_constraints":[],
        "termination_policy":{
            "success_conditions":["object held"],
            "failure_conditions":["recovery exhausted"],
            "max_mission_duration_sec":120,
            "on_unrecoverable_failure":"MISSION_FAILURE"
        },
        "phases":[{
            "id":"pick",
            "objective":"pick",
            "desired_state":["object held"],
            "preconditions":[],
            "nominal_action":{"skill":"PickObject","arguments":{"object":"{target}"}},
            "verification":[{"condition":"IsObjectHeld","arguments":{"object":"{target}"}}] if verify else [],
            "failure_modes":[{
                "failure":"GRASP_FAILED","classification":"TRANSIENT",
                "recovery":[{"strategy":"RETRY","skill":"PickObject","arguments":{"object":"{target}"}}],
                "max_attempts":2,"on_exhaustion":"MISSION_FAILURE"
            }],
            "stale_dependencies":[],"side_effects":[],"resource_requirements":[],"cleanup":[]
        }]
    })

def test_valid_ir():
    assert validate_task_plan_ir(make_ir(),REG)["valid"]

def test_missing_verification_rejected():
    assert not validate_task_plan_ir(make_ir(False),REG)["valid"]

def test_verification_arguments_are_checked():
    ir=make_ir()
    ir.phases[0].verification[0].arguments={}
    result=validate_task_plan_ir(ir,REG)
    assert not result["valid"]
    assert any("verification" in e and "missing arguments" in e for e in result["errors"])

def test_permanent_failure_with_retry_is_rejected():
    ir = make_ir()
    ir.phases[0].failure_modes[0].classification = "PERMANENT"
    result = validate_task_plan_ir(ir, REG)
    assert not result["valid"]
    assert any("classified PERMANENT" in e for e in result["errors"])
