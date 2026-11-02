"""运筹层：算一个"最优上界"，用来回答"你的分配策略是不是拍脑袋"。

口径先说清楚，避免把它当成可执行方案：

- 本模块求的是**松弛最优**：只追求
  ①关键片区未满足最少 → ②总未满足最少，**不计跨区调运的 40 分钟延迟与车辆约束**；
- 因为忽略了延迟，它给出的是**上界**：任何实际策略都不可能比它更好，
  所以"规则策略距最优差多少"这个比较是成立的；
- 反过来不能拿它当执行建议——真正的可执行方案仍由 P9 策略产出。

关键片区的判定沿用 P9 的口径：已过封路阈值或存在危险源。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel
from decision_poc.deduction import DistrictSignal


class TransferPlan(StrictModel):
    from_district: str = Field(min_length=1)
    to_district: str = Field(min_length=1)
    qty: int = Field(gt=0)


class OptimalAllocation(StrictModel):
    allocation_by_district: dict[str, int]
    critical_unmet: dict[str, int]
    total_unmet: int
    transfers: list[TransferPlan] = Field(default_factory=list)
    objective: str = "lexicographic: critical_unmet -> total_unmet（松弛最优，未计跨区延迟）"
    assumptions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _totals_are_consistent(self) -> OptimalAllocation:
        if self.total_unmet < 0:
            raise ValueError("total_unmet 不能为负")
        if any(value < 0 for value in self.allocation_by_district.values()):
            raise ValueError("分配数量不能为负")
        return self


class OptimalityGap(StrictModel):
    policy_version: str = ""
    policy_critical_unmet: int = Field(ge=0)
    optimal_critical_unmet: int = Field(ge=0)
    policy_total_unmet: int = Field(ge=0)
    optimal_total_unmet: int = Field(ge=0)
    verdict: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy_cannot_beat_the_bound(self) -> OptimalityGap:
        # 上界必须成立：任何策略的关键未满足都不能少于最优值
        if self.policy_critical_unmet < self.optimal_critical_unmet:
            raise ValueError("策略的关键未满足少于松弛最优，说明上界算错了")
        if self.policy_total_unmet < self.optimal_total_unmet:
            raise ValueError("策略的总未满足少于松弛最优，说明上界算错了")
        return self


_ASSUMPTIONS = [
    "松弛最优：不计跨区调运时延（真实台账为 40 分钟）与车辆通行约束",
    "需求量与本地库存取自推演产出的 DistrictSignal，不做二次折算",
    "关键片区 = 已过封路阈值或存在危险源；其余片区不占用关键片区的库存",
]


def _is_critical(signal: DistrictSignal) -> bool:
    return bool(signal.closure_required) or signal.has_hazard


def optimal_allocation(signals: Mapping[str, DistrictSignal] | Sequence[DistrictSignal]) -> OptimalAllocation:
    """求"关键未满足 → 总未满足"字典序最优的分配上界。

    算法是"全局池 + 字典序"：先按关键片区优先把库存池分完，再分给非关键片区。
    与规则策略的关键区别是：非关键片区的库存**可以被征用**，这正是 P9"让位"想达到的效果，
    所以它给出的是上界而不是现状复述。
    """

    items = list(signals.values()) if isinstance(signals, Mapping) else list(signals)
    if not items:
        raise ValueError("没有决策输入，不产出分配上界")

    names = sorted({signal.district for signal in items})
    by_name = {signal.district: signal for signal in items}

    pool = sum(max(0, by_name[name].local_available) for name in names)
    order = sorted(names, key=lambda name: (not _is_critical(by_name[name]), name))
    allocation = {name: 0 for name in names}
    for name in order:
        take = min(by_name[name].demand, pool)
        allocation[name] = take
        pool -= take

    # 多拿的片区是受援方，少拿的片区是捐出方；按名称稳定配对
    surplus = {name: by_name[name].local_available - allocation[name] for name in names}
    donors = [(name, surplus[name]) for name in names if surplus[name] > 0]
    recipients = [(name, -surplus[name]) for name in names if surplus[name] < 0]
    transfers: list[TransferPlan] = []
    for recipient, need in recipients:
        for index, (donor, available) in enumerate(donors):
            if need <= 0:
                break
            move = min(available, need)
            if move <= 0:
                continue
            transfers.append(TransferPlan(from_district=donor, to_district=recipient, qty=move))
            donors[index] = (donor, available - move)
            need -= move

    critical_unmet = {
        name: max(0, by_name[name].demand - allocation[name])
        for name in names
        if _is_critical(by_name[name])
    }
    total_unmet = sum(max(0, by_name[name].demand - allocation[name]) for name in names)
    return OptimalAllocation(
        allocation_by_district=allocation,
        critical_unmet=critical_unmet,
        total_unmet=total_unmet,
        transfers=transfers,
        assumptions=list(_ASSUMPTIONS),
    )


def compare_with_policy(
    signals: Mapping[str, DistrictSignal] | Sequence[DistrictSignal],
    policy_allocation: Mapping[str, int],
    *,
    policy_version: str = "",
) -> OptimalityGap:
    """把某个策略的实际分配与上界对照，给出可写进报告的一句话结论。"""

    items = list(signals.values()) if isinstance(signals, Mapping) else list(signals)
    by_name = {signal.district: signal for signal in items}
    optimal = optimal_allocation(items)

    policy_critical = 0
    policy_total = 0
    for name, signal in by_name.items():
        allocated = policy_allocation.get(name, 0)
        unmet = max(0, signal.demand - allocated)
        policy_total += unmet
        if _is_critical(signal):
            policy_critical += unmet

    optimal_critical = sum(optimal.critical_unmet.values())
    gap_critical = policy_critical - optimal_critical
    gap_total = policy_total - optimal.total_unmet
    if gap_critical == 0 and gap_total == 0:
        verdict = "策略已达到松弛最优：关键片区与总未满足都不劣于上界"
    else:
        parts: list[str] = []
        if gap_critical:
            parts.append(f"关键片区多未满足 {gap_critical} 台")
        if gap_total:
            parts.append(f"总未满足多 {gap_total} 台")
        verdict = "策略距松弛最优还差：" + "、".join(parts)

    return OptimalityGap(
        policy_version=policy_version,
        policy_critical_unmet=policy_critical,
        optimal_critical_unmet=optimal_critical,
        policy_total_unmet=policy_total,
        optimal_total_unmet=optimal.total_unmet,
        verdict=verdict,
    )
