"""P8 评测：把"跑得通"变成"跑得对、可复现、够快"。

评测不是再跑一遍测试。测试问的是"这段代码改坏了没有"，评测问的是"这套系统面对
不同形态的事件，产出的东西对不对、说不说得清、拒答得干不干净"。所以这里的三层
结构是固定的：

1. **用例**（`samples/eval/urban_cases.json`）：每个用例只声明"期望看到什么"，
   不写"期望算法怎么走"。正常、冲突、证据不足、过期数据各占一组，共 20+ 例；
2. **判据**：一条用例的期望被拆成可数的断言（状态、六跳、冲突类型、缺口、必须
   出现的对象、必须能定位的证据、引用完整率、拒答理由），失败就记下具体哪条不
   满足——不是"这个用例挂了"；
3. **指标**：资产解析、实体关系、检索命中、多跳完整、引用完整、响应时间、安全处
   理。前六项对应用户任务单里的评测项，第七项是这套系统真正的差异点。

三条设计约束，都是为了评测结果能被人复核：

- 评测不写库、不落账（`persist=False`），跑一百遍不改一个文件；
- 指标全部可从 `PipelineReport` 复算，不看日志猜；
- 时间指标只报 p50/p95 并允许波动，正确性指标必须逐位一致，`baseline` 比对时
  只比后者。
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from pydantic import Field

from .contracts import StrictModel
from .skills import PipelineReport, SkillPipeline, SkillRuntime
from .table_normalizer import normalize_table_file
from .urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

DEFAULT_CASES_PATH = Path(__file__).resolve().parents[2] / "samples" / "eval" / "urban_cases.json"

SIX_HOPS = ("observed", "affected", "owner", "conflict", "resource", "action")


class CaseExpect(StrictModel):
    """一条用例的可核查期望。没写的字段就不检查，不拿默认值凑数。"""

    status: list[str] = Field(default_factory=list)
    kind: str = ""
    publishable: bool | None = None
    conflict_kinds: list[str] = Field(default_factory=list)
    conflict_ids: list[str] = Field(default_factory=list)
    min_affected: int | None = None
    must_objects: list[str] = Field(default_factory=list)
    must_locators: list[str] = Field(default_factory=list)
    gap: dict[str, int] = Field(default_factory=dict)
    refused: bool | None = None
    error_code: str | None = None
    warning_level: str | None = None
    depth_cm: float | None = None
    min_actions: int | None = None
    six_hops: bool | None = None
    citation_completeness: float | None = None


class EvalCase(StrictModel):
    case_id: str
    title: str
    kind: str  # normal / conflict / insufficient / expired / permission
    event_id: str
    as_of: str
    caller_labels: list[str] = Field(default_factory=list)
    expect: CaseExpect = Field(default_factory=CaseExpect)


class CaseOutcome(StrictModel):
    case_id: str
    title: str
    kind: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    status: str = ""
    publishable: bool = False
    stages: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    supported_claims: int = 0
    total_claims: int = 0
    citation_completeness: float = 0.0
    locator_hits: int = 0
    locator_expected: int = 0
    elapsed_ms: float = 0.0


class EvalMetrics(StrictModel):
    cases_total: int = 0
    cases_passed: int = 0
    cases_failed: int = 0
    # 资产解析
    assets_total: int = 0
    assets_parsed: int = 0
    unmapped_columns: int = 0
    # 实体关系
    triples: int = 0
    relation_kinds: int = 0
    # 检索命中
    locator_hit_rate: float = 1.0
    # 多跳
    six_hop_rate: float = 0.0
    # 引用完整
    citation_completeness: float = 1.0
    unsupported_claims: int = 0
    # 响应时间
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    # 安全处理
    safety_by_kind: dict[str, str] = Field(default_factory=dict)


class EvalReport(StrictModel):
    schema_version: str = "urban-eval-1"
    dataset_snapshot: str = ""
    metrics: EvalMetrics = Field(default_factory=EvalMetrics)
    outcomes: list[CaseOutcome] = Field(default_factory=list)

    @property
    def failures(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if not item.passed]

    def correctness_fingerprint(self) -> str:
        """只把"正确性"那部分压成指纹：时间不参与，跑两遍必须一致。"""
        payload = {
            "cases": [
                [item.case_id, item.passed, item.status, item.publishable, item.conflicts]
                for item in self.outcomes
            ],
            "metrics": {
                "passed": self.metrics.cases_passed,
                "triples": self.metrics.triples,
                "hop": self.metrics.six_hop_rate,
                "citation": self.metrics.citation_completeness,
                "locator": self.metrics.locator_hit_rate,
                "unmapped": self.metrics.unmapped_columns,
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_cases(path: Path | str = DEFAULT_CASES_PATH) -> list[EvalCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [EvalCase.model_validate(item) for item in payload["cases"]]


# --- 判据 ----------------------------------------------------------------


def _evidence_refs(report: PipelineReport) -> list[str]:
    refs: set[str] = set()
    if report.audit:
        for claim in report.audit.claims:
            refs.update(claim.evidence_refs)
    if report.deduction:
        for stage in report.deduction.stages:
            refs.update(stage.evidence_refs)
    return sorted(refs)


def _check(case: EvalCase, report: PipelineReport) -> list[str]:
    """逐条比对期望；每条失败都带"期望什么、看到什么"，不用回读报告猜。"""
    expect = case.expect
    failures: list[str] = []
    stages = [stage.stage for stage in report.deduction.stages] if report.deduction else []

    if expect.status and report.status not in expect.status:
        failures.append(f"状态 期望 {expect.status} 实际 {report.status}")
    if expect.kind and expect.kind != case.kind:
        failures.append(f"用例分类不一致：expect.kind={expect.kind} vs case.kind={case.kind}")
    if expect.publishable is not None and report.publishable is not expect.publishable:
        failures.append(f"可发布 期望 {expect.publishable} 实际 {report.publishable}")

    if expect.conflict_kinds:
        actual = sorted({c.kind.value for c in report.deduction.conflicts}) if report.deduction else []
        missing = [kind for kind in expect.conflict_kinds if kind not in actual]
        if missing:
            failures.append(f"冲突类型缺 {missing}，实际 {actual}")
    if expect.conflict_ids:
        actual = [c.conflict_id for c in report.deduction.conflicts] if report.deduction else []
        for conflict_id in expect.conflict_ids:
            if conflict_id not in actual:
                failures.append(f"冲突编号缺 {conflict_id}，实际 {actual}")

    if expect.min_affected is not None:
        actual = len(report.impact.affected) if report.impact else 0
        if actual < expect.min_affected:
            failures.append(f"受影响对象 期望 ≥{expect.min_affected} 实际 {actual}")

    if expect.must_objects:
        names = {item.name for item in report.impact.affected} if report.impact else set()
        for name in expect.must_objects:
            if name not in names:
                failures.append(f"受影响对象缺 {name}")

    for locator in expect.must_locators:
        cited = _evidence_refs(report)
        if not any(locator in ref for ref in cited):
            failures.append(f"证据定位缺 {locator}")

    if expect.gap:
        actual = {gap.resource_id: gap.gap for gap in report.deduction.resource_gaps} if report.deduction else {}
        if actual != expect.gap:
            failures.append(f"资源缺口 期望 {expect.gap} 实际 {actual}")

    if expect.refused:
        refused = bool(
            report.deduction
            and len(report.deduction.stages) == 1
            and report.deduction.stages[0].status == "insufficient"
        )
        if not refused:
            failures.append("应拒答（只有一跳且 insufficient），实际给出了多跳结论")
        if report.publishable:
            failures.append("拒答的结论被标为可发布")

    if expect.error_code:
        codes = [step.error_code for step in report.steps if step.error_code]
        if expect.error_code not in codes:
            failures.append(f"错误码 期望 {expect.error_code} 实际 {codes}")

    if expect.warning_level is not None:
        actual = report.facts.warning_level if report.facts else None
        if actual != expect.warning_level:
            failures.append(f"预警等级 期望 {expect.warning_level} 实际 {actual}")

    if expect.depth_cm is not None:
        actual = report.facts.depth_cm if report.facts else None
        if actual != expect.depth_cm:
            failures.append(f"积水深度 期望 {expect.depth_cm} 实际 {actual}")

    if expect.min_actions is not None:
        actual = len(report.deduction.action_order) if report.deduction else 0
        if actual < expect.min_actions:
            failures.append(f"处置动作 期望 ≥{expect.min_actions} 实际 {actual}")

    if expect.six_hops is not None:
        complete = set(SIX_HOPS).issubset(set(stages))
        if complete is not expect.six_hops:
            failures.append(f"六跳齐全 期望 {expect.six_hops} 实际 {complete}（{sorted(set(stages))}）")

    if expect.citation_completeness is not None:
        actual = report.audit.citation_completeness if report.audit else 0.0
        if round(actual, 4) != round(expect.citation_completeness, 4):
            failures.append(
                f"引用完整率 期望 {expect.citation_completeness} 实际 {actual}"
            )

    return failures


# --- 指标 ----------------------------------------------------------------


def _asset_metrics() -> tuple[int, int, int]:
    assets = load_urban_assets()
    parsed = sum(
        1 for asset, _ in assets if getattr(asset.parse_status, "value", asset.parse_status) == "parsed"
    )
    unmapped = 0
    for asset, _ in assets:
        if getattr(asset.modality, "value", asset.modality) != "table":
            continue
        table = normalize_table_file(asset, DEFAULT_DATASET_ROOT / asset.file_name)
        unmapped += len(table.unmapped_columns)
    return len(assets), parsed, unmapped


def evaluate(
    cases: list[EvalCase] | None = None,
    runtime: SkillRuntime | None = None,
    dataset_root: Path | str | None = None,
) -> EvalReport:
    cases = cases if cases is not None else load_cases()
    runtime = runtime or SkillRuntime(dataset_root=dataset_root)
    outcomes: list[CaseOutcome] = []
    durations: list[float] = []

    for case in cases:
        started = time.perf_counter()
        report = SkillPipeline(runtime).run(
            case.event_id,
            case.as_of,
            caller_labels=case.caller_labels,
            persist=False,
        )
        elapsed = (time.perf_counter() - started) * 1000
        durations.append(elapsed)
        failures = _check(case, report)

        cited = _evidence_refs(report)
        hits = sum(1 for locator in case.expect.must_locators if any(locator in ref for ref in cited))
        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                title=case.title,
                kind=case.kind,
                passed=not failures,
                failures=failures,
                status=report.status,
                publishable=report.publishable,
                stages=[stage.stage for stage in report.deduction.stages] if report.deduction else [],
                conflicts=(
                    [f"{c.conflict_id}:{c.kind.value}" for c in report.deduction.conflicts]
                    if report.deduction
                    else []
                ),
                supported_claims=report.audit.supported if report.audit else 0,
                total_claims=len(report.audit.claims) if report.audit else 0,
                citation_completeness=report.audit.citation_completeness if report.audit else 0.0,
                locator_hits=hits,
                locator_expected=len(case.expect.must_locators),
                elapsed_ms=round(elapsed, 1),
            )
        )

    total, parsed, unmapped = _asset_metrics()
    triples = len(runtime.store.triples())
    relation_kinds = len({triple.predicate.value for triple in runtime.store.triples()})
    expected_locators = sum(item.locator_expected for item in outcomes)
    hit_locators = sum(item.locator_hits for item in outcomes)
    audited = [item for item in outcomes if item.total_claims]
    six_full = [item for item in outcomes if set(SIX_HOPS).issubset(set(item.stages))]

    safety: dict[str, str] = {}
    for kind in sorted({item.kind for item in outcomes}):
        group = [item for item in outcomes if item.kind == kind]
        passed = sum(1 for item in group if item.passed)
        safety[kind] = f"{passed}/{len(group)}"

    metrics = EvalMetrics(
        cases_total=len(outcomes),
        cases_passed=sum(1 for item in outcomes if item.passed),
        cases_failed=sum(1 for item in outcomes if not item.passed),
        assets_total=total,
        assets_parsed=parsed,
        unmapped_columns=unmapped,
        triples=triples,
        relation_kinds=relation_kinds,
        locator_hit_rate=round(hit_locators / expected_locators, 4) if expected_locators else 1.0,
        six_hop_rate=round(len(six_full) / len(outcomes), 4) if outcomes else 0.0,
        citation_completeness=(
            round(statistics.fmean(item.citation_completeness for item in audited), 4)
            if audited
            else 0.0
        ),
        unsupported_claims=sum(item.total_claims - item.supported_claims for item in outcomes),
        p50_ms=round(statistics.median(durations), 1) if durations else 0.0,
        p95_ms=round(sorted(durations)[max(0, int(len(durations) * 0.95) - 1)], 1) if durations else 0.0,
        safety_by_kind=safety,
    )
    return EvalReport(
        dataset_snapshot=runtime.store.snapshot_hash(),
        metrics=metrics,
        outcomes=outcomes,
    )


# --- 输出 ----------------------------------------------------------------


def to_markdown(report: EvalReport, baseline: EvalReport | None = None) -> str:
    m = report.metrics
    lines = [
        "# P8 评测报告",
        "",
        "由 `python -m decision_poc.urban_eval --report <path>` 生成，**不要手改**。",
        "",
        f"- 数据集快照：`{report.dataset_snapshot[:16]}`",
        f"- 用例：{m.cases_total} 例，通过 {m.cases_passed}，失败 {m.cases_failed}",
        "",
        "## 1 评测项",
        "",
        "| 评测项 | 数值 | 口径 |",
        "| --- | --- | --- |",
        f"| 资产解析 | {m.assets_parsed}/{m.assets_total} | `parse_status=parsed` 的资产数 |",
        f"| 未识别列 | {m.unmapped_columns} | 表头没有对应规范列的列数，逐表统计 |",
        f"| 实体关系 | {m.triples} 条 / {m.relation_kinds} 类 | 语义层的三元组总数与谓词种类 |",
        f"| 检索命中 | {m.locator_hit_rate:.2%} | 用例声明的必须证据定位中，实际引到的比例 |",
        f"| 多跳完整 | {m.six_hop_rate:.2%} | 六跳齐全的用例占全部用例比例 |",
        f"| 引用完整率 | {m.citation_completeness:.2%} | 审计报告的平均引用完整率 |",
        f"| 不可支撑结论 | {m.unsupported_claims} | 未通过引用闸门的 claim 数 |",
        f"| 响应时间 p50 / p95 | {m.p50_ms:.0f} ms / {m.p95_ms:.0f} ms | 单用例端到端（五 Skill 全流程） |",
        "",
        "## 2 安全处理（按用例类型）",
        "",
        "| 类型 | 通过 |",
        "| --- | --- |",
    ]
    for kind, value in m.safety_by_kind.items():
        lines.append(f"| {kind} | {value} |")

    lines += ["", "## 3 用例明细", "", "| 用例 | 类型 | 状态 | 可发布 | 结果 | 耗时 |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report.outcomes:
        mark = "通过" if item.passed else "失败"
        lines.append(
            f"| {item.case_id} {item.title} | {item.kind} | {item.status} | "
            f"{'是' if item.publishable else '否'} | {mark} | {item.elapsed_ms:.0f} ms |"
        )

    if report.failures:
        lines += ["", "## 4 失败案例", ""]
        for item in report.failures:
            lines.append(f"- **{item.case_id}** {item.title}：{'；'.join(item.failures)}")
    else:
        lines += ["", "## 4 失败案例", "", "无。", ]

    if baseline is not None:
        before, after = baseline.metrics, report.metrics
        lines += [
            "",
            "## 5 改进前后对比",
            "",
            "| 指标 | 改进前 | 改进后 |",
            "| --- | --- | --- |",
            f"| 通过用例 | {before.cases_passed}/{before.cases_total} | {after.cases_passed}/{after.cases_total} |",
            f"| 失败用例 | {before.cases_failed} | {after.cases_failed} |",
            f"| 多跳完整率 | {before.six_hop_rate:.2%} | {after.six_hop_rate:.2%} |",
            f"| 引用完整率 | {before.citation_completeness:.2%} | {after.citation_completeness:.2%} |",
            "",
            "失败的用例清单（改进前）：",
            "",
        ]
        for item in baseline.failures:
            lines.append(f"- {item.case_id} {item.title}：{'；'.join(item.failures)}")
    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------

__all__ = [
    "CaseExpect",
    "CaseOutcome",
    "EvalCase",
    "EvalMetrics",
    "EvalReport",
    "evaluate",
    "load_cases",
    "to_markdown",
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="urban decision POC evaluation")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--json", dest="json_path", default=None, help="写出机器可读结果")
    parser.add_argument("--report", dest="report_path", default=None, help="写出 Markdown 报告")
    parser.add_argument("--baseline", default=None, help="上一轮结果 JSON，用于改进前后对比")
    args = parser.parse_args(argv)

    report = evaluate(load_cases(args.cases))
    metrics = report.metrics
    print(
        f"用例 {metrics.cases_passed}/{metrics.cases_total} 通过，失败 {metrics.cases_failed}；"
        f"六跳 {metrics.six_hop_rate:.0%}，引用完整 {metrics.citation_completeness:.0%}，"
        f"检索命中 {metrics.locator_hit_rate:.0%}"
    )
    print(
        f"资产解析 {metrics.assets_parsed}/{metrics.assets_total}，未识别列 {metrics.unmapped_columns}，"
        f"三元组 {metrics.triples} / {metrics.relation_kinds} 类，"
        f"p50 {metrics.p50_ms:.0f}ms p95 {metrics.p95_ms:.0f}ms"
    )
    for item in report.failures:
        print(f"  ! {item.case_id} {item.title}: {'；'.join(item.failures)}")

    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写出 {args.json_path}")

    if args.report_path:
        baseline = None
        if args.baseline and Path(args.baseline).exists():
            baseline = EvalReport.model_validate_json(Path(args.baseline).read_text(encoding="utf-8"))
        Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_path).write_text(to_markdown(report, baseline), encoding="utf-8")
        print(f"已写出 {args.report_path}")

    return 0 if metrics.cases_failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
