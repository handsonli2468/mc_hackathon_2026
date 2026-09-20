from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", "/mlsteam/workspace"))
RUNTIME_ROOT = Path(os.getenv("RUNTIME_ROOT", str(PROJECT_ROOT / "agent-runtime")))
CONFIG_ROOT = RUNTIME_ROOT / "config"
STATE_ROOT = RUNTIME_ROOT / "state"
LOG_ROOT = RUNTIME_ROOT / "logs"
FRONTEND_DIR = PROJECT_ROOT / "frontend"


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    runtime_root: Path = RUNTIME_ROOT
    config_root: Path = CONFIG_ROOT
    state_root: Path = STATE_ROOT
    frontend_dir: Path = FRONTEND_DIR
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    planner_base_url: str = os.getenv("PLANNER_BASE_URL", "http://127.0.0.1:8101/v1")
    planner_model: str = os.getenv("PLANNER_SERVED_NAME", "qwen3.6-planner")
    compiler_base_url: str = os.getenv("BT_COMPILER_BASE_URL", "http://127.0.0.1:8102/v1")
    compiler_model: str = os.getenv("BT_COMPILER_SERVED_NAME", "btgenbot-2")
    compiler_max_output_tokens: int = int(os.getenv("BT_COMPILER_MAX_OUTPUT_TOKENS", "500"))
    compiler_temperature: float = float(os.getenv("BT_COMPILER_TEMPERATURE", "0.7"))
    compiler_repair_temperature: float = float(os.getenv("BT_COMPILER_REPAIR_TEMPERATURE", "0.2"))
    compiler_phase_retries: int = int(os.getenv("BT_COMPILER_PHASE_RETRIES", "2"))
    compiler_deterministic_fallback: bool = os.getenv("BT_COMPILER_DETERMINISTIC_FALLBACK", "1") == "1"
    enable_btgenbot: bool = os.getenv("ENABLE_BTGENBOT", "1") == "1"
    enable_plan_critic: bool = os.getenv("ENABLE_PLAN_CRITIC", "1") == "1"
    plan_critic_mode: str = os.getenv("PLAN_CRITIC_MODE", "conditional").strip().lower()
    planner_force_thinking: bool = os.getenv("PLANNER_FORCE_THINKING", "1") == "1"
    planner_thinking_budget_fallback: bool = os.getenv("PLANNER_THINKING_BUDGET_FALLBACK", "1") == "1"
    planner_requirements_thinking_budget: int = int(os.getenv("PLANNER_REQUIREMENTS_THINKING_BUDGET", "640"))
    planner_task_thinking_budget: int = int(os.getenv("PLANNER_TASK_THINKING_BUDGET", "3072"))
    compact_planner_enabled: bool = os.getenv("COMPACT_PLANNER_ENABLED", "1") == "1"
    compact_planner_thinking_budget: int = int(os.getenv("COMPACT_PLANNER_THINKING_BUDGET", "1280"))
    compact_planner_retry_thinking_budget: int = int(os.getenv("COMPACT_PLANNER_RETRY_THINKING_BUDGET", "3072"))
    compact_planner_max_capabilities: int = int(os.getenv("COMPACT_PLANNER_MAX_CAPABILITIES", "8"))
    planner_task_thinking_budget_medium: int = int(os.getenv("PLANNER_TASK_THINKING_BUDGET_MEDIUM", "6144"))
    planner_task_retry_thinking_budget: int = int(os.getenv("PLANNER_TASK_RETRY_THINKING_BUDGET", "4096"))
    planner_task_thinking_budget_complex: int = int(os.getenv("PLANNER_TASK_THINKING_BUDGET_COMPLEX", "8192"))
    planner_critic_thinking_budget: int = int(os.getenv("PLANNER_CRITIC_THINKING_BUDGET", "3072"))
    planner_repair_thinking_budget: int = int(os.getenv("PLANNER_REPAIR_THINKING_BUDGET", "4096"))
    planner_direct_thinking_budget: int = int(os.getenv("PLANNER_DIRECT_THINKING_BUDGET", "4096"))
    plan_auto_harden_search_recovery: bool = os.getenv("PLAN_AUTO_HARDEN_SEARCH_RECOVERY", "1") == "1"
    auto_search_recovery_degrees: float = float(os.getenv("AUTO_SEARCH_RECOVERY_DEGREES", "45"))
    structured_output_fallback: bool = os.getenv("STRUCTURED_OUTPUT_FALLBACK", "1") == "1"
    timing_trace_persist: bool = os.getenv("TIMING_TRACE_PERSIST", "1") == "1"
    request_timeout: float = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "600"))
    max_ir_repairs: int = int(os.getenv("MAX_IR_REPAIR_ATTEMPTS", "2"))
    max_bt_repairs: int = int(os.getenv("MAX_BT_REPAIR_ATTEMPTS", "2"))
    max_policy_attempts: int = int(os.getenv("MAX_POLICY_ATTEMPTS", "10"))
    cors_origins: str = os.getenv("CORS_ORIGINS", "*")
    bt_engine_enabled: bool = os.getenv("BT_ENGINE_ENABLED", "1") == "1"
    bt_engine_auto_execute: bool = os.getenv("BT_ENGINE_AUTO_EXECUTE", "1") == "1"
    bt_engine_url: str = os.getenv("BT_ENGINE_URL", "https://mcpc.taile84e23.ts.net")
    bt_engine_token: str = os.getenv("BT_ENGINE_TOKEN", "")
    bt_engine_request_timeout: float = float(os.getenv("BT_ENGINE_REQUEST_TIMEOUT_SECONDS", "5"))
    bt_engine_health_timeout: float = float(os.getenv("BT_ENGINE_HEALTH_TIMEOUT_SECONDS", "1.5"))
    bt_engine_poll_interval: float = float(os.getenv("BT_ENGINE_POLL_INTERVAL_SECONDS", "0.3"))
    bt_engine_max_poll_seconds: float = float(os.getenv("BT_ENGINE_MAX_POLL_SECONDS", "900"))
    # v6.1: startup synchronization is automatic whenever BT_ENGINE_ENABLED=1.
    # The old BT_ENGINE_SYNC_NODES_ON_START switch is retained only for backwards
    # config readability; use BT_ENGINE_SKIP_STARTUP_NODE_SYNC=1 as an emergency
    # offline-development escape hatch.
    bt_engine_sync_nodes_on_start: bool = True
    bt_engine_skip_startup_node_sync: bool = os.getenv("BT_ENGINE_SKIP_STARTUP_NODE_SYNC", "0") == "1"
    bt_engine_sync_min_custom_nodes: int = int(os.getenv("BT_ENGINE_SYNC_MIN_CUSTOM_NODES", "1"))
    bt_engine_semantic_complete_for_health: bool = os.getenv("BT_ENGINE_SEMANTIC_COMPLETE_FOR_HEALTH", "1") == "1"
    bt_engine_required_for_health: bool = os.getenv("BT_ENGINE_REQUIRED_FOR_HEALTH", "0") == "1"
    rag_enabled: bool = os.getenv("RAG_ENABLED", "1") == "1"
    rag_required: bool = os.getenv("RAG_REQUIRED", "1") == "1"
    rag_skill_max_docs: int = int(os.getenv("RAG_SKILL_MAX_DOCS", "8"))
    rag_pattern_enabled: bool = os.getenv("RAG_PATTERN_ENABLED", "1") == "1"
    rag_pattern_max_docs: int = int(os.getenv("RAG_PATTERN_MAX_DOCS", "2"))
    rag_scene_enabled: bool = os.getenv("RAG_SCENE_ENABLED", "1") == "1"
    rag_scene_max_docs: int = int(os.getenv("RAG_SCENE_MAX_DOCS", "2"))
    rag_scene_overfetch_factor: int = int(os.getenv("RAG_SCENE_OVERFETCH_FACTOR", "4"))
    rag_location_enabled: bool = os.getenv("RAG_LOCATION_ENABLED", "1") == "1"
    rag_location_max_docs: int = int(os.getenv("RAG_LOCATION_MAX_DOCS", "4"))
    rag_search_prior_min_confidence: float = float(os.getenv("RAG_SEARCH_PRIOR_MIN_CONFIDENCE", "0.70"))
    rag_experience_enabled: bool = os.getenv("RAG_EXPERIENCE_ENABLED", "1") == "1"
    rag_experience_max_docs: int = int(os.getenv("RAG_EXPERIENCE_MAX_DOCS", "2"))
    rag_prompt_max_chars: int = int(os.getenv("RAG_PROMPT_MAX_CHARS", "10000"))
    rag_knowledge_root: Path = Path(os.getenv("RAG_KNOWLEDGE_ROOT", str(RUNTIME_ROOT / "rag-knowledge")))
    # v6.2 deterministic scene/location grounding. A stored pose is executable only
    # when it is calibrated and can be bound to an active location-navigation skill.
    location_grounding_enabled: bool = os.getenv("LOCATION_GROUNDING_ENABLED", "1") == "1"
    location_knowledge_mode: str = os.getenv("LOCATION_KNOWLEDGE_MODE", "integration").strip().lower()
    location_require_calibrated: bool = os.getenv("LOCATION_REQUIRE_CALIBRATED", "1") == "1"
    location_require_active_map_version: bool = os.getenv("LOCATION_REQUIRE_ACTIVE_MAP_VERSION", "0") == "1"
    robot_map_version: str = os.getenv("ROBOT_MAP_VERSION", "").strip()
    location_rewrite_visual_preamble: bool = os.getenv("LOCATION_REWRITE_VISUAL_PREAMBLE", "1") == "1"
    version: str = "6.4.0"


settings = Settings()
