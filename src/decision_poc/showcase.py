"""Small deterministic showcase for the policy evolution flow."""

from .contracts import EvaluationReport, FeedbackOutcome, FeedbackRecord
from .evolution import VersionRegistry
from .evolution_gate import evaluate_and_apply


def run_evolution_showcase() -> dict[str, object]:
    registry = VersionRegistry(initial_parameters={"threshold": 1})
    initial = registry.active.version_id
    positive = FeedbackRecord(
        request_id="showcase-1",
        outcome=FeedbackOutcome.POSITIVE,
        note="candidate improves the baseline",
        proposed_updates={"threshold": 2},
    )
    evaluate_and_apply(
        registry,
        positive,
        lambda _: EvaluationReport(passed=True, metrics={"score": 1.0}, notes="pass"),
    )
    negative = FeedbackRecord(
        request_id="showcase-2",
        outcome=FeedbackOutcome.NEGATIVE,
        note="candidate regresses",
        proposed_updates={"threshold": 9},
    )
    rejected = evaluate_and_apply(
        registry,
        negative,
        lambda _: EvaluationReport(passed=False, metrics={"score": 0.1}, notes="fail"),
    )
    return {
        "initial_version": initial,
        "promoted_version": "v2",
        "rejected_active_version": rejected.active.version_id,
        "history": [
            f"{entry.action}:{entry.version_id}"
            for entry in registry.history
        ],
    }

