from decision_poc.contracts import EvaluationReport, FeedbackRecord, FeedbackOutcome
from decision_poc.evolution import VersionRegistry
from decision_poc.evolution_gate import evaluate_and_apply


def test_evaluate_and_apply_promotes_only_passing_candidate():
    registry = VersionRegistry(initial_parameters={"threshold": 1})
    feedback = FeedbackRecord(
        request_id="req-1",
        outcome=FeedbackOutcome.POSITIVE,
        note="improved",
        proposed_updates={"threshold": 2},
    )

    result = evaluate_and_apply(
        registry,
        feedback,
        lambda candidate: EvaluationReport(
            passed=candidate.parameters["threshold"] == 2,
            metrics={"accuracy": 1.0},
            notes="offline gate",
        ),
    )

    assert result.active.version_id == "v2"
    assert result.report.passed is True
    assert result.action == "promoted"


def test_evaluate_and_apply_keeps_active_version_when_gate_fails():
    registry = VersionRegistry(initial_parameters={"threshold": 1})
    feedback = FeedbackRecord(
        request_id="req-1",
        outcome=FeedbackOutcome.NEGATIVE,
        note="regressed",
        proposed_updates={"threshold": 9},
    )

    result = evaluate_and_apply(
        registry,
        feedback,
        lambda candidate: EvaluationReport(
            passed=False,
            metrics={"accuracy": 0.1},
            notes="regression",
        ),
    )

    assert result.active.version_id == "v1"
    assert result.action == "rejected"

