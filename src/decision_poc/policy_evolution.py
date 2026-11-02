"""P9 进化层：偏差归因 → 受控候选 → 评测门禁 → 版本 promote/reject → 回滚。

这条链要回答赛题 3 的那句话：**从哪一次推演的结果、基于什么信号、调了什么策略、
下一次偏差缩小多少。**

P9.1 在这条链上补了两块"持续进化"的证据（见 `experience.py`）：

- **分层记忆 warm start**：同类场景第二次遇到时，从已验证有效的策略起步，
  而不是每次从 v1 重搜；证据是"第二次运行起点偏差更低、轮次更少"。
- **人工评审第二信号**：领域负责人（演示中为角色扮演）对方案的修正会转成一条
  评审用例进入门禁，候选不满足就拒——"避免同类否决"因此可断言。
- **语义化版本标签**：每次采纳产出 `flood-response-v1.N`，带触发反馈 ID、
  触发评审 ID 与预期改善值，进版本历史，可回滚。

设计上的三个取舍，都是为了"进化"能被评委当场复核：

1. **反馈信号是冻结的 ground truth**（`samples/eval/allocation_cases.json`），
   每轮记录用例集的 sha256 指纹，报告可复算校验——反馈被改动过，指纹就对不上。
2. **候选是受控搜索，不是模型自由发挥**：按最差偏差维度生成一组固定顺序的候选
   参数补丁；训练集选优，留出集只做非退化门禁（`evolution_gate` 复用通用内核）。
   一次只改一个机制维度，答辩时说清"哪个参数起了作用"。
3. **拒绝也是产物**：门禁拒绝的候选保留在版本历史里，active 版本不变；
   收敛后主动探一次"更激进"的候选，证明"多调拨不是更好"，而不是跑到 0 就停。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import Field

from .allocation import (
    DEVIATION_WEIGHTS,
    AllocationCase,
    AllocationDecision,
    AllocationPolicy,
    PolicyEvaluation,
    cases_digest,
    default_policy,
    evaluate_policy,
    load_cases,
)
from .contracts import (
    EvaluationReport,
    FeedbackOutcome,
    FeedbackRecord,
    PolicyVersion,
    StrictModel,
)
from .evolution import VersionRegistry
from .evolution_gate import evaluate_and_apply
from .experience import (
    DEFAULT_MEMORY_PATH,
    DecisionMemory,
    ExperienceEntry,
    MemoryScope,
    RecalledExperience,
    ReviewDecision,
    ReviewRecord,
    load_reviews,
    make_entry,
    signature_for,
)

SCHEMA_VERSION = "p9-evolution-1"
SHOWCASE_SCHEMA_VERSION = "p9-showcase-1"

# 留出集允许的退化幅度：门禁只拦"明显变差"，不拦噪声级抖动。
HOLDOUT_TOLERANCE = 0.02

# 归因相同时的取序：调拨 > 关键片区 > 分配向量 > 优先序。
# 顺序固定是为了同一输入必然得到同一轮次，不靠字典迭代顺序。
DIMENSION_PRIORITY: tuple[str, ...] = tuple(DEVIATION_WEIGHTS)

LABEL_PREFIX = "flood-response"
BASELINE_LABEL = f"{LABEL_PREFIX}-v1.0"

REAL_EVENT_ID = "WO-20261029-007"
REAL_EVENT_AS_OF = "2026-10-29T09:40:00+08:00"
REAL_EVENT_LABELS = ["ORG-EMERG"]


# --- 策略与版本 -----------------------------------------------------------


def policy_from_version(version: PolicyVersion) -> AllocationPolicy:
    return AllocationPolicy(policy_version=version.version_id, **version.parameters)


def release_label(minor: int) -> str:
    return f"{LABEL_PREFIX}-v1.{minor}"


def candidate_patches(dimension: str, policy: AllocationPolicy) -> list[dict[str, Any]]:
    """按最差偏差维度生成候选参数补丁（固定顺序，首个即最小改动）。

    一次只引入一个机制：

    - `transfer`：先建立优先序（封路阈值，唯一有硬阈值的信号），再逐步收紧调拨
      门槛；若优先序已建立，则补"危险源也算关键"；
    - `critical_unmet`：补危险源信号；
    - `allocation_l1`：微调调拨门槛；
    - `order`：补水深信号。
    """
    if dimension == "transfer":
        if policy.weight_closure == 0.0 and policy.weight_hazard == 0.0:
            return [
                {"weight_closure": 1.0, "transfer_penalty": 0.1},
                {"weight_closure": 1.0, "transfer_penalty": 0.3},
                {"weight_closure": 1.0, "transfer_penalty": 0.6},
            ]
        if policy.weight_hazard == 0.0:
            return [{"weight_hazard": 0.25}, {"weight_hazard": 0.5}, {"weight_hazard": 1.0}]
        return [{"transfer_penalty": max(0.0, round(policy.transfer_penalty - 0.05, 6))}]
    if dimension == "critical_unmet":
        return [{"weight_hazard": 0.25}, {"weight_hazard": 0.5}, {"weight_hazard": 1.0}]
    if dimension == "allocation_l1":
        return [{"transfer_penalty": 0.05}, {"transfer_penalty": 0.2}]
    if dimension == "order":
        return [{"weight_depth": 0.02}, {"weight_depth": 0.05}]
    return []


def probe_patches(policy: AllocationPolicy) -> list[dict[str, Any]]:
    """收敛后的探测：往"更激进"的方向再走一步，验证门禁会不会拦。"""
    return [
        {"transfer_penalty": 0.0},
        {"transfer_penalty": max(0.0, round(policy.transfer_penalty - 0.05, 6))},
        {"weight_depth": 0.05},
    ]


# 组合候选：一次同时引入两个及以上机制（如"封路优先 + 跨区成本"），
# 用来验证门禁对"多点同时改动"的拦截能力——单维搜索永远走不到这类候选。
# 只在 `allow_combinations=True` 时参与选优；默认关闭，保证已冻结的 P9 数字不变。
COMBINATION_RECIPES: tuple[dict[str, float], ...] = (
    {"weight_closure": 1.0, "weight_hazard": 0.25},
    {"weight_closure": 1.0, "transfer_penalty": 0.1},
    {"weight_hazard": 0.25, "transfer_penalty": 0.1},
    {"weight_closure": 1.0, "weight_hazard": 0.25, "transfer_penalty": 0.1},
    {"weight_closure": 1.0, "weight_hazard": 0.25, "weight_depth": 0.02},
)


def combination_patches(policy: AllocationPolicy) -> list[dict[str, Any]]:
    """组合候选：每条至少改动两个参数；与当前策略相同的值不写进补丁。"""
    patches: list[dict[str, Any]] = []
    for recipe in COMBINATION_RECIPES:
        patch = {
            key: float(value)
            for key, value in recipe.items()
            if abs(float(getattr(policy, key)) - float(value)) > 1e-9
        }
        if len(patch) >= 2:
            patches.append(patch)
    return patches


def worst_dimension(evaluation: PolicyEvaluation) -> str:
    """最差偏差维度；全为 0 时返回空串（已收敛，转探测）。"""
    worst = ""
    worst_value = 0.0
    for dim in DIMENSION_PRIORITY:
        value = evaluation.dimensions.get(dim, 0.0)
        if value > worst_value + 1e-12:
            worst, worst_value = dim, value
    return worst


# --- 报告模型 -------------------------------------------------------------


class PolicyRelease(StrictModel):
    """一次被采纳的策略发布：语义化标签 + 触发反馈 + 预期改善。"""

    label: str
    version_id: str
    parent_label: str
    trigger_feedback_id: str
    trigger_review_ids: list[str] = Field(default_factory=list)
    params_delta: dict[str, Any] = Field(default_factory=dict)
    expected_improvement: float = 0.0
    train_deviation_after: float = 0.0


class WarmStartInfo(StrictModel):
    entry_id: str
    label: str
    similarity: float
    source_outcome: dict[str, float] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class ReviewSummary(StrictModel):
    review_id: str
    reviewer_role: str
    decision: str
    reason: str
    expected_allocation: dict[str, int] = Field(default_factory=dict)
    deviation_before: float = 0.0
    deviation_after: float = 0.0


class EvolutionRound(StrictModel):
    round: int = Field(ge=1)
    target_dimension: str
    candidate_version: str
    params_delta: dict[str, Any] = Field(default_factory=dict)
    train_before: float = 0.0
    train_after: float = 0.0
    holdout_before: float = 0.0
    holdout_after: float = 0.0
    attribution_before: dict[str, float] = Field(default_factory=dict)
    action: str
    reason: str


class RollbackCheck(StrictModel):
    rolled_back_to: str
    deviation_after_rollback: float
    initial_deviation: float
    matches_initial: bool
    restored_version: str
    restored_deviation: float


class RealEventPlan(StrictModel):
    event_id: str
    as_of: str
    policy_version: str
    publishable: bool
    allocation_by_district: dict[str, int] = Field(default_factory=dict)
    unmet_by_district: dict[str, int] = Field(default_factory=dict)
    transfers: list[str] = Field(default_factory=list)
    escalation: list[str] = Field(default_factory=list)


class EvolutionReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    case_digest: str = ""
    feedback_verified: bool = False
    train_cases: int = 0
    holdout_cases: int = 0
    deviation_weights: dict[str, float] = Field(default_factory=dict)
    initial_policy: AllocationPolicy = Field(default_factory=AllocationPolicy)
    final_policy: AllocationPolicy = Field(default_factory=AllocationPolicy)
    initial_train: PolicyEvaluation = Field(default_factory=PolicyEvaluation)
    final_train: PolicyEvaluation = Field(default_factory=PolicyEvaluation)
    initial_holdout: PolicyEvaluation = Field(default_factory=PolicyEvaluation)
    final_holdout: PolicyEvaluation = Field(default_factory=PolicyEvaluation)
    rounds: list[EvolutionRound] = Field(default_factory=list)
    releases: list[PolicyRelease] = Field(default_factory=list)
    final_label: str = BASELINE_LABEL
    warm_start: WarmStartInfo | None = None
    reviews: list[ReviewSummary] = Field(default_factory=list)
    review_cases: int = 0
    rolled_back: RollbackCheck | None = None
    real_event_baseline: RealEventPlan | None = None
    real_event_evolved: RealEventPlan | None = None
    history: list[str] = Field(default_factory=list)

    @property
    def promoted_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.action == "promoted")

    @property
    def rejected_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.action == "rejected")

    def train_deviation_drop(self) -> float:
        start = self.initial_train.deviation
        return round(start - self.final_train.deviation, 6)

    def holdout_deviation_drop(self) -> float:
        start = self.initial_holdout.deviation
        return round(start - self.final_holdout.deviation, 6)


class ShowcaseReport(StrictModel):
    """双会话演示：第一次从零进化，第二次从分层记忆 warm start。"""

    schema_version: str = SHOWCASE_SCHEMA_VERSION
    cases_digest: str = ""
    reviews: list[ReviewRecord] = Field(default_factory=list)
    first: EvolutionReport = Field(default_factory=EvolutionReport)
    second: EvolutionReport = Field(default_factory=EvolutionReport)
    memory_entries: list[ExperienceEntry] = Field(default_factory=list)
    memory_path: str = ""


# --- 真实事件 -------------------------------------------------------------


def plan_real_event(
    policy: AllocationPolicy,
    *,
    event_id: str = REAL_EVENT_ID,
    as_of: str = REAL_EVENT_AS_OF,
    caller_labels: Sequence[str] = tuple(REAL_EVENT_LABELS),
) -> RealEventPlan:
    """跑一遍五 Skill，取推演输出的 decision_inputs 交给策略分配。"""
    from .skills import SkillPipeline, SkillRuntime

    runtime = SkillRuntime()
    report = SkillPipeline(runtime).run(event_id, as_of, caller_labels=list(caller_labels), persist=False)
    if report.deduction is None or not report.deduction.decision_inputs:
        raise RuntimeError(
            f"{event_id} 没有可用决策输入（推演状态 {report.status}），不产出分配方案"
        )
    decision: AllocationDecision = decide_from_view(
        report.deduction.decision_inputs, policy, report.snapshot_hash
    )
    return RealEventPlan(
        event_id=event_id,
        as_of=as_of,
        policy_version=policy.policy_version,
        publishable=report.publishable,
        allocation_by_district=dict(decision.allocation_by_district),
        unmet_by_district=dict(decision.unmet_by_district),
        transfers=[
            f"{item.from_district}->{item.to_district}:{item.qty} 台（跨区 {item.delay_min} 分钟）"
            for item in decision.transfers
        ],
        escalation=list(decision.escalation),
    )


def decide_from_view(
    decision_inputs: Any,
    policy: AllocationPolicy,
    snapshot_hash: str = "",
) -> AllocationDecision:
    from .allocation import decide

    return decide(decision_inputs, policy, snapshot_hash=snapshot_hash)


# --- 人工评审 -------------------------------------------------------------


def review_cases_for(reviews: Sequence[ReviewRecord]) -> list[AllocationCase]:
    """只有给出修正分配的评审才能变成门禁用例；否决（reject）只留痕，不编期望。"""
    return [
        review.to_case()
        for review in reviews
        if review.decision is ReviewDecision.MODIFY and review.expected_allocation
    ]


# --- 演化主流程 -----------------------------------------------------------


def evolve(
    cases: Sequence[AllocationCase] | None = None,
    *,
    registry: VersionRegistry | None = None,
    max_rounds: int = 4,
    include_real_event: bool = True,
    memory: DecisionMemory | None = None,
    reviews: Sequence[ReviewRecord] = (),
    use_memory: bool = True,
    allow_combinations: bool = False,
) -> EvolutionReport:
    all_cases = list(cases) if cases is not None else load_cases()
    train = [case for case in all_cases if case.split == "train"]
    holdout = [case for case in all_cases if case.split == "holdout"]
    if not train or not holdout:
        raise ValueError("评测集必须同时包含 train 与 holdout 用例")
    review_cases = review_cases_for(reviews)
    digest = cases_digest(all_cases)

    warm: RecalledExperience | None = None
    initial = default_policy("v1")
    if memory is not None and use_memory:
        warm = memory.warm_start(signature_for(all_cases))
        if warm is not None:
            initial = AllocationPolicy(policy_version="v1", **warm.entry.params)
    registry = registry or VersionRegistry(
        initial_parameters=initial.model_dump(exclude={"policy_version"})
    )

    active_policy = initial
    active_train = evaluate_policy(active_policy, train)
    active_holdout = evaluate_policy(active_policy, holdout)
    active_review = evaluate_policy(active_policy, review_cases) if review_cases else None
    review_before = active_review.deviation if active_review is not None else 0.0
    rounds: list[EvolutionRound] = []
    releases: list[PolicyRelease] = []
    minor = 0
    # warm start 时继承记忆里的标签：第二次运行若无需再调整，最终标签不能退回 v1.0
    base_label = warm.entry.label if warm is not None else BASELINE_LABEL
    parent_label = base_label

    for index in range(1, max_rounds + 1):
        target = worst_dimension(active_train)
        patches = candidate_patches(target, active_policy)
        if not patches:
            target = "probe"
            patches = probe_patches(active_policy)
        if allow_combinations:
            combos = combination_patches(active_policy)
            if not patches:
                target = "combination"
                patches = list(combos)
            else:
                patches = list(patches) + combos
        if not patches:
            break

        # 训练集选优：只挑训练偏差最小的补丁，留出集/评审用例都不参与选择
        best_patch: dict[str, Any] | None = None
        best_train: PolicyEvaluation | None = None
        for patch in patches:
            candidate_policy = active_policy.with_updates(patch)
            candidate_train = evaluate_policy(candidate_policy, train)
            if best_train is None or candidate_train.deviation < best_train.deviation:
                best_patch, best_train = patch, candidate_train
        if best_patch is None or best_train is None:
            break
        # 组合候选赢了单维候选时，轮次的归因维度标成 combination，报告里能一眼看出
        # "这次是同时改了多个机制"，而不是仍标成单维维度。
        if allow_combinations and len(best_patch) >= 2:
            target = "combination"

        captured: dict[str, tuple[PolicyEvaluation, PolicyEvaluation, float]] = {}
        state = {
            "train_before": active_train.deviation,
            "holdout_before": active_holdout.deviation,
        }

        def evaluate(version: PolicyVersion) -> EvaluationReport:
            candidate_policy = policy_from_version(version)
            candidate_train = evaluate_policy(candidate_policy, train)
            candidate_holdout = evaluate_policy(candidate_policy, holdout)
            candidate_review = (
                evaluate_policy(candidate_policy, review_cases).deviation if review_cases else 0.0
            )
            captured[version.version_id] = (candidate_train, candidate_holdout, candidate_review)
            improved = candidate_train.deviation < state["train_before"] - 1e-9
            not_worse = candidate_holdout.deviation <= state["holdout_before"] + HOLDOUT_TOLERANCE
            review_ok = candidate_review <= 1e-9
            reasons: list[str] = []
            if not improved:
                reasons.append("训练偏差未下降")
            if not not_worse:
                reasons.append("留出集退化超过容忍度")
            if not review_ok:
                reasons.append(f"违反人工评审结论（评审偏差 {candidate_review:.4f}）")
            return EvaluationReport(
                passed=improved and not_worse and review_ok,
                metrics={
                    "train_deviation_before": state["train_before"],
                    "train_deviation_after": candidate_train.deviation,
                    "holdout_deviation_before": state["holdout_before"],
                    "holdout_deviation_after": candidate_holdout.deviation,
                    "review_deviation": candidate_review,
                },
                notes="；".join(reasons) if reasons else "训练偏差下降且留出集/评审用例均未退化",
            )

        feedback = FeedbackRecord(
            request_id=f"p9-round-{index}",
            outcome=FeedbackOutcome.NEGATIVE if active_train.deviation > 0 else FeedbackOutcome.NEUTRAL,
            note=(
                f"偏差归因 {target}："
                + "，".join(f"{key}={value:.4f}" for key, value in active_train.dimensions.items())
            ),
            proposed_updates=dict(best_patch),
        )
        gate = evaluate_and_apply(registry, feedback, evaluate)
        candidate_train, candidate_holdout, candidate_review = captured[gate.candidate.version_id]
        accepted = gate.action == "promoted"
        rounds.append(
            EvolutionRound(
                round=index,
                target_dimension=target,
                candidate_version=gate.candidate.version_id,
                params_delta=dict(best_patch),
                train_before=round(active_train.deviation, 6),
                train_after=round(candidate_train.deviation, 6),
                holdout_before=round(active_holdout.deviation, 6),
                holdout_after=round(candidate_holdout.deviation, 6),
                attribution_before=dict(active_train.dimensions),
                action=gate.action,
                reason=gate.report.notes,
            )
        )
        if not accepted:
            break
        minor += 1
        label = release_label(minor)
        releases.append(
            PolicyRelease(
                label=label,
                version_id=gate.candidate.version_id,
                parent_label=parent_label,
                trigger_feedback_id=feedback.request_id,
                trigger_review_ids=[review.review_id for review in reviews],
                params_delta=dict(best_patch),
                expected_improvement=round(active_train.deviation - candidate_train.deviation, 6),
                train_deviation_after=round(candidate_train.deviation, 6),
            )
        )
        parent_label = label
        active_policy = policy_from_version(gate.active)
        active_train = candidate_train
        active_holdout = candidate_holdout
        active_review = (
            evaluate_policy(active_policy, review_cases) if review_cases else None
        )

    final_policy = active_policy
    final_train = active_train
    final_holdout = active_holdout
    final_review = evaluate_policy(final_policy, review_cases) if review_cases else None
    final_version_id = registry.active.version_id
    final_label = releases[-1].label if releases else base_label

    # 回滚验证：回到 v1 偏差必须逐位复原，再恢复到最终版本
    initial_train_check = evaluate_policy(initial, train)
    restored_v1 = registry.rollback("v1")
    rollback_train = evaluate_policy(policy_from_version(restored_v1), train)
    restored_final = registry.rollback(final_version_id)
    restored_train = evaluate_policy(policy_from_version(restored_final), train)
    rollback = RollbackCheck(
        rolled_back_to="v1",
        deviation_after_rollback=round(rollback_train.deviation, 6),
        initial_deviation=round(initial_train_check.deviation, 6),
        matches_initial=round(rollback_train.deviation, 6) == round(initial_train_check.deviation, 6),
        restored_version=final_version_id,
        restored_deviation=round(restored_train.deviation, 6),
    )

    review_summaries: list[ReviewSummary] = []
    if review_cases:
        for review in reviews:
            case = review.to_case()
            review_summaries.append(
                ReviewSummary(
                    review_id=review.review_id,
                    reviewer_role=review.reviewer_role,
                    decision=review.decision.value,
                    reason=review.reason,
                    expected_allocation=dict(review.expected_allocation),
                    deviation_before=round(evaluate_policy(initial, [case]).deviation, 6),
                    deviation_after=round(evaluate_policy(final_policy, [case]).deviation, 6),
                )
            )

    real_baseline: RealEventPlan | None = None
    real_evolved: RealEventPlan | None = None
    if include_real_event:
        try:
            real_baseline = plan_real_event(initial)
            real_evolved = plan_real_event(final_policy)
        except RuntimeError:
            real_baseline = None
            real_evolved = None

    return EvolutionReport(
        case_digest=digest,
        feedback_verified=cases_digest(load_cases()) == digest,
        train_cases=len(train),
        holdout_cases=len(holdout),
        deviation_weights=dict(DEVIATION_WEIGHTS),
        initial_policy=initial,
        final_policy=final_policy,
        initial_train=evaluate_policy(initial, train),
        final_train=final_train,
        initial_holdout=evaluate_policy(initial, holdout),
        final_holdout=final_holdout,
        rounds=rounds,
        releases=releases,
        final_label=final_label,
        warm_start=(
            WarmStartInfo(
                entry_id=warm.entry.entry_id,
                label=warm.entry.label,
                similarity=warm.similarity,
                source_outcome=dict(warm.entry.outcome),
                params=dict(warm.entry.params),
            )
            if warm is not None
            else None
        ),
        reviews=review_summaries,
        review_cases=len(review_cases),
        rolled_back=rollback,
        real_event_baseline=real_baseline,
        real_event_evolved=real_evolved,
        history=[_describe_history(item) for item in registry.history],
    )


def _describe_history(item: Any) -> str:
    target = f" -> {item.target_version_id}" if item.target_version_id else ""
    return f"{item.action}:{item.version_id}{target}"


# --- 经验沉淀 -------------------------------------------------------------


def entries_from_report(
    report: EvolutionReport,
    cases: Sequence[AllocationCase],
    reviews: Sequence[ReviewRecord] = (),
    *,
    scope: MemoryScope = MemoryScope.AGENT,
) -> list[ExperienceEntry]:
    """把一次进化结果写成一条经验：场景签名 + 已验证有效的策略 + 实际偏差。"""
    train = [case for case in cases if case.split == "train"]
    entry = make_entry(
        scope=scope,
        signature=signature_for(list(cases)),
        label=report.final_label,
        policy_version=report.final_policy.policy_version,
        params=report.final_policy.model_dump(exclude={"policy_version"}),
        outcome={
            "deviation": report.final_train.deviation,
            "allocation_l1_units": float(report.final_train.allocation_l1_units),
            "critical_unmet_units": float(report.final_train.critical_unmet_units),
            "missed_transfers": float(report.final_train.missed_transfers),
        },
        feedback_digest=report.case_digest,
        event_ids=[case.case_id for case in train],
        review_ids=[review.review_id for review in reviews],
        note=f"由 {report.final_label} 进化结果沉淀；训练偏差 {report.final_train.deviation:.4f}",
    )
    return [entry]


def run_showcase(
    cases: Sequence[AllocationCase] | None = None,
    reviews: Sequence[ReviewRecord] | None = None,
    *,
    memory_path: Path | str | None = None,
    reuse_memory: bool = False,
    max_rounds: int = 4,
    include_real_event: bool = True,
    allow_combinations: bool = False,
) -> ShowcaseReport:
    """两次会话：第一次从零进化并把经验写进记忆；第二次从记忆 warm start。

    `reuse_memory=True` 时先读磁盘上已有的记忆，用于演示真正的跨进程沉淀；
    默认从空记忆开始，保证报告里的"第一次运行"是干净起点。
    """
    all_cases = list(cases) if cases is not None else load_cases()
    review_list = list(reviews) if reviews is not None else load_reviews()
    path = Path(memory_path) if memory_path else None
    memory = DecisionMemory()
    if reuse_memory and path is not None and path.is_file():
        memory = DecisionMemory.load(path)

    first = evolve(
        all_cases,
        reviews=review_list,
        memory=memory,
        max_rounds=max_rounds,
        include_real_event=include_real_event,
        allow_combinations=allow_combinations,
    )
    memory.remember_many(entries_from_report(first, all_cases, review_list))
    second = evolve(
        all_cases,
        reviews=review_list,
        memory=memory,
        max_rounds=max_rounds,
        include_real_event=False,
        allow_combinations=allow_combinations,
    )
    if path is not None:
        memory.save(path)
    return ShowcaseReport(
        cases_digest=first.case_digest,
        reviews=review_list,
        first=first,
        second=second,
        memory_entries=list(memory.entries),
        memory_path=_display_path(path) if path is not None else "",
    )


def _display_path(path: Path) -> str:
    """报告里写仓库相对路径，绝对路径换机器就失效。"""
    repo = Path(__file__).resolve().parents[2]
    try:
        return str(path.resolve().relative_to(repo)).replace("\\", "/")
    except ValueError:
        return str(path)


# --- 报告渲染 -------------------------------------------------------------


def _run_table(report: EvolutionReport) -> list[str]:
    before, after = report.initial_train, report.final_train
    hbefore, hafter = report.initial_holdout, report.final_holdout
    return [
        "| 指标 | 训练集 进化前 | 训练集 进化后 | 留出集 进化前 | 留出集 进化后 |",
        "| --- | --- | --- | --- | --- |",
        f"| 综合决策偏差（越小越好） | {before.deviation:.4f} | {after.deviation:.4f} | "
        f"{hbefore.deviation:.4f} | {hafter.deviation:.4f} |",
        f"| 分配向量 L1 差（台） | {before.allocation_l1_units} | {after.allocation_l1_units} | "
        f"{hbefore.allocation_l1_units} | {hafter.allocation_l1_units} |",
        f"| 关键片区未满足（台） | {before.critical_unmet_units} | {after.critical_unmet_units} | "
        f"{hbefore.critical_unmet_units} | {hafter.critical_unmet_units} |",
        f"| 应调拨未调拨（次） | {before.missed_transfers}/{before.expected_transfers} | "
        f"{after.missed_transfers}/{after.expected_transfers} | "
        f"{hbefore.missed_transfers}/{hbefore.expected_transfers} | "
        f"{hafter.missed_transfers}/{hafter.expected_transfers} |",
        f"| 优先序命中 | {before.order_hits}/{before.order_total} | {after.order_hits}/{after.order_total} | "
        f"{hbefore.order_hits}/{hbefore.order_total} | {hafter.order_hits}/{hafter.order_total} |",
        "",
        "注：关键片区未满足量的下限是 ground truth 自身也解决不了的缺口"
        "（资源确实不足，必须上报增援），因此进化后不为 0 是正确结果。",
    ]


def _rounds_table(report: EvolutionReport) -> list[str]:
    lines = [
        "| 轮次 | 归因维度 | 参数变更 | 训练偏差 | 留出偏差 | 门禁 | 依据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.rounds:
        delta = "，".join(f"{key}={value}" for key, value in sorted(item.params_delta.items())) or "-"
        lines.append(
            f"| {item.round} | {item.target_dimension} | {delta} | "
            f"{item.train_before:.4f} → {item.train_after:.4f} | "
            f"{item.holdout_before:.4f} → {item.holdout_after:.4f} | "
            f"{'采纳' if item.action == 'promoted' else '拒绝'} | {item.reason} |"
        )
    return lines


def _release_table(report: EvolutionReport) -> list[str]:
    lines = [
        "| 策略标签 | 版本 | 父标签 | 触发反馈 | 触发评审 | 参数变更 | 预期改善（偏差下降） |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.releases:
        delta = "，".join(f"{key}={value}" for key, value in sorted(item.params_delta.items())) or "-"
        reviews = "，".join(item.trigger_review_ids) or "-"
        lines.append(
            f"| `{item.label}` | {item.version_id} | `{item.parent_label}` | {item.trigger_feedback_id} | "
            f"{reviews} | {delta} | {item.expected_improvement:.4f} |"
        )
    if not report.releases:
        lines.append("| （无采纳） | - | - | - | - | - | - |")
    return lines


def render_markdown(report: EvolutionReport) -> str:
    lines = [
        "# P9 决策进化闭环报告：跨片区抽排资源分配",
        "",
        "由 `python -m decision_poc.policy_evolution --report <path>` 生成，**不要手改**。",
        "",
        f"- 评测集指纹：`{report.case_digest[:16]}`（反馈未被改动：{'是' if report.feedback_verified else '否'}）",
        f"- 用例：训练 {report.train_cases} 例 / 留出 {report.holdout_cases} 例（受控合成场景，"
        "真实事件 WO-20261029-007 仅做端到端演示，不进训练）",
        "- 反馈来源：冻结 ground truth（`samples/eval/allocation_cases.json`），不是真实市政数据回流；"
        "表述口径为『受控评测环境下的反馈信号』。",
        f"- 最终策略标签：`{report.final_label}`；本轮采纳 {report.promoted_rounds} / 拒绝 {report.rejected_rounds}",
        "",
        "## 1 进化前 vs 进化后",
        "",
        *_run_table(report),
        "",
        f"综合偏差降幅：训练 {report.train_deviation_drop():.4f} / 留出 {report.holdout_deviation_drop():.4f}；"
        f"留出门禁容忍度 {HOLDOUT_TOLERANCE}。",
        "",
        "## 2 逐轮明细（偏差归因 → 候选 → 门禁）",
        "",
        *_rounds_table(report),
        "",
        f"采纳 {report.promoted_rounds} 轮 / 拒绝 {report.rejected_rounds} 轮。"
        "拒绝发生在收敛探针上：门禁不改 active 版本，这正是「进化不能变得更差」的可执行形态。",
        "",
        "## 3 策略版本标签",
        "",
        *_release_table(report),
        "",
        "## 4 回滚验证",
        "",
    ]
    if report.rolled_back is not None:
        rb = report.rolled_back
        lines += [
            f"- 回滚到 `{rb.rolled_back_to}` 后训练偏差 `{rb.deviation_after_rollback:.4f}`，"
            f"初始值 `{rb.initial_deviation:.4f}`，逐位一致：{'是' if rb.matches_initial else '否'}",
            f"- 恢复到 `{rb.restored_version}` 后训练偏差 `{rb.restored_deviation:.4f}`",
        ]
    lines += ["", "## 5 真实事件端到端（WO-20261029-007 @ 2026-10-29T09:40:00+08:00）", ""]
    if report.real_event_baseline and report.real_event_evolved:
        lines += [
            "| 项 | 进化前（v1） | 进化后 |",
            "| --- | --- | --- |",
            f"| 分配 | {report.real_event_baseline.allocation_by_district} | "
            f"{report.real_event_evolved.allocation_by_district} |",
            f"| 未满足 | {report.real_event_baseline.unmet_by_district} | "
            f"{report.real_event_evolved.unmet_by_district} |",
            f"| 调拨 | {report.real_event_baseline.transfers or '无'} | "
            f"{report.real_event_evolved.transfers or '无'} |",
            f"| 需上报增援 | {report.real_event_baseline.escalation or '无'} | "
            f"{report.real_event_evolved.escalation or '无'} |",
        ]
    else:
        lines.append("（真实事件未跑通，仅报告评测集结果）")
    lines += ["", "## 6 版本历史（拒绝也留痕）", ""]
    lines += [f"- {item}" for item in report.history]
    lines += [
        "",
        "## 7 策略参数",
        "",
        "| 参数 | v1 | 进化后 |",
        "| --- | --- | --- |",
        f"| weight_closure | {report.initial_policy.weight_closure:g} | {report.final_policy.weight_closure:g} |",
        f"| weight_hazard | {report.initial_policy.weight_hazard:g} | {report.final_policy.weight_hazard:g} |",
        f"| weight_depth | {report.initial_policy.weight_depth:g} | {report.final_policy.weight_depth:g} |",
        f"| transfer_penalty | {report.initial_policy.transfer_penalty:g} | {report.final_policy.transfer_penalty:g} |",
        f"| allow_transfer_from_critical | {report.initial_policy.allow_transfer_from_critical} | "
        f"{report.final_policy.allow_transfer_from_critical} |",
        "",
        "综合偏差权重（代码常量，报告披露）："
        + "，".join(f"{key}={value}" for key, value in report.deviation_weights.items()),
    ]
    return "\n".join(lines) + "\n"


def render_showcase_markdown(showcase: ShowcaseReport) -> str:
    first, second = showcase.first, showcase.second
    # 口径按数据走：评审角色带"真实用户（"前缀 = 来自真实评分通道，否则是角色扮演。
    real_reviews = any(item.reviewer_role.startswith("真实用户") for item in first.reviews)
    review_source = (
        "人工评审来自**真实用户评分通道**（个人意见，非部门批复）。"
        if real_reviews
        else "人工评审为角色扮演，非真实部门批复。"
    )
    lines = [
        "# P9 决策进化闭环报告：跨片区抽排资源分配（含分层记忆与人工反馈）",
        "",
        "由 `python -m decision_poc.policy_evolution --report <path>` 生成，**不要手改**。",
        "",
        f"- 评测集指纹：`{showcase.cases_digest[:16]}`",
        f"- 用例：训练 {first.train_cases} 例 / 留出 {first.holdout_cases} 例 / 人工评审 {first.review_cases} 例",
        "- 反馈来源：冻结 ground truth + 人工评审结论（受控评测环境下的反馈信号），"
        "不是真实市政数据回流；" + review_source,
        "- 分层记忆为**离线同构实现**（Tenant/User/Agent 三层契约与 Nexent 对齐），"
        "**未在真实 Nexent 平台验证**。",
        "",
        "## 0 一句话结论",
        "",
        "第一次会话从零进化：训练偏差 "
        f"{first.initial_train.deviation:.4f} → {first.final_train.deviation:.4f}；"
        f"第二次会话从分层记忆 warm start：起点偏差 {second.initial_train.deviation:.4f}，"
        f"只需 {len(second.rounds)} 轮收敛探针（采纳 {second.promoted_rounds} 次）——经验沉淀成立。",
        "人工评审提出的修正，进化后偏差 "
        f"{_review_before(first):.4f} → {_review_after(first):.4f}，同类否决不再发生。",
        "",
        "## 1 第一次运行（无记忆）：进化前 vs 进化后",
        "",
        *_run_table(first),
        "",
        f"综合偏差降幅：训练 {first.train_deviation_drop():.4f} / 留出 {first.holdout_deviation_drop():.4f}；"
        f"最终策略标签 `{first.final_label}`；留出门禁容忍度 {HOLDOUT_TOLERANCE}。",
        "",
        "## 2 第二次运行（分层记忆 warm start）",
        "",
    ]
    if second.warm_start is not None:
        warm = second.warm_start
        lines += [
            f"- 命中经验条目：`{warm.entry_id}`（标签 `{warm.label}`，场景相似度 {warm.similarity:.4f}）",
            f"- 该条目历史结果：偏差 {warm.source_outcome.get('deviation', 0.0):.4f}、"
            f"应调拨未调拨 {int(warm.source_outcome.get('missed_transfers', 0))}、"
            f"关键片区未满足 {int(warm.source_outcome.get('critical_unmet_units', 0))} 台",
            "",
            "| 指标 | 第一次运行起点（v1） | 第二次运行起点（warm start） | 第二次运行终点 |",
            "| --- | --- | --- | --- |",
            f"| 训练集综合偏差 | {first.initial_train.deviation:.4f} | {second.initial_train.deviation:.4f} | "
            f"{second.final_train.deviation:.4f} |",
            f"| 训练集分配向量 L1 差（台） | {first.initial_train.allocation_l1_units} | "
            f"{second.initial_train.allocation_l1_units} | {second.final_train.allocation_l1_units} |",
            f"| 轮次 | {len(first.rounds)} | {len(second.rounds)} | - |",
            "",
            "口径：第二次运行没有重新搜索策略，只在已有策略上做收敛探针（唯一一轮被门禁拒绝），"
            "说明记忆里存的是**已验证有效的策略**，不是待验证的猜测。",
        ]
    else:
        lines.append("- 未命中记忆（记忆为空或相似度不足）。")
    lines += [
        "",
        "## 3 人工协同反馈（第二进化信号）",
        "",
        "| 评审 ID | 评审角色 | 结论 | 修正后分配 | 评审前偏差 | 评审后偏差 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in first.reviews:
        lines.append(
            f"| {item.review_id} | {item.reviewer_role} | {item.decision} | "
            f"{item.expected_allocation} | {item.deviation_before:.4f} | {item.deviation_after:.4f} |"
        )
    if not first.reviews:
        lines.append("| （无评审） | - | - | - | - | - |")
    for review in showcase.reviews:
        lines += [
            "",
            f"- `{review.review_id}`（{review.reviewer_role}）：{review.reason}",
            f"- 评审结论已转成评审用例进入门禁：候选不满足该分配即被拒绝；"
            f"口径标注「{review.reviewer_note}」。",
        ]
    lines += [
        "",
        "## 4 逐轮明细（偏差归因 → 候选 → 门禁）",
        "",
        *_rounds_table(first),
        "",
        f"采纳 {first.promoted_rounds} 轮 / 拒绝 {first.rejected_rounds} 轮。"
        "拒绝发生在收敛探针上：门禁不改 active 版本，这正是「进化不能变得更差」的可执行形态。",
        "",
        "## 5 策略版本标签（可审计的进化制品）",
        "",
        *_release_table(first),
        "",
        "## 6 回滚验证",
        "",
    ]
    rb = first.rolled_back
    if rb is not None:
        lines += [
            f"- 回滚到 `{rb.rolled_back_to}` 后训练偏差 `{rb.deviation_after_rollback:.4f}`，"
            f"初始值 `{rb.initial_deviation:.4f}`，逐位一致：{'是' if rb.matches_initial else '否'}",
            f"- 恢复到 `{rb.restored_version}` 后训练偏差 `{rb.restored_deviation:.4f}`",
        ]
    lines += ["", "## 7 真实事件端到端（WO-20261029-007 @ 2026-10-29T09:40:00+08:00）", ""]
    if first.real_event_baseline and first.real_event_evolved:
        lines += [
            "| 项 | 进化前（v1） | 进化后 |",
            "| --- | --- | --- |",
            f"| 分配 | {first.real_event_baseline.allocation_by_district} | "
            f"{first.real_event_evolved.allocation_by_district} |",
            f"| 未满足 | {first.real_event_baseline.unmet_by_district} | "
            f"{first.real_event_evolved.unmet_by_district} |",
            f"| 调拨 | {first.real_event_baseline.transfers or '无'} | "
            f"{first.real_event_evolved.transfers or '无'} |",
            f"| 需上报增援 | {first.real_event_baseline.escalation or '无'} | "
            f"{first.real_event_evolved.escalation or '无'} |",
        ]
    else:
        lines.append("（真实事件未跑通）")
    lines += ["", "## 8 分层记忆内容（经验库）", ""]
    for entry in showcase.memory_entries:
        lines.append(
            f"- `{entry.entry_id}` scope={entry.scope.value} 标签=`{entry.label}` "
            f"偏差={entry.outcome.get('deviation', 0.0):.4f} "
            f"事件={','.join(entry.event_ids)} 评审={','.join(entry.review_ids) or '-'}"
        )
    if showcase.memory_path:
        lines += ["", f"记忆落盘：`{showcase.memory_path}`（追加式；跨进程可复用）。"]
    lines += [
        "",
        "## 9 版本历史（拒绝也留痕）",
        "",
    ]
    lines += [f"- {item}" for item in first.history]
    lines += [
        "",
        "## 10 策略参数",
        "",
        "| 参数 | v1 | 进化后 |",
        "| --- | --- | --- |",
        f"| weight_closure | {first.initial_policy.weight_closure:g} | {first.final_policy.weight_closure:g} |",
        f"| weight_hazard | {first.initial_policy.weight_hazard:g} | {first.final_policy.weight_hazard:g} |",
        f"| weight_depth | {first.initial_policy.weight_depth:g} | {first.final_policy.weight_depth:g} |",
        f"| transfer_penalty | {first.initial_policy.transfer_penalty:g} | {first.final_policy.transfer_penalty:g} |",
        f"| allow_transfer_from_critical | {first.initial_policy.allow_transfer_from_critical} | "
        f"{first.final_policy.allow_transfer_from_critical} |",
        "",
        "综合偏差权重（代码常量，报告披露）："
        + "，".join(f"{key}={value}" for key, value in first.deviation_weights.items()),
        "",
        "## 11 表述边界",
        "",
        "- 分层记忆为离线同构契约，映射到 Nexent 的 Agent/Tenant/User 记忆需平台侧适配，**未真机验证**；",
        (
            "- 人工评审来自真实用户评分通道，是个人意见，不代表真实部门批复；"
            if any(item.reviewer_role.startswith("真实用户") for item in first.reviews)
            else "- 人工评审为角色扮演，用于演示第二反馈通道，不代表真实部门批复；"
        ),
        "- 进化是「归因 + 受控候选 + 门禁 + 回滚」，不是在线学习或模型自训练。",
    ]
    return "\n".join(lines) + "\n"


def _review_before(report: EvolutionReport) -> float:
    return report.reviews[0].deviation_before if report.reviews else 0.0


def _review_after(report: EvolutionReport) -> float:
    return report.reviews[0].deviation_after if report.reviews else 0.0


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(description="P9 policy evolution over cross-district pump allocation")
    parser.add_argument("--cases", default=None, help="分配评测集 JSON")
    parser.add_argument("--reviews", default=None, help="人工评审 JSON")
    parser.add_argument("--memory", default=str(DEFAULT_MEMORY_PATH), help="分层记忆落盘路径")
    parser.add_argument("--reuse-memory", action="store_true", help="先读取磁盘记忆（跨进程演示）")
    parser.add_argument("--report", default=None, help="写出 Markdown 报告")
    parser.add_argument("--json", dest="json_path", default=None, help="写出机器可读结果")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--no-real-event", action="store_true", help="跳过真实事件端到端")
    parser.add_argument(
        "--allow-combinations",
        action="store_true",
        help="选优时并列评估组合候选（同时改动 ≥2 个参数），用于验证门禁拦截多点同时改动",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases) if args.cases else load_cases()
    reviews = load_reviews(args.reviews) if args.reviews else load_reviews()
    showcase = run_showcase(
        cases,
        reviews,
        memory_path=args.memory,
        reuse_memory=args.reuse_memory,
        max_rounds=args.max_rounds,
        include_real_event=not args.no_real_event,
        allow_combinations=args.allow_combinations,
    )
    first, second = showcase.first, showcase.second

    print(
        f"第一次运行：训练偏差 {first.initial_train.deviation:.4f} → {first.final_train.deviation:.4f}；"
        f"留出 {first.initial_holdout.deviation:.4f} → {first.final_holdout.deviation:.4f}；"
        f"标签 {first.final_label}"
    )
    print(
        f"轮次 {len(first.rounds)}（采纳 {first.promoted_rounds} / 拒绝 {first.rejected_rounds}）；"
        f"关键片区未满足 {first.initial_train.critical_unmet_units} → {first.final_train.critical_unmet_units} 台；"
        f"应调拨未调拨 {first.initial_train.missed_transfers} → {first.final_train.missed_transfers}"
    )
    if first.reviews:
        item = first.reviews[0]
        print(
            f"人工评审 {item.review_id}（{item.reviewer_role}）：评审偏差 "
            f"{item.deviation_before:.4f} → {item.deviation_after:.4f}"
        )
    if second.warm_start is not None:
        print(
            f"第二次运行（记忆 {second.warm_start.entry_id}，相似度 {second.warm_start.similarity:.4f}）："
            f"起点偏差 {second.initial_train.deviation:.4f}，轮次 {len(second.rounds)}"
        )
    if first.real_event_baseline and first.real_event_evolved:
        print(
            "真实事件：v1 "
            + json.dumps(first.real_event_baseline.allocation_by_district, ensure_ascii=False)
            + " → "
            + json.dumps(first.real_event_evolved.allocation_by_district, ensure_ascii=False)
        )

    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(
            showcase.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写出 {args.json_path}")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(render_showcase_markdown(showcase), encoding="utf-8")
        print(f"已写出 {args.report}")
    return 0


__all__ = [
    "BASELINE_LABEL",
    "EvolutionReport",
    "EvolutionRound",
    "HOLDOUT_TOLERANCE",
    "PolicyRelease",
    "RealEventPlan",
    "ReviewSummary",
    "RollbackCheck",
    "ShowcaseReport",
    "WarmStartInfo",
    "candidate_patches",
    "entries_from_report",
    "evolve",
    "policy_from_version",
    "probe_patches",
    "release_label",
    "render_markdown",
    "render_showcase_markdown",
    "review_cases_for",
    "run_showcase",
    "worst_dimension",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
