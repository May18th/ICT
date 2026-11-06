"""P8 评测自身的测试：用例够不够、指标算得对不对、结论稳不稳。

评测最容易出的问题是"看起来很专业但不可信"：用例不够、指标分母随手写、跑两遍
结果不一样。这里逐条钉住：
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc.urban_dataset import load_urban_assets
from decision_poc.urban_eval import (
    EvalCase,
    EvalReport,
    SIX_HOPS,
    evaluate,
    load_cases,
    to_markdown,
)

BASELINE = Path(__file__).resolve().parents[1] / "samples" / "eval" / "baseline_before_temporal_guard.json"


@pytest.fixture(scope="module")
def cases() -> list[EvalCase]:
    return load_cases()


@pytest.fixture(scope="module")
def report(cases: list[EvalCase]) -> EvalReport:
    return evaluate(cases)


def test_case_set_covers_every_required_shape(cases: list[EvalCase]) -> None:
    assert len(cases) >= 20, "任务单要求至少 20 个事件案例"
    kinds = {case.kind for case in cases}
    assert kinds == {"normal", "conflict", "insufficient", "expired", "permission"}
    ids = [case.case_id for case in cases]
    assert len(ids) == len(set(ids)), "用例编号不能重复"


def test_every_case_declares_a_checkable_expectation(cases: list[EvalCase]) -> None:
    for case in cases:
        expect = case.expect
        declared = [
            expect.status,
            expect.conflict_kinds,
            expect.must_objects,
            expect.gap,
            expect.refused,
            expect.error_code,
            expect.warning_level,
            expect.no_warning,
            expect.depth_cm is not None,
            expect.min_actions is not None,
            expect.min_affected is not None,
            expect.conflict_ids,
            expect.citation_completeness is not None,
            expect.six_hops is not None,
        ]
        assert any(bool(item) for item in declared), f"{case.case_id} 没有可核查的期望"


def test_all_cases_pass(report: EvalReport) -> None:
    """这条就是验收门：任何一条用例失败，评测就是红的。"""
    assert report.metrics.cases_failed == 0, [item.failures for item in report.failures]


def test_metrics_are_computed_from_the_run(report: EvalReport) -> None:
    metrics = report.metrics
    assert metrics.assets_total == 19 and metrics.assets_parsed == 19
    assert metrics.unmapped_columns == 0
    assert metrics.triples > 0 and metrics.relation_kinds > 0
    assert metrics.locator_hit_rate == 1.0
    assert metrics.citation_completeness == 1.0
    assert metrics.unsupported_claims == 0
    assert metrics.six_hop_rate == 1.0
    assert metrics.six_hop_cases <= metrics.eligible_cases
    assert metrics.p50_ms > 0 and metrics.p95_ms >= metrics.p50_ms


def test_safety_groups_are_all_green(report: EvalReport) -> None:
    for kind, value in report.metrics.safety_by_kind.items():
        passed, total = value.split("/")
        assert passed == total, f"{kind} 组有失败：{value}"


def test_correctness_is_reproducible(cases: list[EvalCase], report: EvalReport) -> None:
    """时间指标可以抖，正确性指标必须逐位一致。"""
    again = evaluate(cases)
    assert again.correctness_fingerprint() == report.correctness_fingerprint()


def test_wrong_expectation_is_reported_with_reason(cases: list[EvalCase]) -> None:
    """判据不是摆设：把期望改错，必须报出具体哪一条不满足。"""
    broken = [case.model_copy(deep=True) for case in cases[:1]]
    broken[0].expect.status = ["blocked"]
    broken[0].expect.gap = {"RES-PUMP-M": 99}
    outcome = evaluate(broken).outcomes[0]
    assert outcome.passed is False
    assert any("状态" in item for item in outcome.failures)
    assert any("资源缺口" in item for item in outcome.failures)


def test_evaluation_does_not_touch_the_dataset(report: EvalReport) -> None:
    before = {asset.file_name: asset.checksum_sha256 for asset, _ in load_urban_assets()}
    evaluate(load_cases())
    after = {asset.file_name: asset.checksum_sha256 for asset, _ in load_urban_assets()}
    assert before == after


def test_markdown_has_the_sections_reviewers_ask_for(report: EvalReport) -> None:
    baseline = EvalReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))
    text = to_markdown(report, baseline)
    for heading in ("## 1 评测项", "## 2 安全处理", "## 3 用例明细", "## 5 改进前后对比"):
        assert heading in text
    assert "17/22" in text and "22/22" in text
    assert set(SIX_HOPS)  # 六跳定义是本模块的公共契约


def test_baseline_shows_the_guard_was_the_difference(report: EvalReport) -> None:
    """改进前 17/22，改进后 22/22：差的那 5 条全是时点/状态闸门挡下的。"""
    baseline = EvalReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))
    assert baseline.metrics.cases_passed == 17
    assert report.metrics.cases_passed == 22
    failed_ids = {item.case_id for item in baseline.failures}
    assert failed_ids == {"I02", "I03", "I04", "I05", "I06"}
    for item in baseline.outcomes:
        if item.case_id in failed_ids:
            assert item.refused is False, "改进前这些用例是被当成正常事件推演掉的"


def test_latest_json_matches_a_fresh_run(report: EvalReport) -> None:
    """仓库里提交的结果文件必须能被重新跑出来（时间字段除外）。"""
    latest = Path(__file__).resolve().parents[1] / "samples" / "eval" / "latest.json"
    if not latest.exists():  # pragma: no cover - 首次生成前
        pytest.skip("latest.json 尚未生成")
    stored = json.loads(latest.read_text(encoding="utf-8"))
    fresh = json.loads(report.model_dump_json())
    assert [item["case_id"] for item in stored["outcomes"]] == [
        item["case_id"] for item in fresh["outcomes"]
    ]
    assert stored["metrics"]["cases_passed"] == fresh["metrics"]["cases_passed"]
    assert stored["metrics"]["triples"] == fresh["metrics"]["triples"]
