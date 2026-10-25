from typing import Any

from .contracts import DecisionRequest, ToolContext, ToolResult, ToolStats
from .evaluation import EvaluationSummary, evaluate_cases
from .graph import run_decision


def _request(request_id: str) -> DecisionRequest:
    return DecisionRequest(
        request_id=request_id,
        goal="Choose a safe next action",
        context={"signal": "generic-anomaly"},
        constraints=["preserve evidence"],
    )


def _model_output(request_id: str, *, high_risk: bool = False) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "summary": "A generic baseline decision",
        "options": [
            {
                "option_id": "inspect",
                "description": "Inspect available evidence",
                "expected_impact": "Improves decision confidence",
            }
        ],
        "selected_option_id": "inspect",
        "confidence": 0.8,
        "risk_level": "high" if high_risk else "low",
        "risks": ["Evidence may be incomplete"],
        "evidence": [
            {
                "source": "mock://baseline",
                "content": "Generic baseline evidence",
                "version": "v1",
            }
        ],
    }


def _evidence() -> ToolResult:
    return ToolResult(
        status="ok",
        data={
            "items": [
                {
                    "source": "mock://baseline",
                    "content": "Generic baseline evidence",
                    "version": "v1",
                }
            ]
        },
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="mock://baseline", version="v1"),
    )


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    request_id = case["request_id"]
    request = _request(request_id)
    mode = case["mode"]
    return run_decision(
        request,
        model_output=_model_output(request_id, high_risk=mode == "blocked"),
        evidence_results=[] if mode == "error" else [_evidence()],
        approval=None if mode == "blocked" else "approved",
    )


def run_technical_baseline() -> EvaluationSummary:
    """Run fixed generic paths to measure technical behavior only."""

    cases = [
        {"name": "completed", "mode": "completed", "request_id": "baseline-completed"},
        {"name": "blocked", "mode": "blocked", "request_id": "baseline-blocked"},
        {"name": "error", "mode": "error", "request_id": "baseline-error"},
    ]
    return evaluate_cases(_run_case, cases)
