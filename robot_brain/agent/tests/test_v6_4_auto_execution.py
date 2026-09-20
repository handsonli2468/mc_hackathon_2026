from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from agent.app import main


def _candidate(*, generated: bool = True) -> dict:
    return {
        "pipeline": "hybrid",
        "status": "SUCCESS" if generated else "PLANNING_FAILURE",
        "message": "done",
        "questions": [],
        "mission_id": "mission-auto-1" if generated else None,
        "bt_xml": "<root/>" if generated else None,
        "bt_generation": {
            "succeeded": generated,
            "validated": generated,
            "message": "generated" if generated else "not generated",
        },
        "auto_execution": {
            "enabled": True,
            "attempted": False,
            "started": False,
            "status": "SKIPPED",
            "mission_id": "mission-auto-1" if generated else None,
        },
    }


def test_validated_bt_is_automatically_executed_and_run_id_is_returned(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(bt_engine_auto_execute=True))

    async def fake_execute(mission_id: str):
        assert mission_id == "mission-auto-1"
        return {
            "run_id": "run-auto-1",
            "preempted_previous": False,
            "poll_interval_s": 0.3,
        }

    monkeypatch.setattr(main, "_execute_saved_mission", fake_execute)
    result = asyncio.run(main._attach_auto_execution(_candidate()))

    assert result["bt_generation"]["succeeded"] is True
    assert result["auto_execution"] == {
        "enabled": True,
        "attempted": True,
        "started": True,
        "status": "STARTED",
        "mission_id": "mission-auto-1",
        "run_id": "run-auto-1",
        "engine_state": "running",
        "preempted_previous": False,
        "poll_interval_s": 0.3,
        "reason": None,
    }


def test_failed_bt_generation_is_never_sent_to_engine(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(bt_engine_auto_execute=True))
    called = False

    async def fake_execute(_mission_id: str):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(main, "_execute_saved_mission", fake_execute)
    result = asyncio.run(main._attach_auto_execution(_candidate(generated=False)))
    assert called is False
    assert result["auto_execution"]["status"] == "SKIPPED"
    assert result["auto_execution"]["attempted"] is False


def test_engine_rejection_is_reported_without_hiding_generated_xml(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(bt_engine_auto_execute=True))

    async def fake_execute(_mission_id: str):
        raise HTTPException(422, {"error": "invalid engine XML"})

    monkeypatch.setattr(main, "_execute_saved_mission", fake_execute)
    result = asyncio.run(main._attach_auto_execution(_candidate()))
    assert result["status"] == "SUCCESS"
    assert result["bt_generation"]["succeeded"] is True
    assert result["auto_execution"]["status"] == "FAILED"
    assert result["auto_execution"]["error"]["http_status"] == 422


def test_compare_mode_never_executes_both_candidates(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(bt_engine_auto_execute=True))
    result = asyncio.run(main._attach_auto_execution(
        _candidate(), allow=False, skip_reason="compare mode never executes multiple candidate trees"
    ))
    assert result["auto_execution"]["status"] == "SKIPPED"
    assert "compare mode" in result["auto_execution"]["reason"]


def test_chat_response_schema_exposes_generation_and_execution_contract():
    response = main.ChatResponse.model_validate({
        "session_id": "session-1",
        "pipeline_mode": "hybrid",
        "candidate": _candidate(),
        "conversation_grounding": {},
    })
    assert response.candidate is not None
    assert response.candidate.bt_generation.succeeded is True
    assert response.candidate.auto_execution.status == "SKIPPED"


def test_chat_request_defaults_to_hybrid_auto_execution_and_rejects_unknown_fields():
    request = main.ChatRequest.model_validate({"message": "find the cup"})
    assert request.pipeline_mode == "hybrid"
    assert request.options.auto_execute is True
    with pytest.raises(ValidationError):
        main.ChatRequest.model_validate({"message": "find the cup", "unexpected": True})
