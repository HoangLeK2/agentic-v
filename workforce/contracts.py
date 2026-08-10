"""Structured contracts shared by workforce teams and workflows."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    FAILED = "failed"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EngineeringIntent(StrEnum):
    IMPLEMENT = "implement"
    AUDIT = "audit"


class QualityLevel(StrEnum):
    AUTO = "auto"
    BASIC = "basic"
    STRICT = "strict"


class EngineeringTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    task: str = Field(min_length=1)
    intent: EngineeringIntent = EngineeringIntent.IMPLEMENT
    apply_fixes: bool = True
    acceptance_criteria: tuple[str, ...] = ()
    quality: QualityLevel = QualityLevel.AUTO

    @model_validator(mode="after")
    def validate_fix_mode(self) -> "EngineeringTaskInput":
        return self


class ResearchTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    quality: QualityLevel = QualityLevel.AUTO
    max_search_rounds: int = Field(default=3, ge=1, le=3)


class Artifact(BaseModel):
    kind: str
    name: str
    content: str | None = None


class WorkforceOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OutcomeStatus
    summary: str
    delegated_to: tuple[str, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    checks: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    approvals_required: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()
    unavailable_capabilities: tuple[str, ...] = ()
    learning_candidate_ids: tuple[str, ...] = ()
