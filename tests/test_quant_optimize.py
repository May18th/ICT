"""运筹上界：回答"规则策略距最优差多少"，而不是"我们的策略就是最优"。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.deduction import DistrictSignal
from decision_poc.quant.optimize import (
    OptimalityGap,
    compare_with_policy,
    optimal_allocation,
)

A03 = DistrictSignal(
    district="A-03",
    depth_cm=30.0,
    closure_required=True,
    has_hazard=True,
    demand=5,
    local_available=4,
    world_required=7,
)
B01 = DistrictSignal(
    district="B-01",
    depth_cm=18.0,
    closure_required=False,
    has_hazard=False,
    demand=2,
    local_available=2,
    world_required=7,
)


def test_upper_bound_serves_critical_district_first() -> None:
    optimal = optimal_allocation({"A-03": A03, "B-01": B01})
    assert optimal.allocation_by_district == {"A-03": 5, "B-01": 1}
    assert optimal.critical_unmet == {"A-03": 0}
    assert optimal.total_unmet == 1


def test_upper_bound_records_the_transfer_it_would_need() -> None:
    optimal = optimal_allocation({"A-03": A03, "B-01": B01})
    assert len(optimal.transfers) == 1
    transfer = optimal.transfers[0]
    assert (transfer.from_district, transfer.to_district, transfer.qty) == ("B-01", "A-03", 1)
    assert optimal.assumptions


def test_upper_bound_rejects_empty_signals() -> None:
    with pytest.raises(ValueError):
        optimal_allocation({})


def test_baseline_policy_is_one_unit_behind_the_bound() -> None:
    gap = compare_with_policy(
        {"A-03": A03, "B-01": B01},
        {"A-03": 4, "B-01": 2},
        policy_version="flood-response-v1.0",
    )
    assert gap.policy_critical_unmet == 1
    assert gap.optimal_critical_unmet == 0
    assert gap.policy_total_unmet == 1
    assert gap.optimal_total_unmet == 1
    assert "关键片区多未满足 1 台" in gap.verdict


def test_evolved_policy_reaches_the_bound() -> None:
    gap = compare_with_policy(
        {"A-03": A03, "B-01": B01},
        {"A-03": 5, "B-01": 1},
        policy_version="flood-response-v1.2",
    )
    assert gap.policy_critical_unmet == gap.optimal_critical_unmet
    assert gap.policy_total_unmet == gap.optimal_total_unmet
    assert "已达到松弛最优" in gap.verdict


def test_gap_rejects_a_policy_that_beats_the_bound() -> None:
    with pytest.raises(ValidationError):
        OptimalityGap(
            policy_critical_unmet=0,
            optimal_critical_unmet=1,
            policy_total_unmet=0,
            optimal_total_unmet=0,
            verdict="不可能",
        )


def test_surplus_supply_leaves_nothing_unmet() -> None:
    rich = {name: signal.model_copy(update={"local_available": 10}) for name, signal in
            {"A-03": A03, "B-01": B01}.items()}
    optimal = optimal_allocation(rich)
    assert optimal.total_unmet == 0
    assert optimal.critical_unmet == {"A-03": 0}
