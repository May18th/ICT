"""P9 进化层测试：偏差下降、门禁拒绝、回滚、确定性、反馈指纹。"""

from __future__ import annotations

import pytest

from decision_poc.allocation import (
    AllocationPolicy,
    cases_digest,
    default_policy,
    evaluate_policy,
    load_cases,
)
from decision_poc.experience import DecisionMemory, ReviewDecision, load_reviews
from decision_poc.policy_evolution import (
    EvolutionReport,
    candidate_patches,
    entries_from_report,
    evolve,
    probe_patches,
    render_markdown,
    render_showcase_markdown,
    run_showcase,
    worst_dimension,
)


@pytest.fixture(scope="module")
def report() -> EvolutionReport:
    return evolve()


@pytest.fixture(scope="module")
def cases():
    return load_cases()


def test_evolution_reduces_deviation_on_train(report: EvolutionReport) -> None:
    assert report.initial_train.deviation > 0.2
    assert report.final_train.deviation == 0.0
    assert report.train_deviation_drop() == pytest.approx(0.226428, abs=1e-6)


def test_evolution_generalizes_to_holdout(report: EvolutionReport) -> None:
    assert report.initial_holdout.deviation > 0.2
    assert report.final_holdout.deviation < report.initial_holdout.deviation
    assert report.final_holdout.deviation == 0.0


def test_improvement_is_visible_in_absolute_units(report: EvolutionReport) -> None:
    # 关键片区未满足从 15 台降到 ground truth 下限 6 台（资源本身不够的部分）
    assert report.initial_train.critical_unmet_units == 15
    assert report.final_train.critical_unmet_units == report.final_train.expected_critical_unmet_units == 6
    assert report.initial_train.missed_transfers == 4
    assert report.final_train.missed_transfers == 0
    assert report.initial_train.allocation_l1_units == 13
    assert report.final_train.allocation_l1_units == 0
    assert report.initial_train.order_hits == 7
    assert report.final_train.order_hits == 8


def test_promotion_then_rejection_is_recorded(report: EvolutionReport) -> None:
    actions = [item.action for item in report.rounds]
    assert actions == ["promoted", "promoted", "rejected"]
    assert report.promoted_rounds == 2
    assert report.rejected_rounds == 1
    # 第一轮学到"已过封路阈值优先"，第二轮补"危险源也算关键"
    assert report.rounds[0].params_delta == {"weight_closure": 1.0, "transfer_penalty": 0.1}
    assert report.rounds[1].params_delta == {"weight_hazard": 0.25}


def test_rejected_probe_does_not_change_active_version(report: EvolutionReport) -> None:
    assert report.final_policy.policy_version == "v3"
    assert report.final_policy.weight_closure == 1.0
    assert report.final_policy.weight_hazard == 0.25
    assert report.final_policy.transfer_penalty == 0.1
    # 被拒的探针参数没有写进最终策略
    assert report.final_policy.transfer_penalty != 0.05
    assert any("rejected:v4" in item for item in report.history)


def test_rollback_restores_initial_deviation(report: EvolutionReport) -> None:
    rollback = report.rolled_back
    assert rollback is not None
    assert rollback.matches_initial is True
    assert rollback.deviation_after_rollback == pytest.approx(report.initial_train.deviation)
    assert rollback.restored_version == "v3"
    assert rollback.restored_deviation == pytest.approx(report.final_train.deviation)


def test_real_event_gets_a_different_plan_after_evolution(report: EvolutionReport) -> None:
    before, after = report.real_event_baseline, report.real_event_evolved
    assert before is not None and after is not None
    assert before.allocation_by_district == {"A-03": 4, "B-01": 2}
    assert after.allocation_by_district == {"A-03": 6, "B-01": 0}
    assert before.unmet_by_district == {"A-03": 2, "B-01": 0}
    assert after.unmet_by_district == {"A-03": 0, "B-01": 2}
    assert after.transfers and "B-01->A-03" in after.transfers[0]
    assert "40" in after.transfers[0]
    # 让位后的 B-01 缺口必须被显式上报，不能悄悄消失
    assert any("B-01" in item for item in after.escalation)


def test_feedback_digest_is_verifiable(report: EvolutionReport, cases) -> None:
    assert report.case_digest == cases_digest(cases)
    assert report.feedback_verified is True


def test_evolution_is_deterministic(cases) -> None:
    first = evolve(cases).model_dump(mode="json")
    second = evolve(cases).model_dump(mode="json")
    assert first == second


def test_candidate_generation_follows_attribution() -> None:
    initial = default_policy()
    patches = candidate_patches("transfer", initial)
    assert patches[0]["weight_closure"] == 1.0
    assert initial.weight_closure == 0.0, "候选不能就地修改 active 策略"

    after_priority = initial.with_updates({"weight_closure": 1.0, "transfer_penalty": 0.1})
    assert candidate_patches("transfer", after_priority)[0] == {"weight_hazard": 0.25}

    # 已收敛时没有可改进候选，只能探针
    assert candidate_patches("", initial) == []
    assert probe_patches(initial)


def test_worst_dimension_picks_largest() -> None:
    evaluation = evaluate_policy(default_policy(), load_cases())
    assert worst_dimension(evaluation) == max(
        evaluation.dimensions, key=lambda key: (evaluation.dimensions[key],)
    )
    converged = evaluate_policy(
        AllocationPolicy(weight_closure=1.0, weight_hazard=0.25, transfer_penalty=0.1),
        load_cases(),
    )
    assert worst_dimension(converged) == ""


def test_markdown_reports_before_and_after_numbers(report: EvolutionReport) -> None:
    markdown = render_markdown(report)
    assert "进化前 vs 进化后" in markdown
    assert f"{report.initial_train.deviation:.4f}" in markdown
    assert f"{report.final_train.deviation:.4f}" in markdown
    assert "受控评测环境下的反馈信号" in markdown
    assert "回滚验证" in markdown
    assert "WO-20261029-007" in markdown


def test_max_rounds_limits_the_loop(cases) -> None:
    report = evolve(cases, max_rounds=1, include_real_event=False)
    assert len(report.rounds) == 1
    assert report.rounds[0].action == "promoted"
    assert report.final_train.deviation < report.initial_train.deviation
    assert report.real_event_baseline is None


# --- P9.1：人工评审第二信号 ----------------------------------------------


def test_sample_review_is_violated_first_then_satisfied() -> None:
    report = evolve(reviews=load_reviews())
    assert report.review_cases == 1
    [summary] = report.reviews
    assert summary.review_id == "REV-001"
    assert summary.decision == "modify"
    assert summary.deviation_before > 0.1
    assert summary.deviation_after == 0.0
    assert summary.expected_allocation == {"A-03": 5, "B-01": 1}


def test_review_constraint_can_block_every_candidate() -> None:
    """给一条资源上不可能满足的评审结论，门禁必须全部拒绝、active 不动。"""
    review = load_reviews()[0]
    impossible = review.model_copy(
        update={"review_id": "REV-IMPOSSIBLE", "expected_allocation": {"A-03": 5, "B-01": 2}}
    )
    report = evolve(reviews=[impossible], include_real_event=False)
    assert report.promoted_rounds == 0
    assert report.rounds and report.rounds[0].action == "rejected"
    assert "违反人工评审结论" in report.rounds[0].reason
    assert report.final_policy.policy_version == "v1"
    assert report.reviews[0].deviation_after > 0.0


# --- P9.1：语义化版本标签 --------------------------------------------------


def test_releases_carry_semantic_labels_and_triggers() -> None:
    report = evolve(reviews=load_reviews(), include_real_event=False)
    labels = [item.label for item in report.releases]
    assert labels == ["flood-response-v1.1", "flood-response-v1.2"]
    assert report.final_label == "flood-response-v1.2"
    assert [item.parent_label for item in report.releases] == [
        "flood-response-v1.0",
        "flood-response-v1.1",
    ]
    assert [item.trigger_feedback_id for item in report.releases] == ["p9-round-1", "p9-round-2"]
    assert all(item.trigger_review_ids == ["REV-001"] for item in report.releases)
    assert all(item.expected_improvement > 0 for item in report.releases)
    total = sum(item.expected_improvement for item in report.releases)
    assert total == pytest.approx(report.train_deviation_drop(), abs=1e-6)


# --- P9.1：分层记忆 warm start -------------------------------------------


def test_second_session_starts_from_memory() -> None:
    showcase = run_showcase()
    first, second = showcase.first, showcase.second
    assert second.warm_start is not None
    assert second.warm_start.label == first.final_label
    assert second.initial_train.deviation == first.final_train.deviation == 0.0
    assert len(second.rounds) < len(first.rounds), "记忆命中后不应再从头搜"
    assert [item.action for item in second.rounds] == ["rejected"]
    assert len(showcase.memory_entries) == 1
    entry = showcase.memory_entries[0]
    assert entry.label == first.final_label
    assert entry.outcome["deviation"] == 0.0
    assert entry.review_ids == ["REV-001"]


def test_memory_persists_across_sessions(tmp_path) -> None:
    path = tmp_path / "decision_memory.json"
    first_run = run_showcase(memory_path=path)
    assert path.is_file()
    assert first_run.memory_path.endswith("decision_memory.json")

    second_run = run_showcase(memory_path=path, reuse_memory=True)
    assert second_run.first.warm_start is not None
    assert second_run.first.initial_train.deviation == 0.0
    assert len(DecisionMemory.load(path)) == 1


def test_showcase_is_deterministic() -> None:
    first = run_showcase().model_dump(mode="json")
    second = run_showcase().model_dump(mode="json")
    assert first == second


def test_entries_from_report_match_final_policy() -> None:
    cases = load_cases()
    report = evolve(cases, include_real_event=False)
    [entry] = entries_from_report(report, cases)
    assert entry.scope.value == "agent"
    assert entry.label == report.final_label
    assert entry.params["weight_closure"] == report.final_policy.weight_closure
    assert entry.outcome["deviation"] == report.final_train.deviation


def test_showcase_markdown_documents_memory_and_review() -> None:
    showcase = run_showcase(include_real_event=False)
    markdown = render_showcase_markdown(showcase)
    assert "分层记忆" in markdown
    assert "人工协同反馈" in markdown
    assert "flood-response-v1.2" in markdown
    assert "角色扮演" in markdown
    assert "未在真实 Nexent 平台验证" in markdown
    assert "回滚验证" in markdown
    assert f"{showcase.first.initial_train.deviation:.4f}" in markdown
