import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class EvaluationSummary:
    total_cases: int
    completed_cases: int
    blocked_cases: int
    error_cases: int
    promoted_cases: int
    total_steps: int
    total_retries: int
    serialization_ok: bool
    elapsed_ms: float


def evaluate_cases(
    runner: Callable[[dict[str, Any]], dict[str, Any]],
    cases: Iterable[dict[str, Any]],
) -> EvaluationSummary:
    """Collect observable technical baselines; do not judge domain quality."""

    started = perf_counter()
    total = completed = blocked = errors = promoted = steps = retries = 0
    serialization_ok = True

    for case in cases:
        total += 1
        result = runner(case)
        try:
            encoded = json.dumps(result, ensure_ascii=False)
            json.loads(encoded)
        except (TypeError, ValueError):
            serialization_ok = False

        reason = result.get("termination_reason")
        if reason == "completed":
            completed += 1
        elif reason in {"approval_required", "approval_rejected"}:
            blocked += 1
        else:
            errors += 1

        if result.get("evolution_status") == "promoted":
            promoted += 1
        steps += len(result.get("trace", []))
        retries += sum(result.get("retry_counts", {}).values())

    return EvaluationSummary(
        total_cases=total,
        completed_cases=completed,
        blocked_cases=blocked,
        error_cases=errors,
        promoted_cases=promoted,
        total_steps=steps,
        total_retries=retries,
        serialization_ok=serialization_ok,
        elapsed_ms=round((perf_counter() - started) * 1000, 3),
    )
