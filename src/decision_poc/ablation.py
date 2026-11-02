"""P11 消融阶梯评测：把"改进前后对比"升级成"逐组件归因"。

P8 回答的是"这套系统现在对不对"，P11 回答的是**"是哪一层让它对的"**。参考的是
同类研究常用的消融阶梯（先加感知层、再加单一推理、再加编排、最后加审计），但只借
方法，不借结论与数字：

1. **五档配置**：`rules_only` → `semantic_only` → `single_skill` → `multi_skill`
   → `full_audited`，同一 22 例用例集、同一判据；
2. **同口径指标**：每一档都用同一套指标算，分母写清（谁是 22、谁是 8、谁是 3），
   不让"分母不同"伪装成"能力强弱"；
3. **确定性统计**：本仓库内核是确定性的（同一输入逐位一致），所以这里**不引入任何随机数、
   不伪造独立随机种子**。稳健性用 leave-one-out（逐例剔除后指标怎么变），差异显著性用
   **精确符号翻转检验**（22 例的全部 2^22 种符号组合穷举，p 值精确到 1/2^22）。它度量的是
   "这套用例上差异是否一致"，不是对总体的推断；报告里必须原样写明这一点。

`rules_only` 刻意写成"阈值规则直出"：从 `scene.json` 的工单属性与描述里正则抽深度，
按预案 v2 的 25cm 阈值输出固定动作，不看时点、身份、过期与证据。它代表的是论文里
Rules-only 那一档的能力水平，不是本仓库的历史版本。
"""

from __future__ import annotations

import json
import re
import statistics
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any, Sequence

from pydantic import Field

from .contracts import StrictModel
from .skills import PipelineReport, SkillPipeline, SkillRuntime
from .urban_dataset import DEFAULT_DATASET_ROOT
from .urban_eval import SIX_HOPS, EvalCase, load_cases

SCENE_PATH = DEFAULT_DATASET_ROOT / "scene.json"
ABLATION_SCHEMA = "p11-ablation-2"

# 精确符号翻转检验的规模上限：meet-in-the-middle 复杂度 O(2^(n/2))，n=40 仍是秒级。
# 超过上限就报错而不是偷偷退回随机近似——"用了什么方法"必须能从报告里读出来。
MAX_EXACT_CASES = 40

# 指标名 -> (中文名, 分母口径)。分母写在这里而不是散在代码里，报告直接引用。
METRIC_SPECS: dict[str, tuple[str, str]] = {
    "deliverable_rate": ("可执行率", "全部 22 例里给出非空处置动作的比例"),
    "six_hop_rate": (
        "六跳完整率",
        "全部 22 例里 observed…action 齐全的比例。注意与 P8 的 `six_hop_rate` 口径不同："
        "P8 的分母是 14 例进入推演的用例，这里是全部 22 例，所以 L5 是 64%（14/22）而不是 100%，"
        "差额正好是 8 例应当拒答/拒绝的用例",
    ),
    "conflict_recall": ("冲突识别率", "声明了期望冲突类型的 6 例里，期望类型全部出现的比例"),
    "safety_rate": ("安全处理率", "证据不足/越权 8 例里正确拒答或拒绝的比例"),
    "expiry_filter_rate": ("过期过滤率", "expired 类 4 例里不再暴露已过期预警的比例"),
    "impact_resource_rate": ("范围与资源覆盖率", "全部 22 例里同时给出影响范围与资源核算的比例"),
    "citation_completeness": ("引用完整率", "有审计报告的用例的平均引用完整率（无审计记 0）"),
    "auditable_rate": ("可审计率", "全部 22 例里带可核验审计依据（审计存在且零拒绝）的比例"),
    "publishable_rate": (
        "可直接发布率",
        "全部 22 例里审计通过、无降级、无人审补证即可发布的比例。"
        "注意：高影响动作仍需人工审批，这是设计而非缺陷；这里衡量的是\"结论本身是否已经站得住\"",
    ),
}

FROZEN_METRICS = (
    "six_hop_rate",
    "safety_rate",
    "impact_resource_rate",
    "citation_completeness",
    "auditable_rate",
)

SAFETY_KINDS = ("insufficient", "permission")


class ConfigSpec(StrictModel):
    name: str
    label: str
    stages: list[str] | None = None
    note: str = ""


CONFIGS: tuple[ConfigSpec, ...] = (
    ConfigSpec(
        name="rules_only",
        label="L1 规则基线",
        stages=None,
        note="阈值直出：只看工单深度，不看时点/身份/过期/证据",
    ),
    ConfigSpec(
        name="semantic_only",
        label="L2 语义层",
        stages=["understand", "impact"],
        note="感知：事件理解 + 影响范围，无推演",
    ),
    ConfigSpec(
        name="single_skill",
        label="L3 单一推演技能",
        stages=["understand", "deduce"],
        note="一个推演技能包办六跳，无影响范围/资源/审计",
    ),
    ConfigSpec(
        name="multi_skill",
        label="L4 多技能编排",
        stages=["understand", "impact", "resource", "deduce"],
        note="四技能顺序编排，审计门禁关闭",
    ),
    ConfigSpec(
        name="full_audited",
        label="L5 全量 + 审计门",
        stages=None,
        note="五技能 + 引用闸门 + 哈希链账本（现状）",
    ),
)

CONFIG_NAMES: tuple[str, ...] = tuple(spec.name for spec in CONFIGS)


class CaseRow(StrictModel):
    """一档配置在一条用例上的可核查事实；指标全部从这些布尔值复算。"""

    case_id: str
    kind: str
    config: str
    delivered: bool = False
    six_hops: bool = False
    conflict_expected: bool = False
    conflict_found: bool = False
    safety_expected: bool = False
    safety_ok: bool = False
    expiry_expected: bool = False
    expiry_ok: bool = False
    impact_resource: bool = False
    citation_completeness: float = 0.0
    auditable: bool = False
    publishable: bool = False
    actions: int = 0
    status: str = ""


class ConfigReport(StrictModel):
    config: str
    label: str
    note: str = ""
    cases: int = 0
    metrics: dict[str, float] = Field(default_factory=dict)
    counts: dict[str, list[int]] = Field(default_factory=dict)


class StatBlock(StrictModel):
    """单档单指标的确定性统计：全量值 + 逐例剔除（leave-one-out）范围。

    不用标准差/置信区间：本内核没有随机性，样本量就是分母 n，报"逐例剔除后指标会落在哪"
    比报一个模拟出来的 CI 更诚实，也更容易被当场复算。
    """

    mean: float = 0.0
    loo_min: float = 0.0
    loo_max: float = 0.0
    dropped_case: str = ""
    n: int = 0


class PairwiseTest(StrictModel):
    left: str
    right: str
    metric: str
    delta: float = 0.0
    p_value: float = 1.0


class AblationReport(StrictModel):
    schema_version: str = ABLATION_SCHEMA
    dataset_snapshot: str = ""
    stats_mode: str = "leave-one-out"
    pairwise_mode: str = "exact-sign-flip"
    configs: list[ConfigReport] = Field(default_factory=list)
    stats: dict[str, dict[str, StatBlock]] = Field(default_factory=dict)
    pairwise: list[PairwiseTest] = Field(default_factory=list)
    rows: list[CaseRow] = Field(default_factory=list)

    def config(self, name: str) -> ConfigReport:
        for item in self.configs:
            if item.config == name:
                return item
        raise KeyError(f"unknown config: {name}")

    def fingerprint(self) -> str:
        """正确性指纹：不含任何时间量，跑两遍必须逐位一致。"""
        payload = {
            "rows": [
                [
                    row.case_id,
                    row.config,
                    row.delivered,
                    row.six_hops,
                    row.conflict_found,
                    row.safety_ok,
                    row.expiry_ok,
                    row.impact_resource,
                    row.auditable,
                    round(row.citation_completeness, 6),
                ]
                for row in self.rows
            ],
            "metrics": {item.config: item.metrics for item in self.configs},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# --- L1：阈值规则基线 -----------------------------------------------------


def _load_incidents() -> dict[str, dict[str, Any]]:
    payload = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    return {
        obj["name"]: obj
        for obj in payload["objects"]
        if obj.get("kind") == "incident"
    }


_DEPTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cm|厘米|公分)")
# 预案 v2.0 的两个阈值：≥25cm 封路，≥15cm 抽排（v1.0 写的是 30cm，已废止）
CLOSE_THRESHOLD_CM = 25.0
PUMP_THRESHOLD_CM = 15.0


def parse_depth(description: str) -> float | None:
    """从工单描述里抽积水深度；抽不到就返回 None，不猜。"""
    match = _DEPTH_RE.search(description or "")
    return float(match.group(1)) if match else None


def rules_only_actions(event_id: str, incidents: dict[str, dict[str, Any]]) -> list[str]:
    """阈值直出动作：只读原始工单，不看时点、身份，也不引用任何证据。"""
    incident = incidents.get(event_id)
    if incident is None:
        return []
    depth = parse_depth(str(incident.get("attributes", {}).get("description", "")))
    if depth is None:
        return []
    road = incident.get("attributes", {}).get("road_id") or "事发路段"
    if depth >= CLOSE_THRESHOLD_CM:
        return [f"封路：{road}", "调移动泵抽排", "隔离风险源"]
    if depth >= PUMP_THRESHOLD_CM:
        return ["布设警示并巡查", "调移动泵抽排"]
    return ["巡查"]


# --- 判据 -----------------------------------------------------------------


def _refused(report: PipelineReport) -> bool:
    return bool(
        report.deduction
        and len(report.deduction.stages) == 1
        and report.deduction.stages[0].status == "insufficient"
    )


def _error_codes(report: PipelineReport) -> list[str]:
    return [step.error_code for step in report.steps if step.error_code]


def _safety_ok(case: EvalCase, report: PipelineReport) -> bool:
    """安全处理：期望拒答的必须拒答；期望报错码的必须报同一个码；越权不得可发布。"""
    expect = case.expect
    if expect.refused:
        return _refused(report)
    if expect.error_code:
        return expect.error_code in _error_codes(report)
    if case.kind == "permission":
        return "PERMISSION_DENIED" in _error_codes(report) and not report.publishable
    return not report.publishable


def _conflict_found(case: EvalCase, report: PipelineReport) -> bool:
    want = case.expect.conflict_kinds
    if not want:
        return False
    actual = sorted({item.kind.value for item in report.deduction.conflicts}) if report.deduction else []
    return all(kind in actual for kind in want)


def _expiry_ok(report: PipelineReport) -> bool:
    """过期用例：结论里不再暴露已过期的预警（warning_level 为空）。"""
    return report.facts is not None and report.facts.warning_level is None


def _row_from_report(case: EvalCase, report: PipelineReport, config: str) -> CaseRow:
    stages = [stage.stage for stage in report.deduction.stages] if report.deduction else []
    actions = len(report.deduction.action_order) if report.deduction else 0
    audit = report.audit
    return CaseRow(
        case_id=case.case_id,
        kind=case.kind,
        config=config,
        delivered=actions > 0,
        six_hops=set(SIX_HOPS).issubset(set(stages)),
        conflict_expected=bool(case.expect.conflict_kinds),
        conflict_found=_conflict_found(case, report),
        safety_expected=case.kind in SAFETY_KINDS,
        safety_ok=_safety_ok(case, report) if case.kind in SAFETY_KINDS else False,
        expiry_expected=case.kind == "expired",
        expiry_ok=_expiry_ok(report) if case.kind == "expired" else False,
        impact_resource=bool(
            (report.impact is not None and report.impact.affected) and report.resources is not None
        ),
        citation_completeness=audit.citation_completeness if audit else 0.0,
        auditable=bool(audit is not None and audit.rejected == 0),
        publishable=bool(report.publishable),
        actions=actions,
        status=report.status,
    )


def _row_from_rules(case: EvalCase, incidents: dict[str, dict[str, Any]]) -> CaseRow:
    actions = rules_only_actions(case.event_id, incidents)
    return CaseRow(
        case_id=case.case_id,
        kind=case.kind,
        config="rules_only",
        delivered=bool(actions),
        # 分母必须与其他档一致：期望标记照抄用例，结果一律记否（规则档做不到）
        conflict_expected=bool(case.expect.conflict_kinds),
        safety_expected=case.kind in SAFETY_KINDS,
        expiry_expected=case.kind == "expired",
        actions=len(actions),
        status="ok" if actions else "no_action",
    )


# --- 指标归集 -------------------------------------------------------------


def _subset(rows: Sequence[CaseRow], metric: str) -> list[CaseRow]:
    if metric == "conflict_recall":
        return [row for row in rows if row.conflict_expected]
    if metric == "safety_rate":
        return [row for row in rows if row.safety_expected]
    if metric == "expiry_filter_rate":
        return [row for row in rows if row.expiry_expected]
    return list(rows)


def case_value(row: CaseRow, metric: str) -> float:
    return float(
        {
            "deliverable_rate": row.delivered,
            "six_hop_rate": row.six_hops,
            "conflict_recall": row.conflict_found,
            "safety_rate": row.safety_ok,
            "expiry_filter_rate": row.expiry_ok,
            "impact_resource_rate": row.impact_resource,
            "citation_completeness": row.citation_completeness,
            "auditable_rate": row.auditable,
            "publishable_rate": row.publishable,
        }[metric]
    )


def compute_metrics(rows: Sequence[CaseRow]) -> tuple[dict[str, float], dict[str, list[int]]]:
    metrics: dict[str, float] = {}
    counts: dict[str, list[int]] = {}
    for metric in METRIC_SPECS:
        group = _subset(rows, metric)
        values = [case_value(row, metric) for row in group]
        total = len(values)
        hits = sum(1 for value in values if value >= 0.5) if metric != "citation_completeness" else 0
        if metric == "citation_completeness":
            metrics[metric] = round(statistics.fmean(values), 4) if values else 0.0
            counts[metric] = [sum(1 for value in values if value >= 0.999), total]
        else:
            metrics[metric] = round(hits / total, 4) if total else 0.0
            counts[metric] = [hits, total]
    return metrics, counts


# --- 确定性统计 -----------------------------------------------------------


def _signed_sums(values: Sequence[float]) -> list[float]:
    """返回全部 ±value 组合的和（共 2^n 个）。n 由 MAX_EXACT_CASES 限住。"""
    sums = [0.0]
    for value in values:
        sums = [current + value for current in sums] + [current - value for current in sums]
    return sums


def _exact_sign_flip_p(deltas: Sequence[float]) -> float:
    """精确符号翻转检验（两侧）：零随机，穷举全部 2^n 种符号组合。

    零假设：两档在本用例集上无差异，即每个逐例差值的正负号是等概率的。
    统计量取"符号翻转后差值和"的绝对值，p = |S| ≥ |观测 S| 的组合占比。
    用 meet-in-the-middle 把 2^n 拆成两个 2^(n/2) 和集再二分计数，
    22 例（2^22 = 4,194,304）毫秒级算完，p 值是精确有理数 k/2^n。
    """
    n = len(deltas)
    if n == 0:
        return 1.0
    if n > MAX_EXACT_CASES:
        raise ValueError(
            f"精确符号翻转最多支持 {MAX_EXACT_CASES} 例，当前 {n} 例；"
            "不要偷偷退回随机近似，先把用例设计讲清楚"
        )
    observed = abs(sum(deltas))
    half = n // 2
    left = _signed_sums(deltas[:half])
    right = sorted(_signed_sums(deltas[half:]))
    total = 1 << n
    # 严格落在 (-|observed|, |observed|) 内的组合数，用补集避免边界重叠。
    eps = 1e-12
    inside = 0
    for value in left:
        low = -observed + eps - value
        high = observed - eps - value
        if high <= low:
            continue
        inside += max(0, bisect_left(right, high) - bisect_right(right, low))
    # 不四舍五入：p 是精确有理数 k/2^n，舍入会让"精确"这两个字站不住
    return (total - inside) / total


def _leave_one_out(rows_by_config: dict[str, list[CaseRow]]) -> dict[str, dict[str, StatBlock]]:
    """leave-one-out：每次剔除一个用例重算指标，给出确定性的取值范围。"""
    stats: dict[str, dict[str, StatBlock]] = {}
    for config in CONFIG_NAMES:
        rows = rows_by_config[config]
        per_metric: dict[str, StatBlock] = {}
        for metric in FROZEN_METRICS:
            group = _subset(rows, metric)
            values = [case_value(row, metric) for row in group]
            n = len(values)
            mean = statistics.fmean(values) if values else 0.0
            if n <= 1:
                per_metric[metric] = StatBlock(
                    mean=round(mean, 4), loo_min=round(mean, 4),
                    loo_max=round(mean, 4), n=n,
                )
                continue
            loo: list[float] = []
            dropped_case = ""
            max_shift = -1.0
            for index, row in enumerate(group):
                rest_mean = statistics.fmean(values[:index] + values[index + 1:])
                loo.append(rest_mean)
                shift = abs(rest_mean - mean)
                if shift > max_shift + 1e-12:
                    max_shift, dropped_case = shift, row.case_id
            per_metric[metric] = StatBlock(
                mean=round(mean, 4),
                loo_min=round(min(loo), 4),
                loo_max=round(max(loo), 4),
                dropped_case=dropped_case,
                n=n,
            )
        stats[config] = per_metric
    return stats


def _exact_sign_flip(left: Sequence[float], right: Sequence[float]) -> tuple[float, float]:
    """配对检验：返回 (观测逐例差值均值, 精确两侧 p 值)。零假设是"两档在本用例集上无差异"。"""
    if len(left) != len(right) or not left:
        return 0.0, 1.0
    deltas = [a - b for a, b in zip(left, right)]
    return round(statistics.fmean(deltas), 4), _exact_sign_flip_p(deltas)


def _pairwise(rows_by_config: dict[str, list[CaseRow]]) -> list[PairwiseTest]:
    tests: list[PairwiseTest] = []
    for previous, current in zip(CONFIG_NAMES, CONFIG_NAMES[1:]):
        for metric in FROZEN_METRICS:
            left = _subset(rows_by_config[current], metric)
            right = _subset(rows_by_config[previous], metric)
            if [row.case_id for row in left] != [row.case_id for row in right]:
                continue
            values_left = [case_value(row, metric) for row in left]
            values_right = [case_value(row, metric) for row in right]
            delta, p_value = _exact_sign_flip(values_left, values_right)
            tests.append(
                PairwiseTest(
                    left=previous,
                    right=current,
                    metric=metric,
                    delta=delta,
                    p_value=p_value,
                )
            )
    return tests


# --- 主流程 ---------------------------------------------------------------


def evaluate(
    cases: Sequence[EvalCase] | None = None,
    *,
    runtime: SkillRuntime | None = None,
) -> AblationReport:
    cases = list(cases) if cases is not None else load_cases()
    runtime = runtime or SkillRuntime()
    incidents = _load_incidents()
    rows_by_config: dict[str, list[CaseRow]] = {}
    all_rows: list[CaseRow] = []

    for spec in CONFIGS:
        rows: list[CaseRow] = []
        if spec.name == "rules_only":
            rows = [_row_from_rules(case, incidents) for case in cases]
        else:
            pipeline = SkillPipeline(runtime, stages=spec.stages)
            for case in cases:
                report = pipeline.run(
                    case.event_id,
                    case.as_of,
                    caller_labels=case.caller_labels,
                    persist=False,
                )
                rows.append(_row_from_report(case, report, spec.name))
        rows_by_config[spec.name] = rows
        all_rows.extend(rows)

    configs: list[ConfigReport] = []
    for spec in CONFIGS:
        metrics, counts = compute_metrics(rows_by_config[spec.name])
        configs.append(
            ConfigReport(
                config=spec.name,
                label=spec.label,
                note=spec.note,
                cases=len(rows_by_config[spec.name]),
                metrics=metrics,
                counts=counts,
            )
        )

    return AblationReport(
        dataset_snapshot=runtime.store.snapshot_hash(),
        configs=configs,
        stats=_leave_one_out(rows_by_config),
        pairwise=_pairwise(rows_by_config),
        rows=all_rows,
    )


# --- 输出 -----------------------------------------------------------------


def to_markdown(report: AblationReport) -> str:
    lines = [
        "# P11 消融阶梯评测报告",
        "",
        "由 `python -m decision_poc.ablation --report <path>` 生成，**不要手改**。",
        "",
        f"- 数据集快照：`{report.dataset_snapshot[:16]}`",
        f"- 用例：{report.configs[0].cases if report.configs else 0} 例 × {len(report.configs)} 档配置",
        f"- 统计口径：{report.stats_mode}（逐例剔除）+ {report.pairwise_mode}（全部符号组合穷举，零随机）",
        "",
        "> **口径说明**：本仓库内核是确定性的（同一输入逐位一致），所以这里**不引入任何随机数、",
        "> 也不伪造独立随机种子**：稳健性用 leave-one-out（逐例剔除后指标怎么变），差异显著性用",
        "> **精确符号翻转检验**（穷举全部正负号组合，p 值精确到 1/2^n）。两者都只说明",
        "> \"这套用例上差异是否一致\"，不外推到总体；与纯仿真研究（合成多个独立随机种子）不同，不要混写。",
        "",
        "## 1 五档配置",
        "",
        "| 档 | 配置 | 能力子集 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for spec, item in zip(CONFIGS, report.configs):
        stages = "、".join(spec.stages) if spec.stages else ("阈值直出" if spec.name == "rules_only" else "全部五技能")
        lines.append(f"| {item.label} | `{item.config}` | {stages} | {spec.note} |")

    lines += [
        "",
        "## 2 指标对比（同口径，分母见第 3 节）",
        "",
        "| 指标 | " + " | ".join(item.label for item in report.configs) + " |",
        "| --- | " + " | ".join("---" for _ in report.configs) + " |",
    ]
    for metric, (label, _) in METRIC_SPECS.items():
        cells = []
        for item in report.configs:
            value = item.metrics.get(metric, 0.0)
            hits, total = item.counts.get(metric, [0, 0])
            cells.append(f"{value:.2%}（{hits}/{total}）" if metric != "citation_completeness" else f"{value:.2%}")
        lines.append(f"| {label} `{metric}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 3 指标口径",
        "",
        "| 指标 | 口径 |",
        "| --- | --- |",
    ]
    for metric, (label, spec) in METRIC_SPECS.items():
        lines.append(f"| {label} | {spec} |")

    lines += [
        "",
        "## 4 稳健性：逐例剔除（leave-one-out，确定性）",
        "",
        "每次剔除 1 个用例重算指标，给出该档该指标的取值范围与"
        "\"剔除哪个用例影响最大\"；样本量就是分母 n，不模拟随机抽样。",
        "",
        "| 配置 | 指标 | 全量 | 剔除后最小 | 剔除后最大 | 影响最大用例 | n |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.configs:
        block = report.stats.get(item.config, {})
        for metric in FROZEN_METRICS:
            stat = block.get(metric)
            if stat is None:
                continue
            lines.append(
                f"| {item.label} | {METRIC_SPECS[metric][0]} | {stat.mean:.2%} | "
                f"{stat.loo_min:.2%} | {stat.loo_max:.2%} | {stat.dropped_case or '—'} | {stat.n} |"
            )

    lines += [
        "",
        "## 5 相邻档位精确符号翻转检验",
        "",
        "对每对相邻档位做**精确符号翻转检验**：把逐例差值的正负号全部 2^n 种组合穷举"
        "（n 为该指标分母），p = |S| ≥ 观测 |S| 的比例。p 值是精确有理数 k/2^n，"
        "不是抽样估计；它只说明\"本用例集上两档无差异\"的可能性，不是总体显著性。",
        "",
        "| 对比 | 指标 | Δ | 精确 p |",
        "| --- | --- | --- | --- |",
    ]
    for test in report.pairwise:
        lines.append(
            f"| `{test.left}` → `{test.right}` | {METRIC_SPECS[test.metric][0]} | "
            f"{test.delta:+.2%} | {_format_p(test.p_value)} |"
        )

    lines += [
        "",
        "## 6 结论（只陈述本用例集上成立的差异）",
        "",
    ]
    lines.extend(_conclusions(report))
    lines += [
        "",
        "## 7 与外部研究的表述边界",
        "",
        "- 本报告是**受控评测环境**下的组件归因，不是真实市政部署测量；",
        "- `rules_only` 是阈值规则基线（不含语义层/推演/审计），不代表本仓库的历史版本；",
        "- 统计量**全部确定性**：没有随机种子、没有多次重复实验，p 值与 leave-one-out 范围"
        "换机器重跑逐位一致；不宣称\"多运行重复实验\"；",
        "- 引用外部论文的消融结论时，只借方法（五档阶梯、同口径指标、威胁到有效性自省），",
        "  不引用其效果量——其数字来自合成蒙特卡洛仿真；",
        "- 本仓库不宣称区块链：审计层是 append-only 哈希链日志，见 P4 设计红线。",
    ]
    return "\n".join(lines) + "\n"


def _format_p(p_value: float) -> str:
    """p 值显示：精确到 1/2^n 的小值用科学计数法，不写成虚假的 0.0000。"""
    if p_value >= 1.0 - 1e-9:
        return "1.0000"
    if p_value < 1e-4:
        return f"{p_value:.2e}"
    return f"{p_value:.4f}"


def _conclusions(report: AblationReport) -> list[str]:
    lines: list[str] = []
    names = [item.config for item in report.configs]
    for metric in FROZEN_METRICS:
        values = [item.metrics.get(metric, 0.0) for item in report.configs]
        jumps = [values[index + 1] - values[index] for index in range(len(values) - 1)]
        title = METRIC_SPECS[metric][0]
        if max(jumps) <= 0:
            lines.append(
                f"- **{title}**：{values[0]:.2%} → {values[-1]:.2%}，阶梯上没有出现正增量。"
            )
            continue
        index = jumps.index(max(jumps))
        lines.append(
            f"- **{title}**：{values[0]:.2%} → {values[-1]:.2%}；"
            f"增量集中在 `{names[index]}` → `{names[index + 1]}`（{jumps[index]:+.2%}）。"
        )
    # 末两档的变化面：只动审计相关指标才叫"审计层只贡献可审计性"
    multi, full = report.config("multi_skill"), report.config("full_audited")
    labels = {name: METRIC_SPECS[name][0] for name in FROZEN_METRICS}
    changed = [labels[name] for name in FROZEN_METRICS if full.metrics[name] != multi.metrics[name]]
    unchanged = [labels[name] for name in FROZEN_METRICS if full.metrics[name] == multi.metrics[name]]
    lines.append(
        f"- **末两档之间**（`multi_skill` → `full_audited`）："
        f"{'、'.join(changed) if changed else '无指标'} 变化，"
        f"{'、'.join(unchanged) if unchanged else '无指标'} 逐位不变。"
    )
    lines.append(
        "- 归因只看阶梯上的**增量方向**；某档增量为 0 就写 0，不为了叙事把它包装成\"显著提升\"。"
    )
    return lines


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")

    parser = argparse.ArgumentParser(description="P11 ablation ladder evaluation")
    parser.add_argument("--cases", default=None, help="用例文件（默认 samples/eval/urban_cases.json）")
    parser.add_argument("--json", dest="json_path", default=None, help="写出机器可读结果")
    parser.add_argument("--report", dest="report_path", default=None, help="写出 Markdown 报告")
    parser.add_argument("--check", action="store_true", help="只跑一次并打印指纹（自检用）")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases) if args.cases else load_cases()
    report = evaluate(cases)
    for item in report.configs:
        summary = "，".join(
            f"{METRIC_SPECS[name][0]} {item.metrics[name]:.0%}" for name in FROZEN_METRICS
        )
        print(f"{item.label:<14} {summary}")
    print(f"指纹 {report.fingerprint()[:16]}")

    if args.check:
        again = evaluate(cases)
        if again.fingerprint() != report.fingerprint():
            print("! 两次运行不一致：消融结果不确定")
            return 1
        if [(t.left, t.right, t.metric, t.p_value) for t in again.pairwise] != [
            (t.left, t.right, t.metric, t.p_value) for t in report.pairwise
        ]:
            print("! 两次运行的精确 p 值不一致")
            return 1
        print("两次运行逐位一致（含精确 p 值）")

    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"已写出 {args.json_path}")
    if args.report_path:
        target = Path(args.report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_markdown(report), encoding="utf-8")
        print(f"已写出 {args.report_path}")
    return 0


__all__ = [
    "ABLATION_SCHEMA",
    "CONFIGS",
    "CONFIG_NAMES",
    "FROZEN_METRICS",
    "METRIC_SPECS",
    "AblationReport",
    "CaseRow",
    "ConfigReport",
    "ConfigSpec",
    "PairwiseTest",
    "StatBlock",
    "case_value",
    "compute_metrics",
    "evaluate",
    "parse_depth",
    "rules_only_actions",
    "to_markdown",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
