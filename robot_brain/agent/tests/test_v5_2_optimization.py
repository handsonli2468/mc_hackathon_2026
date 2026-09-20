import asyncio
from copy import deepcopy
from pathlib import Path

import yaml
from pydantic import BaseModel

from agent.app import model_client
from agent.app.plan_hardening import harden_plan_for_quality
from agent.app.quality_gate import assess_plan_quality
from agent.app.schemas import TaskPlanIR
from agent.app.settings import settings

ROOT = Path(__file__).resolve().parents[1]
DEMO = yaml.safe_load((ROOT / "defaults" / "skill_registry.demo.yaml").read_text(encoding="utf-8"))


class _Tiny(BaseModel):
    value: int


def make_search_ir(with_recovery: bool = False) -> TaskPlanIR:
    recovery = []
    if with_recovery:
        recovery = [
            {
                "strategy": "REFRESH_INFORMATION",
                "skill": "RotateInPlace",
                "arguments": {"degrees": 45},
            }
        ]
    return TaskPlanIR.model_validate(
        {
            "schema_version": "1.0",
            "mission_status": "EXECUTABLE",
            "goal": {"description": "find bottle", "success_conditions": ["bottle located"]},
            "required_capabilities": [],
            "missing_capabilities": [],
            "required_user_information": [],
            "assumptions": [],
            "global_constraints": [],
            "termination_policy": {
                "success_conditions": ["bottle located"],
                "failure_conditions": ["search exhausted"],
                "max_mission_duration_sec": 300,
                "on_unrecoverable_failure": "MISSION_FAILURE",
            },
            "phases": [
                {
                    "id": "locate_bottle",
                    "objective": "find bottle",
                    "desired_state": ["localized"],
                    "preconditions": [],
                    "nominal_action": {"skill": "FindObject", "arguments": {"query": "bottle"}},
                    "verification": [
                        {"condition": "IsObjectLocated", "arguments": {"object": "FindObject.object"}}
                    ],
                    "failure_modes": [
                        {
                            "failure": "OBJECT_NOT_FOUND",
                            "classification": "STALE_INFORMATION",
                            "recovery": recovery,
                            "max_attempts": 2,
                            "timeout_sec": 20,
                            "escalation": None,
                            "on_exhaustion": "MISSION_FAILURE",
                        }
                    ],
                    "stale_dependencies": ["object_pose"],
                    "side_effects": [],
                    "resource_requirements": ["CAMERA"],
                    "cleanup": [],
                }
            ],
        }
    )


def test_v52_hardens_missing_search_recovery_without_mutating_raw_plan():
    raw = make_search_ir(False)
    out = harden_plan_for_quality(raw, DEMO)
    hardened = out["ir"]

    assert out["changed"] is True
    assert raw.phases[0].failure_modes[0].recovery == []
    failure = hardened.phases[0].failure_modes[0]
    assert failure.recovery[0].skill == "RotateInPlace"
    assert failure.recovery[0].arguments["degrees"] == settings.auto_search_recovery_degrees
    assert failure.max_attempts >= 8
    assert assess_plan_quality(hardened, DEMO)["pass"] is True


def test_v52_does_not_duplicate_existing_viewpoint_recovery():
    raw = make_search_ir(True)
    out = harden_plan_for_quality(raw, DEMO)
    assert out["changed"] is False
    assert len(out["ir"].phases[0].failure_modes[0].recovery) == 1


def test_v52_refuses_to_guess_when_viewpoint_skill_is_ambiguous():
    registry = deepcopy(DEMO)
    rotate = next(s for s in registry["skills"] if s["id"] == "RotateInPlace")
    duplicate = deepcopy(rotate)
    duplicate["id"] = "RotateInPlaceAlternative"
    registry["skills"].append(duplicate)

    out = harden_plan_for_quality(make_search_ir(False), registry)
    assert out["changed"] is False
    assert any("exactly one viewpoint-changing ACTION" in x for x in out["warnings"])


def test_planner_json_uses_larger_retry_thinking_budget(monkeypatch):
    budgets = []
    responses = ['{"value":"not-an-int"}', '{"value":7}']

    async def fake_planner_chat(*args, **kwargs):
        budgets.append(kwargs.get("thinking_token_budget"))
        return responses.pop(0)

    monkeypatch.setattr(model_client, "planner_chat", fake_planner_chat)
    result = asyncio.run(
        model_client.planner_json(
            _Tiny,
            [{"role": "user", "content": "return JSON"}],
            retries=1,
            thinking_token_budget=3072,
            retry_thinking_token_budget=4096,
            operation="test.v52",
        )
    )
    assert result.value == 7
    assert budgets == [3072, 4096]
