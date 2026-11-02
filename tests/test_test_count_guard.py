"""测试数不能漂：pytest 会话实测值与对外文档声明必须等于冻结期望。

为什么单独一个文件
------------------
`evidence_check` 不能在进程内递归跑 pytest，所以"真跑出来多少项"这一项由本文件的
pytest 会话自查（`request.session.testscollected`）；而"文档里写了多少项"由
`evidence_check.collect_actuals()` 扫描对外文档核对。两边都锚到
`samples/eval/evidence_expectations.json`：

- `test_baseline.collected`：冻结的实测值；
- `checks.test_suite.docs_declared`：冻结的文档声明值（evidence_check 逐项比对）。

改测试数量时必须三处同步：期望文件 + README + 竞赛文档，缺一处就失败。
"""

from __future__ import annotations

import copy

import pytest

from decision_poc.evidence_check import (
    docs_declared_test_counts,
    load_expectations,
    run_evidence_check,
)


@pytest.fixture(scope="module")
def expectations() -> dict:
    return load_expectations()


def test_docs_declare_one_test_count(expectations: dict) -> None:
    """对外文档只能出现一种测试数，且等于冻结值。"""
    frozen = expectations["test_baseline"]["collected"]
    counted = docs_declared_test_counts()
    assert counted == [frozen], (
        f"文档声明的测试数 {counted} 与冻结值 {frozen} 不一致；"
        "统一 README / docs/competition-gap-audit.md / preliminary-submission-draft.md 后再改期望"
    )


def test_collected_count_matches_frozen_baseline(request, expectations: dict) -> None:
    """本次 pytest 真收集到的用例数必须等于冻结值——加/删测试都要先改期望。"""
    frozen = expectations["test_baseline"]["collected"]
    actual = request.session.testscollected
    assert actual == frozen, (
        f"实际收集到 {actual} 项测试，冻结值是 {frozen}；"
        "若是新增/删除测试，请同步 samples/eval/evidence_expectations.json "
        "的 test_baseline.collected 与 README / 竞赛文档里的对外数字"
    )


def test_evidence_check_detects_doc_drift(expectations: dict) -> None:
    """有人把文档里的测试数改旧，自检必须报失败而不是跟着变。"""
    tampered = copy.deepcopy(expectations["checks"]["test_suite"])
    tampered["docs_declared"] = [483]
    actual = {"test_suite": {"docs_declared": docs_declared_test_counts()}}
    report = run_evidence_check({"checks": {"test_suite": tampered}}, actuals=actual)
    assert report.passed is False
    [failure] = report.failures
    assert failure.path == "test_suite.docs_declared"
    assert failure.expected == [483]
