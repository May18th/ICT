import asyncio
import json

import pytest

from decision_poc.contracts import (
    DecisionOutput,
    DecisionRequest,
    FeedbackOutcome,
    FeedbackRecord,
    RiskLevel,
    ToolContext,
    ToolError,
    ToolResult,
    ToolStats,
)
from decision_poc.demo import build_demo_runnable
from decision_poc.graph import run_decision_async


def _request(request_id="req-001"):
    return DecisionRequest(
        request_id=request_id,
        goal="Choose a safe next action",
        context={"signal": "anomaly"},
        constraints=["preserve evidence"],
    )


def _model_output(request_id="req-001", risk_level="low"):
    return {
        "request_id": request_id,
        "summary": "A safe generic decision",
        "options": [
            {
                "option_id": "opt-1",
                "description": "Inspect evidence",
                "expected_impact": "Improves confidence",
            }
        ],
        "selected_option_id": "opt-1",
        "confidence": 0.82,
        "risk_level": risk_level,
        "risks": ["The signal may be incomplete"],
        "evidence": [
            {"source": "mock://evidence", "content": "Observed anomaly", "version": "v1"}
        ],
    }


def _evidence(status="ok"):
    return ToolResult(
        status=status,
        data={
            "items": [
                {"source": "mock://evidence", "content": "Observed anomaly", "version": "v1"}
            ]
        },
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="mock://evidence", version="v1"),
    )


def _failed_evidence(retryable=True):
    return ToolResult(
        status="error",
        data={"items": []},
        error=ToolError(code="TEMP", message="temporary", retryable=retryable),
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="mock://evidence", version="v1"),
    )


def _feedback():
    return FeedbackRecord(
        request_id="req-001",
        outcome=FeedbackOutcome.POSITIVE,
        note="The action was useful",
        proposed_updates={"confidence_bias": 0.1},
    )


def test_approved_request_reaches_simulated_execution_and_feedback():
    result = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(risk_level="high"),
            evidence_results=[_evidence()],
            approval="approved",
            feedback=_feedback(),
            evaluation_report={"passed": True, "metrics": {"cases": 1}, "notes": "ok"},
        )
    )

    assert isinstance(DecisionOutput.model_validate(result["decision"]), DecisionOutput)
    assert result["execution"]["status"] == "ok"
    assert result["feedback"]["outcome"] == "positive"
    assert result["termination_reason"] == "completed"
    assert result["trace"] == [
        "evidence_collected",
        "decision_generated",
        "decision_validated",
        "approval_accepted",
        "execution_completed",
        "feedback_recorded",
        "candidate_promoted",
    ]


def test_feedback_creates_candidate_but_does_not_promote_without_evaluation():
    result = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(),
            evidence_results=[_evidence()],
            approval="approved",
            feedback=_feedback(),
        )
    )

    assert result["candidate"]["version_id"] == "v2"
    assert result["active_version"]["version_id"] == "v1"
    assert result["termination_reason"] == "completed"
    assert "candidate_awaiting_evaluation" in result["trace"]


def test_rejected_approval_blocks_execution():
    result = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(),
            evidence_results=[_evidence()],
            approval="rejected",
        )
    )

    assert result["execution"] is None
    assert result["termination_reason"] == "approval_rejected"
    assert "approval_rejected" in result["trace"]


def test_missing_approval_blocks_high_risk_execution():
    result = asyncio.run(
        run_decision_async(
            _request("req-high"),
            model_output=_model_output("req-high", risk_level="high"),
            evidence_results=[_evidence()],
            approval=None,
        )
    )

    assert result["execution"] is None
    assert result["termination_reason"] == "approval_required"


def test_unknown_approval_value_is_rejected():
    with pytest.raises(ValueError, match="approval"):
        asyncio.run(
            run_decision_async(
                _request(),
                model_output=_model_output(),
                evidence_results=[_evidence()],
                approval="maybe",
            )
        )


def test_invalid_decision_stops_before_approval():
    with pytest.raises(ValueError, match="request_id mismatch"):
        asyncio.run(
            run_decision_async(
                _request(),
                model_output=_model_output("wrong-request"),
                evidence_results=[_evidence()],
                approval="approved",
            )
        )


def test_graph_state_is_json_serializable_after_success_and_tool_failure():
    success = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(),
            evidence_results=[_evidence()],
            approval="approved",
        )
    )
    failed_tool = _failed_evidence(retryable=False)
    failure = asyncio.run(
        run_decision_async(
            _request("req-failure"),
            model_output=_model_output("req-failure"),
            evidence_results=[failed_tool],
            approval="approved",
        )
    )

    assert json.loads(json.dumps(success))["execution"]["status"] == "ok"
    assert json.loads(json.dumps(failure))["termination_reason"] == "evidence_failed"
    assert failure["evidence_result"]["error"]["code"] == "TEMP"
    assert failure["evidence_result"]["context"]["source"] == "mock://evidence"
    assert failure["evidence_result"]["stats"]["attempt"] == 1


def test_transient_evidence_failure_retries_and_records_attempt_count():
    result = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(),
            evidence_results=[_failed_evidence(), _evidence()],
            approval=None,
        )
    )

    assert result["retry_counts"]["evidence"] == 2
    assert result["termination_reason"] == "completed"


def test_decision_cannot_cite_evidence_not_returned_by_tool():
    mismatched = _evidence()
    mismatched.data["items"][0]["content"] = "Different observation"

    with pytest.raises(ValueError, match="evidence not found"):
        asyncio.run(
            run_decision_async(
                _request(),
                model_output=_model_output(),
                evidence_results=[mismatched],
                approval="approved",
            )
        )


def test_decision_evidence_must_match_source_and_version():
    mismatched = _evidence()
    mismatched.data["items"][0]["source"] = "mock://different-source"

    with pytest.raises(ValueError, match="evidence not found"):
        asyncio.run(
            run_decision_async(
                _request(),
                model_output=_model_output(),
                evidence_results=[mismatched],
                approval="approved",
            )
        )


def test_empty_scripted_evidence_does_not_fabricate_context_evidence():
    result = asyncio.run(
        run_decision_async(
            _request(),
            model_output=_model_output(),
            evidence_results=[],
            approval="approved",
        )
    )

    assert result["termination_reason"] == "evidence_failed"
    assert "decision" not in result


def test_langchain_adapter_calls_graph_and_returns_json_safe_output():
    runnable = build_demo_runnable(
        model_output=_model_output("req-demo"),
        evidence_results=[_evidence()],
        approval="approved",
    )
    result = runnable.invoke(
        {
            "request_id": "req-demo",
            "goal": "Choose a safe next action",
            "context": {"signal": "anomaly"},
            "constraints": ["preserve evidence"],
        }
    )

    assert result["decision"]["request_id"] == "req-demo"
    assert result["termination_reason"] == "completed"
    json.dumps(result, ensure_ascii=False)


def test_langchain_boundary_accepts_generic_request_async():
    runnable = build_demo_runnable(
        model_output=_model_output("req-demo-async"),
        evidence_results=[_evidence()],
        approval="approved",
    )
    result = asyncio.run(
        runnable.ainvoke(
            {
                "request_id": "req-demo-async",
                "goal": "Choose a safe next action",
                "context": {"signal": "anomaly"},
                "constraints": ["preserve evidence"],
            }
        )
    )

    assert result["decision"]["request_id"] == "req-demo-async"
    assert result["termination_reason"] == "completed"
