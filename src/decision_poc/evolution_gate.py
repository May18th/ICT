"""Deterministic evaluation gate for policy candidates."""

from dataclasses import dataclass
from typing import Callable

from .contracts import EvaluationReport, FeedbackRecord, PolicyVersion
from .evolution import VersionRegistry


@dataclass(frozen=True)
class GateResult:
    candidate: PolicyVersion
    report: EvaluationReport
    active: PolicyVersion
    action: str


def evaluate_and_apply(
    registry: VersionRegistry,
    feedback: FeedbackRecord,
    evaluate: Callable[[PolicyVersion], EvaluationReport],
) -> GateResult:
    candidate = registry.propose(feedback)
    report = evaluate(candidate)
    active = registry.promote(candidate, report)
    return GateResult(
        candidate=candidate,
        report=report,
        active=active,
        action="promoted" if report.passed else "rejected",
    )

