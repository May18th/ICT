from decision_poc.baseline import run_technical_baseline


def test_technical_baseline_runs_real_graph_paths_without_domain_thresholds():
    summary = run_technical_baseline()

    assert summary.total_cases == 3
    assert summary.completed_cases == 1
    assert summary.blocked_cases == 1
    assert summary.error_cases == 1
    assert summary.serialization_ok is True
    assert summary.total_steps > 0
    assert summary.total_retries == 3
    assert summary.elapsed_ms >= 0
