"""P9 决策层：跨片区抽排资源分配策略 + 偏差信号。

为什么单独做一个决策层
----------------------
P6 的六跳只回答"缺多少台"，不回答"这几台先给谁"。缺口的分配口径此前只存在于
CF2 反事实的一句话里（"只能保一处，优先保障 A-03"），没有形成可版本化、可评测、
可回滚的决策。

所以这里把"分配"从推演里拆出来：

    DeductionResult.decision_inputs   ->  策略 decide()  ->  AllocationDecision
    AllocationDecision vs 冻结 ground truth  ->  DeviationReport

三条约束：

1. **策略只决定怎么分，不改推演结论**。需求、缺口、冲突、序位都是输入，不是被优化
   的对象；否则"进化"会退化成调结论。
2. **不调模型**。分配是确定性算法，策略只是参数；同一输入两次运行逐位一致。
3. **没有决策输入就不给方案**。`decision_inputs` 为空（资源测算没跑通）时抛
   `AllocationUnavailable`，而不是把空信号当"各片区需求 0"分一遍。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import ConfigDict, Field, model_validator

from .contracts import StrictModel
from .deduction import DistrictSignal

# 跨区调用时延：urban-scenario-ground-truth.md 跳 5（B-01 泵站到 A-03 需 40 分钟）
TRANSFER_DELAY_MIN = 40

DEFAULT_CASES_PATH = (
    Path(__file__).resolve().parents[2] / "samples" / "eval" / "allocation_cases.json"
)

# 综合偏差权重。写死在这里并在报告披露，避免"换个权重换个结论"。
DEVIATION_WEIGHTS: dict[str, float] = {
    "allocation_l1": 0.4,
    "critical_unmet": 0.3,
    "transfer": 0.2,
    "order": 0.1,
}

# 关键状态 = 已过封路阈值，或受影响范围含燃气/电力。
# 关键片区默认不可被调出（见 AllocationPolicy.allow_transfer_from_critical）。
CRITICAL_RULE_NOTE = "关键片区判定：closure_required=True 或 has_hazard=True（Ground Truth 跳 5 / CF2）"


class AllocationUnavailable(RuntimeError):
    """没有可用决策输入时拒绝出方案，而不是给一份"空分配"。"""


class AllocationPolicy(StrictModel):
    """有版本、可评测、可回滚的分配策略。

    v1（默认值）= 所有权重 0、不跨区调拨 = "各片区先用本区库存"。
    这不是人为设弱的稻草人：Ground Truth 跳 5 的缺口口径就是按"本区 5 台 vs 本区
    4 台"算出来的，即默认只比较本区。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(default="v1", min_length=1)
    weight_closure: float = Field(default=0.0, ge=0.0)
    weight_hazard: float = Field(default=0.0, ge=0.0)
    weight_depth: float = Field(default=0.0, ge=0.0)
    depth_scale_cm: float = Field(default=25.0, gt=0.0)
    transfer_penalty: float = Field(default=1.0, ge=0.0)
    allow_transfer_from_critical: bool = False

    # --- 优先序 -----------------------------------------------------------
    def score(self, signal: DistrictSignal) -> float:
        score = 0.0
        if signal.closure_required is True:
            score += self.weight_closure
        if signal.has_hazard:
            score += self.weight_hazard
        if signal.depth_cm is not None and self.weight_depth:
            score += self.weight_depth * min(signal.depth_cm, self.depth_scale_cm) / self.depth_scale_cm
        return round(score, 6)

    def is_critical(self, signal: DistrictSignal) -> bool:
        return signal.closure_required is True or signal.has_hazard

    # --- 进化 -------------------------------------------------------------
    def with_updates(self, updates: Mapping[str, Any]) -> "AllocationPolicy":
        return self.model_copy(update=dict(updates))


def default_policy(version: str = "v1") -> AllocationPolicy:
    return AllocationPolicy(policy_version=version)


class Transfer(StrictModel):
    from_district: str = Field(min_length=1)
    to_district: str = Field(min_length=1)
    qty: int = Field(ge=1)
    delay_min: int = Field(default=TRANSFER_DELAY_MIN, ge=0)
    reason: str = ""


class AllocationLine(StrictModel):
    district: str
    depth_cm: float | None = None
    closure_required: bool | None = None
    has_hazard: bool = False
    critical: bool = False
    demand: int = Field(ge=0)
    local_available: int = Field(ge=0)
    allocated: int = Field(ge=0)
    unmet: int = Field(ge=0)
    score: float = 0.0
    basis: list[str] = Field(default_factory=list)


class AllocationDecision(StrictModel):
    policy_version: str
    allocation_by_district: dict[str, int] = Field(default_factory=dict)
    unmet_by_district: dict[str, int] = Field(default_factory=dict)
    priority_order: list[str] = Field(default_factory=list)
    lines: list[AllocationLine] = Field(default_factory=list)
    transfers: list[Transfer] = Field(default_factory=list)
    total_demand: int = 0
    total_allocated: int = 0
    total_unmet: int = 0
    # 缺口无法在本级解决时的显式动作，不四舍五入成"已满足"
    escalation: list[str] = Field(default_factory=list)
    snapshot_hash: str = ""


def decide(
    signals: Mapping[str, DistrictSignal] | Sequence[DistrictSignal],
    policy: AllocationPolicy | None = None,
    *,
    snapshot_hash: str = "",
) -> AllocationDecision:
    """按策略分配稀缺抽水泵。

    算法（确定性，无随机）：

    1. 本区库存先满足本区需求；
    2. 未满足片区按 (分值降序, 片区名升序) 处理；
    3. 调出方按 (分值升序, 片区名升序) 扫描：跳过本区、跳过关键片区（默认）、
       跳过优先序差 < transfer_penalty 的组合；
    4. 调出方可动用"未用库存 + 已分配给自己的库存"，因此允许低风险片区降到 0
       —— 这正是 CF2"只能保一处"的口径；
    5. 仍有未满足的，写进 `escalation`（上报跨区增援），不当成已满足。
    """
    policy = policy or default_policy()
    by_district: dict[str, DistrictSignal] = {}
    for item in signals.values() if isinstance(signals, Mapping) else signals:
        by_district[item.district] = item
    if not by_district:
        raise AllocationUnavailable(
            "没有决策输入（资源测算未产出 decision_inputs），不产出分配方案"
        )

    districts = sorted(by_district)
    scores = {name: policy.score(by_district[name]) for name in districts}
    priority_order = sorted(districts, key=lambda name: (-scores[name], name))

    allocated = {
        name: min(by_district[name].demand, by_district[name].local_available)
        for name in districts
    }
    stock = {
        name: max(0, by_district[name].local_available - allocated[name])
        for name in districts
    }
    transfers: list[Transfer] = []

    for beneficiary in priority_order:
        need = by_district[beneficiary].demand - allocated[beneficiary]
        if need <= 0:
            continue
        for donor in sorted(districts, key=lambda name: (scores[name], name)):
            if need <= 0:
                break
            if donor == beneficiary:
                continue
            if not policy.allow_transfer_from_critical and policy.is_critical(by_district[donor]):
                continue
            if round(scores[beneficiary] - scores[donor], 6) < policy.transfer_penalty:
                continue
            movable = stock[donor] + allocated[donor]
            if movable <= 0:
                continue
            qty = min(need, movable)
            from_stock = min(qty, stock[donor])
            stock[donor] -= from_stock
            from_allocated = qty - from_stock
            allocated[donor] -= from_allocated
            allocated[beneficiary] += qty
            need -= qty
            transfers.append(
                Transfer(
                    from_district=donor,
                    to_district=beneficiary,
                    qty=qty,
                    delay_min=TRANSFER_DELAY_MIN,
                    reason=(
                        f"{beneficiary} 优先序 {scores[beneficiary]:g} 高于 {donor} {scores[donor]:g}，"
                        f"本区缺口 {by_district[beneficiary].demand - allocated[beneficiary] + qty} 台"
                    ),
                )
            )

    lines: list[AllocationLine] = []
    escalation: list[str] = []
    for name in priority_order:
        signal = by_district[name]
        unmet = signal.demand - allocated[name]
        basis: list[str] = []
        if signal.closure_required is None:
            basis.append("深度未知，未判定封路")
        elif signal.closure_required:
            basis.append("已过封路阈值")
        if signal.has_hazard:
            basis.append("受影响范围含燃气/电力")
        lines.append(
            AllocationLine(
                district=name,
                depth_cm=signal.depth_cm,
                closure_required=signal.closure_required,
                has_hazard=signal.has_hazard,
                critical=policy.is_critical(signal),
                demand=signal.demand,
                local_available=signal.local_available,
                allocated=allocated[name],
                unmet=unmet,
                score=scores[name],
                basis=basis,
            )
        )
        if unmet > 0:
            escalation.append(
                f"{name} 仍缺 {unmet} 台：本区库存 {signal.local_available} 台，"
                f"本区需求 {signal.demand} 台，需上报跨区/市级增援"
            )

    return AllocationDecision(
        policy_version=policy.policy_version,
        allocation_by_district={name: allocated[name] for name in districts},
        unmet_by_district={name: by_district[name].demand - allocated[name] for name in districts},
        priority_order=priority_order,
        lines=lines,
        transfers=transfers,
        total_demand=sum(by_district[name].demand for name in districts),
        total_allocated=sum(allocated.values()),
        total_unmet=sum(by_district[name].demand - allocated[name] for name in districts),
        escalation=escalation,
        snapshot_hash=snapshot_hash,
    )


# --- ground truth 用例 ----------------------------------------------------


class TransferExpect(StrictModel):
    from_district: str = Field(min_length=1)
    to_district: str = Field(min_length=1)
    qty: int = Field(ge=1)
    delay_min: int = Field(default=TRANSFER_DELAY_MIN, ge=0)

    def key(self) -> tuple[str, str]:
        return (self.from_district, self.to_district)


class AllocationExpect(StrictModel):
    allocation_by_district: dict[str, int] = Field(default_factory=dict)
    transfers: list[TransferExpect] = Field(default_factory=list)
    priority_order: list[str] = Field(default_factory=list)
    critical_unmet: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unmet_matches_critical(self) -> "AllocationExpect":
        # 关键片区未满足量必须与分配向量一致：两处写不一致就是 ground truth 自身坏了
        for name, unmet in self.critical_unmet.items():
            if unmet < 0:
                raise ValueError(f"critical_unmet[{name}] 不能为负")
        return self


class AllocationCase(StrictModel):
    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    split: str = "train"
    basis: list[str] = Field(min_length=1)
    signals: list[DistrictSignal] = Field(min_length=1)
    expect: AllocationExpect

    @model_validator(mode="after")
    def split_known(self) -> "AllocationCase":
        if self.split not in {"train", "holdout", "review"}:
            raise ValueError("split must be train, holdout or review")
        return self


def load_cases(path: Path | str = DEFAULT_CASES_PATH) -> list[AllocationCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [AllocationCase.model_validate(item) for item in payload["cases"]]


def cases_digest(cases: Sequence[AllocationCase]) -> str:
    """反馈指纹：用例集规范化 JSON 的 sha256，用于校验反馈未被改动。"""
    payload = [case.model_dump(mode="json") for case in cases]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- 偏差 ----------------------------------------------------------------


class CaseDeviation(StrictModel):
    case_id: str
    split: str
    title: str = ""
    deviation: float = 0.0
    dimensions: dict[str, float] = Field(default_factory=dict)
    allocation: dict[str, int] = Field(default_factory=dict)
    expected_allocation: dict[str, int] = Field(default_factory=dict)
    transfers: list[str] = Field(default_factory=list)
    expected_transfers: list[str] = Field(default_factory=list)
    priority_order: list[str] = Field(default_factory=list)
    expected_priority_order: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PolicyEvaluation(StrictModel):
    """一个策略在一组用例上的表现：偏差 + 可直接写进报告的绝对量指标。"""

    policy_version: str = ""
    cases_total: int = 0
    deviation: float = 0.0
    dimensions: dict[str, float] = Field(default_factory=dict)
    # 绝对量指标：报告里比"综合分"更可核对
    critical_unmet_units: int = 0
    expected_critical_unmet_units: int = 0
    allocation_l1_units: int = 0
    missed_transfers: int = 0
    expected_transfers: int = 0
    order_hits: int = 0
    order_total: int = 0
    case_results: list[CaseDeviation] = Field(default_factory=list)

    @property
    def order_accuracy(self) -> float:
        return round(self.order_hits / self.order_total, 4) if self.order_total else 1.0


def _kendall_distance(actual: Sequence[str], expected: Sequence[str]) -> float:
    common = [item for item in expected if item in actual]
    n = len(common)
    if n < 2:
        return 0.0
    pos = {name: index for index, name in enumerate(actual)}
    inversions = 0
    for i in range(n):
        for j in range(i + 1, n):
            if pos[common[i]] > pos[common[j]]:
                inversions += 1
    return round(inversions / (n * (n - 1) / 2), 6)


def case_deviation(decision: AllocationDecision, case: AllocationCase) -> CaseDeviation:
    expect = case.expect
    names = sorted(set(decision.allocation_by_district) | set(expect.allocation_by_district))
    demand_total = sum(signal.demand for signal in case.signals)
    diff_units = sum(
        abs(decision.allocation_by_district.get(name, 0) - expect.allocation_by_district.get(name, 0))
        for name in names
    )
    allocation_l1 = round(diff_units / (2 * demand_total), 6) if demand_total else 0.0

    critical_names = [signal.district for signal in case.signals if signal.closure_required is True or signal.has_hazard]
    critical_demand = sum(
        signal.demand for signal in case.signals if signal.district in critical_names
    )
    expected_critical_unmet = sum(
        expect.critical_unmet.get(name, 0) for name in critical_names
    )
    actual_critical_unmet = sum(
        decision.unmet_by_district.get(name, 0) for name in critical_names
    )
    critical_unmet = (
        round(abs(expected_critical_unmet - actual_critical_unmet) / critical_demand, 6)
        if critical_demand
        else 0.0
    )

    expected_by_key = {item.key(): item for item in expect.transfers}
    actual_by_key = {(item.from_district, item.to_district): item for item in decision.transfers}
    expected_qty = sum(item.qty for item in expect.transfers)
    if expected_qty:
        matched = sum(
            min(item.qty, actual_by_key[key].qty)
            for key, item in expected_by_key.items()
            if key in actual_by_key
        )
        transfer = round(1 - matched / expected_qty, 6)
    else:
        transfer = 0.0 if not decision.transfers else 1.0

    order = _kendall_distance(decision.priority_order, expect.priority_order)

    dimensions = {
        "allocation_l1": allocation_l1,
        "critical_unmet": critical_unmet,
        "transfer": transfer,
        "order": order,
    }
    if case.split == "review":
        # 评审只对"谁拿几台"负责：跨区时延与处置排序不由评审裁定，
        # 否则等于让一条人工意见替系统背了它没看过的指标。
        dimensions["transfer"] = 0.0
        dimensions["order"] = 0.0
    deviation = round(
        sum(DEVIATION_WEIGHTS[key] * value for key, value in dimensions.items()), 6
    )

    notes: list[str] = []
    if diff_units:
        notes.append(f"分配向量差 {diff_units} 台")
    if expected_critical_unmet != actual_critical_unmet:
        notes.append(f"关键片区未满足 {actual_critical_unmet} 台，期望 {expected_critical_unmet} 台")
    if transfer:
        notes.append("调拨动作与期望不一致")
    if order:
        notes.append(f"优先序 {decision.priority_order}，期望 {expect.priority_order}")

    return CaseDeviation(
        case_id=case.case_id,
        split=case.split,
        title=case.title,
        deviation=deviation,
        dimensions=dimensions,
        allocation=dict(decision.allocation_by_district),
        expected_allocation=dict(expect.allocation_by_district),
        transfers=[f"{item.from_district}->{item.to_district}:{item.qty}" for item in decision.transfers],
        expected_transfers=[f"{item.from_district}->{item.to_district}:{item.qty}" for item in expect.transfers],
        priority_order=list(decision.priority_order),
        expected_priority_order=list(expect.priority_order),
        notes=notes,
    )


def evaluate_policy(
    policy: AllocationPolicy,
    cases: Sequence[AllocationCase],
) -> PolicyEvaluation:
    results: list[CaseDeviation] = []
    critical_unmet_units = 0
    expected_critical_unmet_units = 0
    allocation_l1_units = 0
    missed_transfers = 0
    expected_transfers = 0
    order_hits = 0
    dimensions_acc = {key: 0.0 for key in DEVIATION_WEIGHTS}

    for case in cases:
        decision = decide(case.signals, policy)
        dev = case_deviation(decision, case)
        results.append(dev)
        for key in DEVIATION_WEIGHTS:
            dimensions_acc[key] += dev.dimensions[key]

        critical_names = [
            signal.district
            for signal in case.signals
            if signal.closure_required is True or signal.has_hazard
        ]
        critical_unmet_units += sum(decision.unmet_by_district.get(name, 0) for name in critical_names)
        expected_critical_unmet_units += sum(
            case.expect.critical_unmet.get(name, 0) for name in critical_names
        )
        names = sorted(
            set(decision.allocation_by_district) | set(case.expect.allocation_by_district)
        )
        allocation_l1_units += sum(
            abs(
                decision.allocation_by_district.get(name, 0)
                - case.expect.allocation_by_district.get(name, 0)
            )
            for name in names
        )
        actual_keys = {(item.from_district, item.to_district) for item in decision.transfers}
        for item in case.expect.transfers:
            expected_transfers += 1
            if item.key() not in actual_keys:
                missed_transfers += 1
        if dev.dimensions["order"] == 0.0:
            order_hits += 1

    if not results:
        return PolicyEvaluation(policy_version=policy.policy_version)

    dimensions = {
        key: round(value / len(results), 6) for key, value in dimensions_acc.items()
    }
    deviation = round(
        sum(DEVIATION_WEIGHTS[key] * value for key, value in dimensions.items()), 6
    )
    return PolicyEvaluation(
        policy_version=policy.policy_version,
        cases_total=len(results),
        deviation=deviation,
        dimensions=dimensions,
        critical_unmet_units=critical_unmet_units,
        expected_critical_unmet_units=expected_critical_unmet_units,
        allocation_l1_units=allocation_l1_units,
        missed_transfers=missed_transfers,
        expected_transfers=expected_transfers,
        order_hits=order_hits,
        order_total=len(results),
        case_results=results,
    )


__all__ = [
    "AllocationCase",
    "AllocationDecision",
    "AllocationExpect",
    "AllocationLine",
    "AllocationPolicy",
    "AllocationUnavailable",
    "CaseDeviation",
    "DEVIATION_WEIGHTS",
    "DEFAULT_CASES_PATH",
    "DistrictSignal",
    "PolicyEvaluation",
    "TRANSFER_DELAY_MIN",
    "Transfer",
    "TransferExpect",
    "case_deviation",
    "cases_digest",
    "decide",
    "default_policy",
    "evaluate_policy",
    "load_cases",
]
