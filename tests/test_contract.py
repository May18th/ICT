import pytest
from pydantic import ValidationError

from decision_poc.contracts import (
    DecisionOption,
    DecisionOutput,
    DecisionPolicy,
    DecisionRequest,
    EvidenceItem,
    RiskLevel,
    ToolContext,
    ToolError,
    ToolResult,
    ToolStats,
)


def test_decision_request_rejects_blank_goal_and_unknown_domain_fields():
    with pytest.raises(ValidationError):
        DecisionRequest(request_id="req-1", goal="  ", context={}, constraints=[])

    with pytest.raises(ValidationError):
        DecisionRequest(
            request_id="req-1",
            goal="Choose an option",
            context={},
            constraints=[],
            incident_id="domain-specific",
        )


def test_decision_output_requires_selected_option_and_evidence():
    option = DecisionOption(
        option_id="opt-1",
        description="Option one",
        expected_impact="Low impact",
    )
    evidence = EvidenceItem(source="mock://source", content="Observed fact", version="v1")
    output = DecisionOutput(
        request_id="req-1",
        summary="A generic decision",
        options=[option],
        selected_option_id="opt-1",
        confidence=0.75,
        risk_level=RiskLevel.MEDIUM,
        risks=["Outcome is uncertain"],
        evidence=[evidence],
    )

    assert output.selected_option_id == "opt-1"
    assert output.evidence[0].content == "Observed fact"

    with pytest.raises(ValidationError, match="selected_option_id"):
        DecisionOutput(
            request_id="req-1",
            summary="A generic decision",
            options=[option],
            selected_option_id="missing",
            confidence=0.75,
            risk_level="low",
            risks=["Outcome is uncertain"],
            evidence=[evidence],
        )

    with pytest.raises(ValidationError):
        DecisionOutput(
            request_id="req-1",
            summary="A generic decision",
            options=[option],
            selected_option_id="opt-1",
            confidence=0.75,
            risk_level="low",
            risks=["Outcome is uncertain"],
            evidence=[],
        )


def test_tool_result_status_and_error_fields_must_agree():
    error = ToolError(code="FAIL", message="failed", retryable=False)
    common = {
        "data": {},
        "stats": ToolStats(attempt=1, latency_ms=0),
        "context": ToolContext(source="mock://tool", version="v1"),
    }

    with pytest.raises(ValidationError, match="error status requires error"):
        ToolResult(status="error", error=None, **common)

    with pytest.raises(ValidationError, match="non-error status forbids error"):
        ToolResult(status="ok", error=error, **common)


def test_decision_policy_defaults_are_domain_neutral_and_bounded():
    policy = DecisionPolicy()

    assert policy.max_tool_attempts == 2
    assert policy.approval_risk_levels == [RiskLevel.HIGH]
    assert policy.initial_parameters == {}

    with pytest.raises(ValidationError):
        DecisionPolicy(max_tool_attempts=0)
