"""P11 消融阶梯自身的测试：分母一致、阶梯方向可解释、结论确定性。

消融评测最容易出的问题是"看起来有五档，其实分母各不相同"：某一档在应当拒答的用例上
也给出了动作，于是"可执行率"凭空变高。这里逐条钉住口径，而不是钉住某个好看的数字。
"""

from __future__ import annotations

import pytest

from decision_poc.ablation import (
    CONFIG_NAMES,
    FROZEN_METRICS,
    METRIC_SPECS,
    AblationReport,
    evaluate,
    parse_depth,
    rules_only_actions,
    to_markdown,
)
from decision_poc.skills import SkillPipeline, SkillRuntime


@pytest.fixture(scope="module")
def report() -> AblationReport:
    return evaluate()


def test_five_configs_share_one_case_set(report: AblationReport) -> None:
    assert [item.config for item in report.configs] == list(CONFIG_NAMES)
    assert {item.cases for item in report.configs} == {22}


def test_every_metric_has_a_published_definition() -> None:
    """指标不能只有名字：每个都要在报告里写清中文名与分母口径。"""
    for metric, (label, definition) in METRIC_SPECS.items():
        assert label and definition, f"指标 {metric} 缺中文名或口径"
        assert "例" in definition or "%" in definition, f"指标 {metric} 的口径没写分母"


def test_denominators_are_identical_across_configs(report: AblationReport) -> None:
    """分母必须一致，否则"能力差异"里有分母差异的功劳。"""
    for metric in METRIC_SPECS:
        totals = {item.counts[metric][1] for item in report.configs}
        assert len(totals) == 1, f"{metric} 的分母在不同档位不一致：{totals}"


def test_rules_baseline_trades_safety_for_actions(report: AblationReport) -> None:
    rules = report.config("rules_only")
    assert rules.metrics["deliverable_rate"] > 0, "阈值基线至少要在正常事件上给出动作"
    assert rules.metrics["safety_rate"] == 0.0, "阈值基线不看时点/身份，安全处理率必须为 0"
    assert rules.metrics["auditable_rate"] == 0.0


def test_semantic_layer_filters_expiry_but_cannot_deduce(report: AblationReport) -> None:
    semantic = report.config("semantic_only")
    assert semantic.metrics["expiry_filter_rate"] == 1.0
    assert semantic.metrics["six_hop_rate"] == 0.0


def test_single_skill_turns_deduction_on(report: AblationReport) -> None:
    single = report.config("single_skill")
    assert single.metrics["six_hop_rate"] > 0.0
    assert single.metrics["conflict_recall"] > 0.0
    assert single.metrics["safety_rate"] == 1.0


def test_multi_skill_adds_scope_and_resource_only(report: AblationReport) -> None:
    single, multi = report.config("single_skill"), report.config("multi_skill")
    assert multi.metrics["impact_resource_rate"] > single.metrics["impact_resource_rate"]
    # 影响范围/资源核算不该改变六跳与安全处理——那两层本来就已经在
    assert multi.metrics["six_hop_rate"] == single.metrics["six_hop_rate"]
    assert multi.metrics["safety_rate"] == single.metrics["safety_rate"]


def test_audit_layer_moves_only_audit_metrics(report: AblationReport) -> None:
    multi, full = report.config("multi_skill"), report.config("full_audited")
    assert full.metrics["auditable_rate"] > multi.metrics["auditable_rate"]
    assert full.metrics["citation_completeness"] > multi.metrics["citation_completeness"]
    assert full.metrics["six_hop_rate"] == multi.metrics["six_hop_rate"]
    assert full.metrics["impact_resource_rate"] == multi.metrics["impact_resource_rate"]


def test_report_is_deterministic(report: AblationReport) -> None:
    again = evaluate()
    assert again.fingerprint() == report.fingerprint()
    assert again.model_dump(mode="json")["rows"] == report.model_dump(mode="json")["rows"]
    # 精确 p 值也必须逐位一致：这里并不存在"随机种子"，两次运行必须完全相同
    assert [(t.left, t.right, t.metric, t.p_value) for t in again.pairwise] == [
        (t.left, t.right, t.metric, t.p_value) for t in report.pairwise
    ]


def test_leave_one_out_stats_are_deterministic_and_bracket_the_mean(report: AblationReport) -> None:
    """逐例剔除是确定性算法：范围必须夹住全量值，且分母 n 与实际用例数一致。"""
    for config in CONFIG_NAMES:
        for metric in FROZEN_METRICS:
            stat = report.stats[config][metric]
            assert 0.0 <= stat.mean <= 1.0
            assert 0.0 <= stat.loo_min <= stat.loo_max <= 1.0
            assert stat.loo_min - 1e-9 <= stat.mean <= stat.loo_max + 1e-9
            assert stat.n >= 1
            if stat.n > 1:
                assert stat.dropped_case, "n>1 时必须能报出'剔除哪个用例影响最大'"


def test_pairwise_p_values_are_exact_not_simulated(report: AblationReport) -> None:
    """精确符号翻转的 p 必然是 k/2^n：乘以 2^22 后是整数，随机近似做不到。"""
    pairs = {(test.left, test.right) for test in report.pairwise}
    assert pairs == set(zip(CONFIG_NAMES, CONFIG_NAMES[1:]))
    for test in report.pairwise:
        assert 0.0 <= test.p_value <= 1.0
        assert test.metric in FROZEN_METRICS
        scaled = test.p_value * (1 << 22)
        assert abs(scaled - round(scaled)) < 1e-6, f"{test.metric} 的 p 值不是精确 k/2^22"
    # 退化为全零差值的组合：p 必须正好是 1（没有任何证据支持"有差异"）
    from decision_poc.ablation import _exact_sign_flip_p

    assert _exact_sign_flip_p([0.0, 0.0, 0.0]) == 1.0
    # 三例全为正差：8 种符号组合里只有 2 种满足 |S| >= 3，精确 p = 0.25
    assert _exact_sign_flip_p([1.0, 1.0, 1.0]) == 0.25


def test_markdown_states_the_deterministic_caveat(report: AblationReport) -> None:
    text = to_markdown(report)
    assert "不引入任何随机数" in text
    assert "精确符号翻转检验" in text
    assert "不外推到总体" in text
    assert "不宣称区块链" in text


def test_parse_depth_reads_only_real_numbers() -> None:
    assert parse_depth("积水深约 30cm，雨水口反涌") == 30.0
    assert parse_depth("积水约 18 cm") == 18.0
    assert parse_depth("基坑内积水上涨") is None
    assert parse_depth("") is None


def test_rules_only_actions_follow_the_published_thresholds() -> None:
    incidents = {
        "CLOSE": {"attributes": {"description": "积水 30cm", "road_id": "R-101"}},
        "PUMP": {"attributes": {"description": "积水 18cm", "road_id": "R-201"}},
        "PATROL": {"attributes": {"description": "轻微积水 5cm", "road_id": "R-202"}},
        "NO_DEPTH": {"attributes": {"description": "路面沉陷", "road_id": "R-103"}},
    }
    assert len(rules_only_actions("CLOSE", incidents)) == 3
    assert len(rules_only_actions("PUMP", incidents)) == 2
    assert rules_only_actions("PATROL", incidents) == ["巡查"]
    assert rules_only_actions("NO_DEPTH", incidents) == []
    assert rules_only_actions("WO-2099-000", incidents) == []


def test_stage_subset_keeps_canonical_order_and_rejects_unknown() -> None:
    runtime = SkillRuntime()
    pipeline = SkillPipeline(runtime, stages=["deduce", "understand"])
    assert pipeline.stages == ("understand", "deduce")
    assert [skill.stage for skill in pipeline._skills] == ["understand", "deduce"]
    with pytest.raises(ValueError):
        SkillPipeline(runtime, stages=["no_such_stage"])
