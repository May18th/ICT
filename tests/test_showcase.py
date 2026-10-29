from decision_poc.showcase import run_evolution_showcase


def test_evolution_showcase_reports_publish_and_rejection_paths():
    result = run_evolution_showcase()

    assert result == {
        "initial_version": "v1",
        "promoted_version": "v2",
        "rejected_active_version": "v2",
        "history": ["proposed:v2", "promoted:v2", "proposed:v3", "rejected:v3"],
    }

