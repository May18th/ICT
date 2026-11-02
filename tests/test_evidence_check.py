"""对外数字自检的测试：能通过、能抓漂移、不把当前输出当期望。"""

from __future__ import annotations

import copy
import json

import pytest

from decision_poc.evidence_check import (
    EXPECTATIONS_PATH,
    EvidenceReport,
    collect_actuals,
    load_expectations,
    run_evidence_check,
    to_markdown,
)


@pytest.fixture(scope="module")
def expectations() -> dict:
    return load_expectations()


@pytest.fixture(scope="module")
def actuals() -> dict:
    return collect_actuals()


@pytest.fixture(scope="module")
def report(expectations: dict, actuals: dict) -> EvidenceReport:
    return run_evidence_check(expectations, actuals=actuals)


def test_all_public_numbers_match(report: EvidenceReport) -> None:
    assert report.passed is True, [item.path for item in report.failures]
    assert report.failed_count == 0
    assert report.total >= 40, "检查项太少说明期望文件被削过"


def test_report_covers_every_public_claim(report: EvidenceReport) -> None:
    paths = {item.path for item in report.items}
    for path in [
        "p6_deduction.six_hops",
        "p6_deduction.conflict_ids",
        "p6_deduction.resource_gap",
        "p6_deduction.isolate_before_drain",
        "p8_eval.cases_passed",
        "p8_eval.citation_completeness",
        "p9_evolution.train.initial_deviation",
        "p9_evolution.train.final_deviation",
        "p9_evolution.holdout.final_deviation",
        "p9_evolution.final_label",
        "p9_evolution.review.deviation_before",
        "p9_evolution.warm_start.second_initial_deviation",
        "p9_evolution.real_event.evolved_allocation",
        "mcp.tools_count",
    ]:
        assert any(
            item == path or item.startswith(path + ".") for item in paths
        ), path


def test_drift_is_detected(expectations: dict, actuals: dict) -> None:
    """有人偷偷改规则让数字变了，自检必须报失败，而不是自己更新期望。"""
    tampered = copy.deepcopy(expectations)
    tampered["checks"]["p9_evolution"]["train"]["initial_deviation"] = 0.2
    drifted = run_evidence_check(tampered, actuals=actuals)
    assert drifted.passed is False
    assert drifted.failed_count == 1
    [failure] = drifted.failures
    assert failure.path == "p9_evolution.train.initial_deviation"
    assert failure.expected == 0.2


def test_float_tolerance_is_tight(expectations: dict, actuals: dict) -> None:
    loose = copy.deepcopy(expectations)
    loose["checks"]["p9_evolution"]["train"]["initial_deviation"] = 0.226428 + 5e-7
    assert run_evidence_check(loose, actuals=actuals).passed is True
    tight = copy.deepcopy(expectations)
    tight["checks"]["p9_evolution"]["train"]["initial_deviation"] = 0.2265
    assert run_evidence_check(tight, actuals=actuals).passed is False


def test_missing_section_is_reported_as_failure(actuals: dict) -> None:
    drifted = run_evidence_check(
        {"checks": {"p11_imaginary": {"deviation": 0.0}}}, actuals=actuals
    )
    assert drifted.passed is False
    [failure] = drifted.failures
    assert failure.path.startswith("p11_imaginary")
    assert "缺失" in str(failure.actual)


def test_markdown_reports_pass_state(expectations: dict, actuals: dict) -> None:
    markdown = to_markdown(run_evidence_check(expectations, actuals=actuals))
    assert "全部一致" in markdown
    assert "| 检查项 | 期望 | 实际 | 结果 |" in markdown


def test_markdown_reports_failures(expectations: dict, actuals: dict) -> None:
    tampered = copy.deepcopy(expectations)
    tampered["checks"]["p8_eval"]["cases_passed"] = 21
    markdown = to_markdown(run_evidence_check(tampered, actuals=actuals))
    assert "存在漂移" in markdown
    assert "p8_eval.cases_passed" in markdown


def test_report_is_json_serializable(expectations: dict, actuals: dict) -> None:
    report = run_evidence_check(expectations, actuals=actuals)
    payload = json.loads(report.model_dump_json())
    assert payload["passed"] is True
    assert payload["expectations_digest"]


def test_expectations_file_is_documented() -> None:
    raw = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    assert raw["note"].strip()
    assert raw["schema_version"] == "evidence-expectations-1"
