from enum import StrEnum
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class DecisionRequest(StrictModel):
    request_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    context: dict[str, Any]
    constraints: list[str]

    @field_validator("request_id", "goal")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("constraints")
    @classmethod
    def reject_blank_constraints(cls, values: list[str]) -> list[str]:
        return [_nonblank(value) for value in values]


class EvidenceItem(StrictModel):
    source: str = Field(min_length=1)
    content: str = Field(min_length=1)
    version: str | None = None

    @field_validator("source", "content")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        return _nonblank(value)


class DecisionOption(StrictModel):
    option_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    expected_impact: str = Field(min_length=1)

    @field_validator("option_id", "description", "expected_impact")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        return _nonblank(value)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionPolicy(StrictModel):
    max_tool_attempts: int = Field(default=2, ge=1, le=10)
    approval_risk_levels: list[RiskLevel] = Field(default_factory=lambda: [RiskLevel.HIGH])
    initial_parameters: dict[str, Any] = Field(default_factory=dict)


class FeedbackOutcome(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class FeedbackRecord(StrictModel):
    request_id: str = Field(min_length=1)
    outcome: FeedbackOutcome
    note: str
    proposed_updates: dict[str, Any]

    @field_validator("request_id")
    @classmethod
    def reject_blank_request_id(cls, value: str) -> str:
        return _nonblank(value)


class PolicyVersion(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: str = Field(min_length=1)
    parent_version_id: str | None
    parameters: dict[str, Any]


class EvaluationReport(StrictModel):
    passed: bool
    metrics: dict[str, float | int | str]
    notes: str


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REQUIRED = "required"


class ApprovalDecision(StrictModel):
    status: ApprovalStatus
    reason: str

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        return _nonblank(value)


class VersionHistoryEntry(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["proposed", "promoted", "rejected", "rollback"]
    version_id: str
    target_version_id: str | None = None


class DecisionOutput(StrictModel):
    request_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    options: list[DecisionOption] = Field(min_length=1)
    selected_option_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    risks: list[str] = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(min_length=1)

    @field_validator("request_id", "summary", "selected_option_id")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("risks")
    @classmethod
    def reject_blank_risks(cls, values: list[str]) -> list[str]:
        return [_nonblank(value) for value in values]

    @model_validator(mode="after")
    def selected_option_must_exist(self) -> "DecisionOutput":
        option_ids = {option.option_id for option in self.options}
        if self.selected_option_id not in option_ids:
            raise ValueError("selected_option_id must reference an option")
        return self


class ToolError(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool

    @field_validator("code", "message")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        return _nonblank(value)


class ToolStats(StrictModel):
    attempt: int = Field(ge=1)
    latency_ms: int = Field(ge=0)


class ToolContext(StrictModel):
    source: str = Field(min_length=1)
    version: str | None = None

    @field_validator("source")
    @classmethod
    def reject_blank_source(cls, value: str) -> str:
        return _nonblank(value)


class ToolResult(StrictModel):
    status: str
    data: dict[str, Any]
    error: ToolError | None
    stats: ToolStats
    context: ToolContext

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        value = _nonblank(value)
        if value not in {"ok", "partial", "error"}:
            raise ValueError("status must be ok, partial, or error")
        return value

    @model_validator(mode="after")
    def status_matches_error(self) -> "ToolResult":
        if self.status == "error" and self.error is None:
            raise ValueError("error status requires error")
        if self.status != "error" and self.error is not None:
            raise ValueError("non-error status forbids error")
        return self
