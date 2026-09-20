from __future__ import annotations

import json
from typing import Any

import yaml

from .registry import (
    allowed_actions_text,
    bt_binding_example_for_prompt,
    bt_port_contract_for_prompt,
    load_builtin_registry,
    skill_registry_for_prompt,
)
from .schemas import CompactSemanticPlan, MissionRequirements, TaskPlanDraft
from .settings import settings
from .rag import capability_ontology_for_prompt, planner_context_for_prompt


def _prompt(name: str) -> str:
    path = settings.config_root / "prompts" / f"{name}.md"
    return path.read_text(encoding="utf-8")


def _history_text(history: list[dict[str, str]]) -> str:
    return json.dumps(history[-12:], ensure_ascii=False, indent=2)


def requirement_messages(message: str, history: list[dict[str, str]], world_state: dict[str, Any] | None) -> list[dict[str, str]]:
    user = f"""Conversation history:
{_history_text(history)}

Current user input:
{message}

World state (may be empty):
{json.dumps(world_state or {}, ensure_ascii=False, indent=2)}

Capability ontology (semantic capability names only; detailed robot skill contracts are intentionally hidden until RAG retrieval):
{capability_ontology_for_prompt()}

Your response is schema-constrained to MissionRequirements. For reference, the JSON Schema is:
{json.dumps(MissionRequirements.model_json_schema(), ensure_ascii=False, indent=2)}"""
    return [{"role": "system", "content": _prompt("requirement")}, {"role": "user", "content": user}]



def compact_planner_messages(message: str, requirements: dict[str, Any], history: list[dict[str, str]], world_state: dict[str, Any] | None, rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    user = f"""User mission:
{message}

Conversation history:
{_history_text(history)}

Validated mission requirements:
{json.dumps(requirements, ensure_ascii=False, indent=2)}

World state and deterministic conversation grounding:
{json.dumps(world_state or {}, ensure_ascii=False, indent=2)}

RAG-grounded planning context (closed skill set + relevant patterns/scene/experience):
{planner_context_for_prompt(rag_context)}

Your response is schema-constrained to CompactSemanticPlan. JSON Schema:
{json.dumps(CompactSemanticPlan.model_json_schema(), ensure_ascii=False, indent=2)}"""
    return [{"role": "system", "content": _prompt("compact_planner")}, {"role": "user", "content": user}]

def planner_messages(message: str, requirements: dict[str, Any], history: list[dict[str, str]], world_state: dict[str, Any] | None, rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    user = f"""User mission:
{message}

Conversation history:
{_history_text(history)}

Deterministically validated mission requirements:
{json.dumps(requirements, ensure_ascii=False, indent=2)}

World state:
{json.dumps(world_state or {}, ensure_ascii=False, indent=2)}

RAG-grounded planning context. Use ONLY the retrieved allowed_skill_ids and exact ports shown here:
{planner_context_for_prompt(rag_context)}

IMPORTANT RESPONSIBILITY BOUNDARY:
- Do NOT output mission_status.
- Do NOT output missing_capabilities.
- Do NOT output required_user_information.
Those fields are owned by deterministic orchestration and will be assembled after your draft.

Your response is schema-constrained to TaskPlanDraft. For reference, the JSON Schema is:
{json.dumps(TaskPlanDraft.model_json_schema(), ensure_ascii=False, indent=2)}"""
    return [{"role": "system", "content": _prompt("planner")}, {"role": "user", "content": user}]


def critic_messages(draft: dict[str, Any], quality_gate: dict[str, Any] | None = None, rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    user = f"""RAG-grounded closed skill context:
{planner_context_for_prompt(rag_context)}

Draft TaskPlanDraft:
{json.dumps(draft, ensure_ascii=False, indent=2)}

Deterministic robustness gate findings:
{json.dumps(quality_gate or {}, ensure_ascii=False, indent=2)}

Return a corrected TaskPlanDraft only. Resolve the gate findings when they are applicable. Do not add orchestration-owned mission status/capability fields."""
    return [{"role": "system", "content": _prompt("critic")}, {"role": "user", "content": user}]


def ir_repair_messages(draft: dict[str, Any] | None, errors: list[str], message: str, requirements: dict[str, Any], rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    body = {
        "user_mission": message,
        "requirements": requirements,
        "previous_task_plan_draft": draft,
        "semantic_validation_errors": errors,
        "rag_grounded_context": planner_context_for_prompt(rag_context),
        "task_plan_draft_json_schema": TaskPlanDraft.model_json_schema(),
    }
    return [
        {"role": "system", "content": _prompt("ir_repair") + "\n" + _prompt("planner")},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False, indent=2)},
    ]


def _bt_context() -> str:
    builtins = yaml.safe_dump(load_builtin_registry(), sort_keys=False, allow_unicode=True)
    return f"""Registered SkillManifest / leaf nodes:
{skill_registry_for_prompt()}

{bt_port_contract_for_prompt()}

{bt_binding_example_for_prompt()}

Allowed builtin BT nodes and constraints:
{builtins}"""


def direct_bt_messages(message: str, requirements: dict[str, Any], world_state: dict[str, Any] | None, rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    body = f"""User mission:
{message}

Validated requirements:
{json.dumps(requirements, ensure_ascii=False, indent=2)}

World state:
{json.dumps(world_state or {}, ensure_ascii=False, indent=2)}

RAG-grounded planning context:
{planner_context_for_prompt(rag_context)}

Builtin BT control-node contract:
{yaml.safe_dump(load_builtin_registry(), sort_keys=False, allow_unicode=True)}"""
    return [{"role": "system", "content": _prompt("direct_bt")}, {"role": "user", "content": body}]


def compiler_messages(task_text: str, actions_text: str | None = None) -> list[dict[str, str]]:
    """BTGenBot-2 phase prompt kept intentionally close to the released interface."""
    user = f"""Task:
{task_text}

Actions:
{actions_text or allowed_actions_text()}"""
    return [{"role": "system", "content": _prompt("compiler")}, {"role": "user", "content": user}]


def compiler_repair_messages(
    previous_xml: str,
    errors: list[str],
    task_text: str,
    actions_text: str | None = None,
) -> list[dict[str, str]]:
    user = f"""Task:
{task_text}

Actions:
{actions_text or allowed_actions_text()}

Previous XML:
{previous_xml}

Validation errors to fix:
{chr(10).join(f'- {e}' for e in errors)}

Return a corrected small complete XML tree only. Use every listed required leaf exactly once, use no other leaf nodes, and do not add RetryUntilSuccessful, Timeout, recovery branches, or SubTrees."""
    return [
        {"role": "system", "content": _prompt("compiler") + "\n\n" + _prompt("bt_repair")},
        {"role": "user", "content": user},
    ]


def bt_repair_messages(previous_xml: str, errors: list[str], task_text: str, rag_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """RAG-grounded repair for the direct Qwen -> XML baseline."""
    system = _prompt("bt_repair") + "\n" + _prompt("direct_bt")
    user = f"""Task specification:
{task_text}

Previous XML:
{previous_xml}

Deterministic validation errors:
{chr(10).join(f'- {e}' for e in errors)}

RAG-grounded closed skill context:
{planner_context_for_prompt(rag_context)}

Allowed builtin BT nodes and constraints:
{yaml.safe_dump(load_builtin_registry(), sort_keys=False, allow_unicode=True)}

Repair only what is necessary and return one complete XML document."""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
