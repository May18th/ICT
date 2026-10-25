import pytest
from pydantic import ValidationError

from decision_poc.contracts import (
    EvaluationReport,
    FeedbackOutcome,
    FeedbackRecord,
)
from decision_poc.evolution import VersionRegistry


def _feedback(*, outcome=FeedbackOutcome.POSITIVE, updates=None):
    return FeedbackRecord(
        request_id="req-1",
        outcome=outcome,
        note="The result was useful",
        proposed_updates=updates or {"confidence_bias": 0.1},
    )


def test_feedback_creates_candidate_without_mutating_active_version():
    registry = VersionRegistry(initial_parameters={"confidence_bias": 0.0})
    active_before = registry.active

    candidate = registry.propose(_feedback())

    assert candidate.parent_version_id == active_before.version_id
    assert candidate.parameters["confidence_bias"] == 0.1
    assert registry.active == active_before
    assert candidate.version_id != active_before.version_id


def test_failed_evaluation_keeps_active_version_unchanged():
    registry = VersionRegistry(initial_parameters={"confidence_bias": 0.0})
    candidate = registry.propose(_feedback())
    report = EvaluationReport(
        passed=False,
        metrics={"completed_cases": 2, "failed_cases": 1},
        notes="Candidate regressed on a held-out case",
    )

    retained = registry.promote(candidate, report)

    assert retained.version_id == "v1"
    assert registry.active.version_id == "v1"
    assert registry.accepted_versions == (registry.active,)
    assert registry.history[-1].action == "rejected"


def test_passed_evaluation_promotes_candidate():
    registry = VersionRegistry(initial_parameters={"confidence_bias": 0.0})
    candidate = registry.propose(_feedback())
    report = EvaluationReport(
        passed=True,
        metrics={"completed_cases": 3, "failed_cases": 0},
        notes="Candidate passed the generic regression set",
    )

    promoted = registry.promote(candidate, report)

    assert promoted == candidate
    assert registry.active.version_id == candidate.version_id
    assert registry.accepted_versions[-1] == candidate

    with pytest.raises(KeyError, match="unknown candidate"):
        registry.promote(
            candidate,
            EvaluationReport(passed=True, metrics={"score": 1.0}, notes="duplicate"),
        )


def test_rollback_restores_known_previous_version_and_unknown_version_fails():
    registry = VersionRegistry(initial_parameters={"confidence_bias": 0.0})
    candidate = registry.propose(_feedback())
    registry.promote(
        candidate,
        EvaluationReport(passed=True, metrics={"score": 1.0}, notes="accepted"),
    )

    restored = registry.rollback("v1")

    assert restored.version_id == "v1"
    assert registry.active.version_id == "v1"
    assert registry.history[-1].action == "rollback"
    assert registry.history[-1].version_id == candidate.version_id

    with pytest.raises(KeyError, match="unknown version"):
        registry.rollback("v999")

    with pytest.raises(ValidationError):
        registry.history[-1].action = "promoted"
