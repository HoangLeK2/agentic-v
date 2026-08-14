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


class ExecutionMode(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class EngineeringTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    task: str = Field(min_length=1)
    intent: EngineeringIntent = EngineeringIntent.IMPLEMENT
    apply_fixes: bool = True
    execution_mode: ExecutionMode = ExecutionMode.STANDARD
    max_fix_loops: int = Field(default=2, ge=0, le=6)
    acceptance_criteria: tuple[str, ...] = ()
    quality: QualityLevel = QualityLevel.AUTO

    @model_validator(mode="after")
    def validate_fix_mode(self) -> "EngineeringTaskInput":
        if "max_fix_loops" not in self.model_fields_set:
            if self.execution_mode == ExecutionMode.FAST:
                self.max_fix_loops = 1
            elif self.execution_mode == ExecutionMode.DEEP:
                self.max_fix_loops = 4
        return self


class ResearchTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    execution_mode: ExecutionMode = ExecutionMode.FAST
    quality: QualityLevel = QualityLevel.AUTO
    max_search_rounds: int = Field(default=1, ge=1, le=3)
    max_sources: int = Field(default=3, ge=1, le=8)

    @model_validator(mode="after")
    def tune_budget_for_mode(self) -> "ResearchTaskInput":
        if "max_search_rounds" not in self.model_fields_set:
            if self.execution_mode == ExecutionMode.DEEP:
                self.max_search_rounds = 3
            elif self.execution_mode == ExecutionMode.STANDARD:
                self.max_search_rounds = 2
            else:
                self.max_search_rounds = 1
        if "max_sources" not in self.model_fields_set:
            if self.execution_mode == ExecutionMode.DEEP:
                self.max_sources = 8
            elif self.execution_mode == ExecutionMode.STANDARD:
                self.max_sources = 5
            else:
                self.max_sources = 3
        if self.quality == QualityLevel.STRICT and self.execution_mode == ExecutionMode.FAST:
            self.execution_mode = ExecutionMode.STANDARD
        return self


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
