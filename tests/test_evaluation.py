from decision_poc.evaluation import evaluate_cases


def test_evaluation_summary_records_generic_technical_baselines():
    cases = [
        {
            "name": "completed",
            "result": {
                "termination_reason": "completed",
                "trace": ["a", "b"],
                "retry_counts": {"evidence": 2},
            },
        },
        {
            "name": "blocked",
            "result": {
                "termination_reason": "approval_required",
                "trace": ["a", "b", "c"],
                "retry_counts": {"evidence": 1},
            },
        },
    ]

    summary = evaluate_cases(lambda case: case["result"], cases)

    assert summary.completed_cases == 1
    assert summary.blocked_cases == 1
    assert summary.error_cases == 0
    assert summary.total_steps == 5
    assert summary.total_retries == 3
    assert summary.serialization_ok is True
    assert summary.elapsed_ms >= 0


def test_evaluation_does_not_claim_promotion_when_case_fails():
    cases = [
        {
            "name": "failed",
            "result": {
                "termination_reason": "evidence_failed",
                "trace": ["evidence_failed"],
                "retry_counts": {"evidence": 2},
                "evolution_status": "rejected",
            },
        }
    ]

    summary = evaluate_cases(lambda case: case["result"], cases)

    assert summary.error_cases == 1
    assert summary.promoted_cases == 0
