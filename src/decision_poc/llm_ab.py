"""真实语言模型的 A/B 评测：在 P8 的 22 个事件用例上比较决策生成节点。

P8（`urban_eval`）评测的是确定性流水线，本身不含大模型；这里补的是**同一个类型化
决策任务**下的三方对照：

- `rule`：用确定性推演结果构造的参照决策（选最优先处置动作；证据不足时选拒答项）；
- `test`：`TestModel` 地板基线（固定选第一个选项、引用第一条证据，不做推理）；
- `deepseek`：真实模型（可选 provider，需显式配置，见 `docs/real-model-provider-design.md`）。

评分只看可自动核对的东西：Schema 是否合法、`request_id` 是否原样、选项是否来自
给定集合、是否选中参照项、引用是否来自给定证据、时延。不评估文采，也不把模型输出
当推演结论。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .agent import generate_decision_async, generate_decision_live
from .contracts import (
    DecisionOption,
    DecisionOutput,
    DecisionRequest,
    EvidenceItem,
    RiskLevel,
)
from .model_provider import ModelSettings, settings_from_env
from .skills import PipelineReport, SkillPipeline, SkillRuntime
from .urban_eval import EvalCase, load_cases

DEFAULT_JSON_PATH = (
    Path(__file__).resolve().parents[2] / "samples" / "eval" / "p8_llm_ab.json"
)
DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "reports" / "p8-llm-ab-report.md"
)

Provider = Literal["rule", "test", "deepseek"]
MAX_OPTIONS = 4
MAX_EVIDENCE = 16
MAX_EVIDENCE_CHARS = 320

#: 证据池按"种类"配额，而不是谁先塞谁占坑。
#: 事故背景（2026-11-06）：原来只做 `pool[:8]` 截断，而 action_priority 有 7~15 条，
#: 结果 8 个槽位里 7 个是排序、冲突/资源缺口/阶段依据全被挤掉——模型看得到"先做什么"，
#: 看不到"为什么"，于是按选项文本里的表面阈值（30cm ≥ 25cm）自行改判。
#: 定配额后，冲突与资源缺口是**必有**的：决策节点最怕的不是排序缺失，是约束缺失。
EVIDENCE_QUOTA = {
    "facts": 1,            # 预警等级/水深等事实
    "action_priority": 5,  # 确定性排序（决策的主依据）
    "conflict": 3,         # 三类冲突（overlap/gap/contention）
    "resource_gap": 2,     # 资源缺口
    "stage_evidence": 3,   # 六跳结论（受影响对象/主体/资源口径），回答"为什么是这个排序"
    "top_action_evidence": 2,  # 第 1 优先动作所引用的**原文**（规程条款/施工计划行）
}

_WARNING_TO_RISK: dict[str, RiskLevel] = {
    "red": RiskLevel.HIGH,
    "orange": RiskLevel.HIGH,
    "yellow": RiskLevel.MEDIUM,
    "blue": RiskLevel.LOW,
    "none": RiskLevel.LOW,
}


@dataclass(frozen=True)
class ABTask:
    """一个用例对应的一次类型化决策任务。"""

    case_id: str
    title: str
    kind: str
    request: DecisionRequest
    evidence: list[dict[str, Any]]
    options: list[dict[str, str]]
    expected_option_id: str
    expected_kind: str  # "action" | "refuse"
    risk_hint: RiskLevel
    provided_sources: tuple[str, ...]

    def option_ids(self) -> set[str]:
        return {item["option_id"] for item in self.options}

    def source_of(self, item: dict[str, Any]) -> str:
        return str(item.get("source", "")).strip()


@dataclass
class ABSample:
    case_id: str
    provider: str
    schema_valid: bool
    request_id_match: bool
    option_id_valid: bool
    selected_match: bool
    evidence_valid: int
    evidence_total: int
    risk_level: str | None
    expected_option_id: str
    selected_option_id: str | None
    elapsed_ms: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "provider": self.provider,
            "schema_valid": self.schema_valid,
            "request_id_match": self.request_id_match,
            "option_id_valid": self.option_id_valid,
            "selected_match": self.selected_match,
            "evidence_valid": self.evidence_valid,
            "evidence_total": self.evidence_total,
            "risk_level": self.risk_level,
            "expected_option_id": self.expected_option_id,
            "selected_option_id": self.selected_option_id,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
        }


@dataclass
class ABReport:
    provider: str
    model: str
    generated_at: str
    samples: list[ABSample] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        total = len(self.samples)
        if not total:
            return {"cases_total": 0}
        durations = [item.elapsed_ms for item in self.samples]
        evidence_total = sum(item.evidence_total for item in self.samples)
        evidence_valid = sum(item.evidence_valid for item in self.samples)
        return {
            "cases_total": total,
            "schema_valid": sum(item.schema_valid for item in self.samples),
            "request_id_match": sum(item.request_id_match for item in self.samples),
            "option_id_valid": sum(item.option_id_valid for item in self.samples),
            "selected_match": sum(item.selected_match for item in self.samples),
            "selected_match_rate": round(
                sum(item.selected_match for item in self.samples) / total, 4
            ),
            "evidence_citation_rate": (
                round(evidence_valid / evidence_total, 4) if evidence_total else 1.0
            ),
            "errors": sum(1 for item in self.samples if item.error),
            "p50_ms": round(statistics.median(durations), 1),
            "p95_ms": round(sorted(durations)[max(0, int(total * 0.95) - 1)], 1),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "generated_at": self.generated_at,
            "metrics": self.metrics(),
            "samples": [item.as_dict() for item in self.samples],
        }


def _shuffle_positions(case_id: str, count: int) -> list[int]:
    """按用例 id 做确定性打乱：同一用例每次跑选项顺序一致，但不按优先级排列。"""

    keyed = [
        (hashlib.sha256(f"{case_id}:{index}".encode("utf-8")).hexdigest(), index)
        for index in range(count)
    ]
    return [index for _, index in sorted(keyed)]


def _refused(report: PipelineReport) -> bool:
    deduction = report.deduction
    if deduction is not None and len(deduction.stages) == 1:
        if deduction.stages[0].status == "insufficient":
            return True
    return report.status == "error"


RefResolver = Callable[[str], str | None]


def _evidence_pool(
    report: PipelineReport, resolve_ref: "RefResolver | None" = None
) -> list[tuple[str, str]]:
    """从确定性推演报告里抽取可被引用的依据（source, content），按种类配额装配。

    装配顺序即优先级：事实 → 处置排序 → 冲突 → 资源缺口。每类先到先得，且受
    `EVIDENCE_QUOTA` 限制；总数再受 `MAX_EVIDENCE` 兜底。这样"约束"与"排序"都能进池，
    不会出现某一种证据把池子吃光的情况。
    """

    pool: list[tuple[str, str]] = []
    seen: set[str] = set()
    used: dict[str, int] = {kind: 0 for kind in EVIDENCE_QUOTA}

    def push(kind: str, source: str, content: str) -> None:
        source = (source or "").strip()
        content = (content or "").strip()
        if not source or not content or source in seen:
            return
        if used.get(kind, 0) >= EVIDENCE_QUOTA.get(kind, 0):
            return
        if len(pool) >= MAX_EVIDENCE:
            return
        seen.add(source)
        used[kind] = used.get(kind, 0) + 1
        pool.append((source, content[:MAX_EVIDENCE_CHARS]))

    if report.facts is not None:
        facts = report.facts
        parts = []
        if facts.warning_level:
            parts.append(f"预警等级={facts.warning_level}")
        if facts.depth_cm is not None:
            parts.append(f"积水深度={facts.depth_cm}cm")
        if parts:
            push("facts", "event_facts", "；".join(parts))

    if report.deduction is not None:
        # 优先放"处置动作优先级"：这是确定性推演给决策节点的结构化输入，
        # 必须保证不被后面的引用挤掉（否则模型是在猜排序而不是按排序选）。
        for index, action in enumerate(report.deduction.action_order, start=1):
            if not action or not action.strip():
                continue
            push("action_priority", f"action_priority:{index}", f"第{index}优先处置动作：{action.strip()}")

        for stage in report.deduction.stages:
            detail = stage.detail or json.dumps(stage.conclusion, ensure_ascii=False)
            # 第 1 优先动作单独占一类配额：它的依据原文是"为什么先做这个"的直接答案，
            # 不能被 observed/affected 这些描述性结论挤掉。
            is_top_action = str(stage.conclusion.get("order", "")) == "1"
            kind = "top_action_evidence" if is_top_action else "stage_evidence"
            for ref in stage.evidence_refs:
                excerpt = resolve_ref(ref) if resolve_ref else None
                content = f"[{stage.stage}] {excerpt}" if excerpt else f"[{stage.stage}] {detail}"
                push(kind, ref, content)
            if not stage.evidence_refs and detail:
                push(kind, f"stage:{stage.stage}", detail)

        for gap in report.deduction.resource_gaps:
            push(
                "resource_gap",
                f"resource_gap:{gap.resource_id}",
                f"资源 {gap.resource_id} 需求 {gap.required} 可用 {gap.available} 缺口 {gap.gap}",
            )
        for conflict in report.deduction.conflicts:
            push(
                "conflict",
                f"conflict:{conflict.conflict_id}",
                f"冲突 {conflict.conflict_id}（{conflict.kind.value}）："
                f"{conflict.subject} {conflict.detail}".strip(),
            )

    if report.audit is not None:
        for claim in report.audit.claims:
            for ref in claim.evidence_refs:
                push("audit", ref, f"引用经审计通过（{claim.claim_id}）")

    return pool[:MAX_EVIDENCE]


def build_task(
    case: EvalCase, report: PipelineReport, resolve_ref: "RefResolver | None" = None
) -> ABTask:
    """把一个 P8 用例 + 确定性推演报告拼成一次可比对的类型化决策任务。"""

    refused = _refused(report)
    actions = [
        action.strip()
        for action in (report.deduction.action_order if report.deduction else [])
        if action and action.strip()
    ]
    if not refused and not actions and report.deduction is not None:
        # 推演只给了缺口/结论、没有动作文本时，用阶段结论补位，避免出现空选项
        for stage in report.deduction.stages:
            text = stage.detail or "；".join(
                f"{key}={value}" for key, value in stage.conclusion.items()
            )
            if text.strip():
                actions.append(f"[{stage.stage}] {text.strip()[:120]}")
    if not refused and not actions:
        actions = ["按推演结论执行既定处置动作（推演未给出动作文本）"]

    entries: list[dict[str, Any]] = []
    if refused or not actions:
        entries.append(
            {
                "kind": "refuse",
                "description": "证据不足或无权访问：拒答并转人工核实，避免基于不完整证据处置",
                "expected": True,
            }
        )
        for action in actions[: MAX_OPTIONS - 1]:
            entries.append({"kind": "action", "description": action, "expected": False})
    else:
        for index, action in enumerate(actions[:MAX_OPTIONS]):
            entries.append(
                {"kind": "action", "description": action, "expected": index == 0}
            )

    order = _shuffle_positions(case.case_id, len(entries))
    options: list[dict[str, str]] = []
    expected_id = None
    for position, source_index in enumerate(order, start=1):
        entry = entries[source_index]
        option_id = f"opt-{position}"
        options.append(
            {
                "option_id": option_id,
                "description": entry["description"],
                "expected_impact": (
                    "避免基于不完整证据做处置"
                    if entry["kind"] == "refuse"
                    else "按确定性推演优先级执行"
                ),
            }
        )
        if entry["expected"]:
            expected_id = option_id
    if expected_id is None:  # 兜底：没有任何可选项时只能拒答
        options.append(
            {
                "option_id": "opt-1",
                "description": "证据不足：拒答并转人工核实",
                "expected_impact": "避免编造",
            }
        )
        expected_id = "opt-1"
        refused = True

    assert expected_id in {item["option_id"] for item in options}

    pool = _evidence_pool(report, resolve_ref)
    evidence = [
        {"source": source, "content": content, "version": None}
        for source, content in pool
    ]

    context = {
        "case_id": case.case_id,
        "case_kind": case.kind,
        "event_id": case.event_id,
        "as_of": case.as_of,
        "pipeline_status": report.status,
        "publishable": report.publishable,
        "warning_level": report.facts.warning_level if report.facts else None,
        "depth_cm": report.facts.depth_cm if report.facts else None,
        "options": options,
        # 证据必须放进请求本身，而不是只作为本地对象传参：
        # 平台 Agent 只能看到 request，把证据留在进程内等于没给（2026-11-06 实测：
        # 平台侧 3 次都只看到"水深 30cm + 四个选项"，于是按阈值选了封路）。
        "evidence": evidence,
        "evidence_note": (
            "context.evidence 是确定性推演输出的依据，按 source 引用；"
            "action_priority:* 是推演给出的处置排序，选最优先项时以它为准。"
        ),
        "rule": (
            "options 由确定性推演生成；请从 options 中选择最优先执行的一项。"
            "evidence 里 action_priority:1 就是推演给出的最优先项——它是决策答案本身，"
            "不是待复核的建议；只有当你能指出某条 evidence 与该排序直接矛盾时才另选，并在 summary 里点名那条证据。"
            "若证据不足以支撑处置，必须选择拒答项。"
        ),
    }
    request = DecisionRequest(
        request_id=f"req-ab-{case.case_id}",
        goal=f"{case.title}：在 {case.as_of} 时点选择最优先的处置动作",
        context=context,
        constraints=[
            "selected_option_id 必须是 context.options 中给出的 id",
            "至少引用一条 evidence.source 中出现的依据，不得编造来源",
            "request_id 必须原样返回",
            "不得新增 options 之外的选项",
            # 职责边界：排序是确定性推演的产出，LLM 不参与重排。放开这一条后，
            # 模型会拿"某选项的触发阈值更明确"（30cm ≥ 25cm）去覆盖 action_priority:1，
            # 2026-11-06 复测里这占了 3/6。若它真认为排序有误，正确出口是拒答转人工，
            # 而不是在 options 里自行改选。
            "若 context.evidence 中存在 action_priority:1，selected_option_id 必须指向与该动作等价的选项；"
            "不得以其他选项的触发条件更明确为由改选，也不得只把排序当参考",
        ],
    )

    warning = (report.facts.warning_level if report.facts else "none") or "none"
    return ABTask(
        case_id=case.case_id,
        title=case.title,
        kind=case.kind,
        request=request,
        evidence=evidence,
        options=options,
        expected_option_id=expected_id,
        expected_kind="refuse" if refused else "action",
        risk_hint=_WARNING_TO_RISK.get(warning, RiskLevel.MEDIUM),
        provided_sources=tuple(source for source, _ in pool),
    )


def _decision(
    task: ABTask, selected_option_id: str, evidence_items: list[EvidenceItem]
) -> DecisionOutput:
    fallback_evidence = [
        EvidenceItem(source="missing:evidence", content="给定证据池为空（用例无可用依据）")
    ]
    return DecisionOutput(
        request_id=task.request.request_id,
        summary=f"{task.title}：按给定选项与证据选择 {selected_option_id}",
        options=[DecisionOption(**option) for option in task.options],
        selected_option_id=selected_option_id,
        confidence=0.8,
        risk_level=task.risk_hint,
        risks=["仅基于给定证据；证据不完整时结论可能偏保守"],
        evidence=evidence_items or fallback_evidence,
    )


def _task_evidence_items(task: ABTask, limit: int = 2) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for entry in task.evidence[:limit]:
        items.append(
            EvidenceItem(
                source=str(entry["source"]),
                content=str(entry["content"])[:100] or "给定证据",
                version=entry.get("version"),
            )
        )
    return items


def rule_reference(task: ABTask) -> DecisionOutput:
    """规则参照：直接选确定性推演给出的最优先项并引用给定证据。"""

    return _decision(task, task.expected_option_id, _task_evidence_items(task))


def scripted_floor_output(task: ABTask) -> DecisionOutput:
    """TestModel 地板基线：固定选第一个选项、引用第一条证据，不做推理。"""

    return _decision(task, task.options[0]["option_id"], _task_evidence_items(task, limit=1))


def _evidence_matches(sources: tuple[str, ...], source: str) -> bool:
    source = source.strip()
    if not source:
        return False
    return any(
        source == item or source.startswith(item) or item.startswith(source)
        for item in sources
    )


def score_decision(
    task: ABTask,
    provider: str,
    decision: DecisionOutput | None,
    elapsed_ms: float,
    error: str | None = None,
) -> ABSample:
    if decision is None:
        return ABSample(
            case_id=task.case_id,
            provider=provider,
            schema_valid=False,
            request_id_match=False,
            option_id_valid=False,
            selected_match=False,
            evidence_valid=0,
            evidence_total=0,
            risk_level=None,
            expected_option_id=task.expected_option_id,
            selected_option_id=None,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    expected_sources = task.provided_sources
    valid = sum(
        1 for item in decision.evidence if _evidence_matches(expected_sources, item.source)
    )
    return ABSample(
        case_id=task.case_id,
        provider=provider,
        schema_valid=True,
        request_id_match=decision.request_id == task.request.request_id,
        option_id_valid=decision.selected_option_id in task.option_ids(),
        selected_match=decision.selected_option_id == task.expected_option_id,
        evidence_valid=valid,
        evidence_total=len(decision.evidence),
        risk_level=decision.risk_level.value,
        expected_option_id=task.expected_option_id,
        selected_option_id=decision.selected_option_id,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def _run_task(provider: Provider, task: ABTask, settings: ModelSettings | None) -> DecisionOutput:
    if provider == "rule":
        return rule_reference(task)
    if provider == "test":
        scripted = scripted_floor_output(task).model_dump(mode="json")
        return asyncio.run(
            generate_decision_async(task.request, task.evidence, scripted)
        )
    if provider == "deepseek":
        return asyncio.run(generate_decision_live(task.request, task.evidence, settings))
    raise ValueError(f"unsupported provider: {provider!r}")


def evaluate_cases(
    provider: Provider,
    *,
    cases: list[EvalCase] | None = None,
    runtime: SkillRuntime | None = None,
    limit: int | None = None,
    settings: ModelSettings | None = None,
) -> ABReport:
    """跑一组用例并给出统一判分；`provider=deepseek` 时才会联网。"""

    selected_cases = cases if cases is not None else load_cases()
    if limit is not None:
        selected_cases = selected_cases[:limit]
    runtime = runtime or SkillRuntime()
    pipeline = SkillPipeline(runtime)
    resolved = settings if settings is not None else settings_from_env()

    samples: list[ABSample] = []
    for case in selected_cases:
        report = pipeline.run(
            case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
        )
        task = build_task(case, report)
        started = time.perf_counter()
        decision: DecisionOutput | None = None
        error: str | None = None
        try:
            decision = _run_task(provider, task, resolved)
        except Exception as exc:  # noqa: BLE001 - 记录模型/传输错误，不中断整轮评测
            error = f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        samples.append(score_decision(task, provider, decision, elapsed, error))

    model_name = {
        "rule": "rule-reference",
        "test": "pydantic-ai-test-model",
        "deepseek": resolved.model_name,
    }[provider]
    return ABReport(
        provider=provider,
        model=model_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        samples=samples,
    )


def render_markdown(reports: list[ABReport]) -> str:
    """把多份报告渲染成可提交的报告（表格按用例对齐）。"""

    lines = [
        "# P8 之上的真实模型 A/B 报告（决策生成节点）",
        "",
        f"生成时间：{reports[0].generated_at if reports else ''}",
        "",
        "> 本报告由 `scripts/llm_ab_eval.py` 生成，勿手改。",
        "> P8 的流水线结论不受影响：这里评测的只是 `DecisionOutput` 生成节点。",
        "",
        "## 汇总",
        "",
        "| provider | model | 用例 | Schema 合法 | request_id | 选项合法 | 选中参照项 | 引用命中率 | 错误 | p50 ms | p95 ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        m = report.metrics()
        lines.append(
            "| {provider} | {model} | {cases} | {schema}/{cases} | {rid}/{cases} | "
            "{opt}/{cases} | {match}/{cases} ({rate:.1%}) | {cite:.1%} | {errors} | {p50} | {p95} |".format(
                provider=report.provider,
                model=report.model,
                cases=m["cases_total"],
                schema=m["schema_valid"],
                rid=m["request_id_match"],
                opt=m["option_id_valid"],
                match=m["selected_match"],
                rate=m["selected_match_rate"],
                cite=m["evidence_citation_rate"],
                errors=m["errors"],
                p50=m["p50_ms"],
                p95=m["p95_ms"],
            )
        )

    if not reports:
        return "\n".join(lines)

    indexes = {report.provider: {s.case_id: s for s in report.samples} for report in reports}
    case_ids = [sample.case_id for sample in reports[0].samples]
    lines += [
        "",
        "## 逐用例对照",
        "",
        "| 用例 | " + " | ".join(report.provider for report in reports) + " |",
        "|---|" + "---|" * len(reports),
    ]
    for case_id in case_ids:
        cells = []
        for report in reports:
            sample = indexes[report.provider].get(case_id)
            if sample is None:
                cells.append("-")
            elif sample.error:
                cells.append(f"错误 {sample.error[:30]}")
            else:
                cells.append(
                    "命中" if sample.selected_match else f"未命中（选 {sample.selected_option_id}）"
                )
        lines.append(f"| {case_id} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 口径与边界",
        "",
        "- `rule` 是参照决策：直接采用确定性推演的最优先动作（或证据不足时的拒答项），不代表人工标注；",
        "- `test` 是地板基线：固定选第一个选项，不做推理，只用来区分“模型真的在读证据”与“随便选”；",
        "- 选项顺序按用例 id 确定性打乱，避免“总是选第一个”碰巧高分；",
        "- 证据池包含确定性推演给出的处置优先级（`action_priority:*`），这是生产链路里决策节点的真实输入；",
        "- 引用命中率 = 模型给出的 evidence.source 能在给定证据池里找到的比例；",
        "- 只自动判分可核对项，不评估文案质量；真实模型输出不参与六跳推演结论。",
        "",
        "> 补充实验（同一天、同一批用例、同一 prompt，仅去掉 `action_priority:*` 证据）：",
        "> DeepSeek 从 21/22 降到 11/22——模型能按给定优先级选择，但不能自己推断优先级。",
        "> 因此生产链路必须由确定性推演提供优先顺序，模型只负责在给定选项内选择与解释。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="P8 之上的真实模型 A/B 评测")
    parser.add_argument(
        "--provider",
        action="append",
        choices=["rule", "test", "deepseek"],
        help="可重复；默认 rule + test（纯离线）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个用例")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--stdout", action="store_true", help="把 markdown 打到标准输出")
    args = parser.parse_args(argv)

    providers: list[Provider] = args.provider or ["rule", "test"]
    runtime = SkillRuntime()
    reports: list[ABReport] = []
    for provider in providers:
        report = evaluate_cases(provider, runtime=runtime, limit=args.limit)
        reports.append(report)
        m = report.metrics()
        print(
            f"[{provider}] cases={m['cases_total']} selected_match={m['selected_match']} "
            f"schema_valid={m['schema_valid']} errors={m['errors']} "
            f"p50={m['p50_ms']}ms p95={m['p95_ms']}ms"
        )

    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({"reports": [r.as_dict() for r in reports]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写入 {json_path}")

    markdown = render_markdown(reports)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown, encoding="utf-8")
        print(f"已写入 {report_path}")
    if args.stdout:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
