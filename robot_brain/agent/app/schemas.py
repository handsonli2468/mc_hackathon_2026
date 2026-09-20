from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class RequirementHint(str, Enum):
    READY = "READY"
    NEED_MORE_INFO = "NEED_MORE_INFO"
    UNSAFE = "UNSAFE"
    INVALID_REQUEST = "INVALID_REQUEST"


class RequestKind(str, Enum):
    CHAT = "CHAT"
    MISSION = "MISSION"


class NavigationSpeed(str, Enum):
    SLOW = "slow"
    NORMAL = "normal"
    FAST = "fast"


class ExperienceParameterFeedback(BaseModel):
    """User-recommended runtime parameters for similar future missions."""

    set_gripper_position: int | None = Field(default=None, ge=0, le=100)
    navigate_to_detected_object_speed: NavigationSpeed | None = None
    navigate_to_point_speed: NavigationSpeed | None = None


class MissionFeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=4000)
    parameters: ExperienceParameterFeedback = Field(default_factory=ExperienceParameterFeedback)


class SessionResetRequest(BaseModel):
    previous_session_id: str | None = Field(default=None, max_length=200)


class RequiredUserInformation(BaseModel):
    field: str
    question: str


class RetrievalSketch(BaseModel):
    """Compact semantic expansion used only to drive RAG retrieval.

    It names capabilities/concepts, never authoritative robot skill IDs. Detailed
    skill contracts are retrieved after this first LLM call.
    """
    semantic_concepts: list[str] = Field(default_factory=list)
    target_descriptions: list[str] = Field(default_factory=list)
    skill_queries: list[str] = Field(default_factory=list)
    pattern_queries: list[str] = Field(default_factory=list)
    scene_queries: list[str] = Field(default_factory=list)
    location_queries: list[str] = Field(default_factory=list)
    experience_queries: list[str] = Field(default_factory=list)


class MissionOperation(str, Enum):
    GENERAL = "GENERAL"
    FIND_OBJECT = "FIND_OBJECT"
    APPROACH_TARGET = "APPROACH_TARGET"
    ACQUIRE_OBJECT = "ACQUIRE_OBJECT"
    RELOCATE_OBJECT = "RELOCATE_OBJECT"
    DELIVER_OBJECT = "DELIVER_OBJECT"
    FETCH_OBJECT = "FETCH_OBJECT"
    NAVIGATE = "NAVIGATE"
    INSPECT = "INSPECT"


class TaskSemantics(BaseModel):
    """Environment-agnostic semantic frame for deterministic capability completion.

    The fields name user-level roles only. Concrete places, coordinates, robot node
    names, and scene facts must come from RAG/world state rather than this schema.
    """

    operation: MissionOperation = MissionOperation.GENERAL
    object_targets: list[str] = Field(default_factory=list)
    destination_targets: list[str] = Field(default_factory=list)
    recipient_targets: list[str] = Field(default_factory=list)
    source_hints: list[str] = Field(default_factory=list)
    destination_relation: str = ""


class MissionRequirements(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    request_kind: RequestKind
    status_hint: RequirementHint = RequirementHint.READY
    message: str = ""
    success_conditions: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    required_user_information: list[RequiredUserInformation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    task_semantics: TaskSemantics = Field(default_factory=TaskSemantics)
    retrieval_sketch: RetrievalSketch = Field(default_factory=RetrievalSketch)


class GroundingMode(str, Enum):
    VISUAL = "VISUAL"
    PROVIDED = "PROVIDED"
    STATIC_KNOWN = "STATIC_KNOWN"


class CompactTarget(BaseModel):
    id: str
    role: str = "object"
    object_name: str
    grounding: GroundingMode = GroundingMode.VISUAL


class CompactPlanStep(BaseModel):
    id: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    objective: str = ""


class CompactSemanticPlan(BaseModel):
    """Short planner-owned plan used by the v5.5 fast path.

    The model chooses semantic targets and ordered robot actions only. Deterministic
    code expands this into the full TaskPlanIR using the active SkillManifest.
    """

    schema_version: Literal["1.0"] = "1.0"
    goal_description: str
    success_conditions: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    global_constraints: list[str] = Field(default_factory=list)
    targets: list[CompactTarget] = Field(default_factory=list)
    steps: list[CompactPlanStep] = Field(min_length=1, max_length=16)


class MissionStatus(str, Enum):
    EXECUTABLE = "EXECUTABLE"
    NEED_MORE_INFO = "NEED_MORE_INFO"
    UNSUPPORTED = "UNSUPPORTED"
    UNSAFE = "UNSAFE"
    INVALID_REQUEST = "INVALID_REQUEST"


class Goal(BaseModel):
    description: str
    success_conditions: list[str] = Field(min_length=1)


class ActionSpec(BaseModel):
    skill: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class VerificationSpec(BaseModel):
    condition: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class RecoveryStrategy(str, Enum):
    RETRY = "RETRY"
    REFRESH_INFORMATION = "REFRESH_INFORMATION"
    LOCAL_ADJUSTMENT = "LOCAL_ADJUSTMENT"
    REPLAN_LOCAL = "REPLAN_LOCAL"
    ESCALATE = "ESCALATE"


class RecoveryStep(BaseModel):
    strategy: RecoveryStrategy
    skill: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class FailureClassification(str, Enum):
    TRANSIENT = "TRANSIENT"
    STALE_INFORMATION = "STALE_INFORMATION"
    ENVIRONMENT_CHANGED = "ENVIRONMENT_CHANGED"
    CAPABILITY_LIMIT = "CAPABILITY_LIMIT"
    PERMANENT = "PERMANENT"
    SAFETY = "SAFETY"
    UNKNOWN = "UNKNOWN"


class FailureMode(BaseModel):
    failure: str
    classification: FailureClassification
    recovery: list[RecoveryStep] = Field(default_factory=list)
    max_attempts: int = Field(ge=1, le=10)
    timeout_sec: float | None = Field(default=None, gt=0, le=600)
    escalation: str | None = None
    on_exhaustion: str


class Phase(BaseModel):
    id: str
    objective: str
    desired_state: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    nominal_action: ActionSpec
    verification: list[VerificationSpec] = Field(default_factory=list)
    failure_modes: list[FailureMode] = Field(default_factory=list)
    stale_dependencies: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    resource_requirements: list[str] = Field(default_factory=list)
    cleanup: list[ActionSpec] = Field(default_factory=list)


class TerminationPolicy(BaseModel):
    success_conditions: list[str] = Field(min_length=1)
    failure_conditions: list[str] = Field(default_factory=list)
    max_mission_duration_sec: float | None = Field(default=None, gt=0, le=86400)
    on_unrecoverable_failure: str = "MISSION_FAILURE"


class TaskPlanDraft(BaseModel):
    """Planner-owned semantic content only.

    Mission status, missing capabilities and missing user information are deliberately
    absent. Those fields are owned by deterministic orchestration and are assembled
    into TaskPlanIR after capability gating.
    """

    schema_version: Literal["1.0"] = "1.0"
    goal: Goal
    assumptions: list[str] = Field(default_factory=list)
    global_constraints: list[str] = Field(default_factory=list)
    termination_policy: TerminationPolicy
    phases: list[Phase] = Field(min_length=1)


class TaskPlanIR(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    mission_id: str | None = None
    mission_status: MissionStatus
    goal: Goal
    required_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    required_user_information: list[RequiredUserInformation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    task_semantics: TaskSemantics = Field(default_factory=TaskSemantics)
    retrieval_sketch: RetrievalSketch = Field(default_factory=RetrievalSketch)
    global_constraints: list[str] = Field(default_factory=list)
    termination_policy: TerminationPolicy
    phases: list[Phase] = Field(default_factory=list)
