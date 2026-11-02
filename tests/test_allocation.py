"""P9 决策层测试：分配、偏差、拒绝空输入、确定性。"""

from __future__ import annotations

import pytest

from decision_poc.allocation import (
    AllocationPolicy,
    AllocationUnavailable,
    DistrictSignal,
    case_deviation,
    cases_digest,
    decide,
    default_policy,
    evaluate_policy,
    load_cases,
)


def signals_a03_b01() -> list[DistrictSignal]:
    """实况决策输入：A-03 已过封路阈值且有燃气基坑，B-01 18cm 无危险源。"""
    return [
        DistrictSignal(
            district="A-03",
            depth_cm=30.0,
            closure_required=True,
            has_hazard=True,
            demand=5,
            local_available=4,
            world_required=7,
        ),
        DistrictSignal(
            district="B-01",
            depth_cm=18.0,
            closure_required=False,
            has_hazard=False,
            demand=2,
            local_available=2,
            world_required=7,
        ),
    ]


def test_default_policy_keeps_local_stock_and_reports_unmet() -> None:
    decision = decide(signals_a03_b01(), default_policy())
    assert decision.allocation_by_district == {"A-03": 4, "B-01": 2}
    assert decision.unmet_by_district == {"A-03": 1, "B-01": 0}
    assert decision.transfers == []
    assert any("A-03" in item and "仍缺 1 台" in item for item in decision.escalation)


def test_evolved_policy_moves_pump_to_critical_district() -> None:
    policy = AllocationPolicy(
        policy_version="v2",
        weight_closure=1.0,
        weight_hazard=0.25,
        transfer_penalty=0.1,
    )
    decision = decide(signals_a03_b01(), policy)
    assert decision.allocation_by_district == {"A-03": 5, "B-01": 1}
    assert decision.unmet_by_district == {"A-03": 0, "B-01": 1}
    assert len(decision.transfers) == 1
    transfer = decision.transfers[0]
    assert (transfer.from_district, transfer.to_district, transfer.qty) == ("B-01", "A-03", 1)
    # 跨区时延来自数据集台账，不是叙事
    assert transfer.delay_min == 40
    assert decision.priority_order == ["A-03", "B-01"]


def test_no_decision_inputs_refuses_to_allocate() -> None:
    with pytest.raises(AllocationUnavailable):
        decide({})


def test_critical_district_is_never_drained() -> None:
    signals = [
        DistrictSignal(district="A-03", depth_cm=30.0, closure_required=True, has_hazard=True, demand=5, local_available=4),
        DistrictSignal(district="B-01", depth_cm=30.0, closure_required=True, has_hazard=True, demand=5, local_available=4),
    ]
    decision = decide(signals, AllocationPolicy(weight_closure=1.0, weight_hazard=1.0, transfer_penalty=0.0))
    assert decision.transfers == []
    assert decision.allocation_by_district == {"A-03": 4, "B-01": 4}


def test_transfer_penalty_blocks_pointless_swap() -> None:
    """两片区同分且都不关键时，调拨没有优先序依据，门槛必须拦住。"""
    signals = [
        DistrictSignal(district="A-03", depth_cm=18.0, closure_required=False, has_hazard=False, demand=5, local_available=4),
        DistrictSignal(district="B-01", depth_cm=18.0, closure_required=False, has_hazard=False, demand=2, local_available=2),
    ]
    decision = decide(signals, AllocationPolicy(weight_closure=1.0, transfer_penalty=0.1))
    assert decision.transfers == []
    assert decision.allocation_by_district == {"A-03": 4, "B-01": 2}


def test_allocation_is_deterministic() -> None:
    policy = AllocationPolicy(weight_closure=1.0, weight_hazard=0.25, transfer_penalty=0.1)
    first = decide(signals_a03_b01(), policy).model_dump(mode="json")
    second = decide(signals_a03_b01(), policy).model_dump(mode="json")
    assert first == second


def test_ground_truth_cases_are_self_consistent() -> None:
    """关键片区未满足量必须与分配向量一致，否则 ground truth 自身就是坏的。"""
    cases = load_cases()
    assert len(cases) == 12
    assert {case.split for case in cases} == {"train", "holdout"}
    for case in cases:
        demand = {signal.district: signal.demand for signal in case.signals}
        for name, unmet in case.expect.critical_unmet.items():
            assert unmet == demand[name] - case.expect.allocation_by_district[name], case.case_id
        for name, allocated in case.expect.allocation_by_district.items():
            assert 0 <= allocated <= demand[name], case.case_id
        assert sum(case.expect.allocation_by_district.values()) <= sum(
            signal.local_available for signal in case.signals
        ), f"{case.case_id} 分配量超过库存"


def test_every_case_has_a_documented_basis() -> None:
    for case in load_cases():
        assert case.basis, f"{case.case_id} 缺依据"
        assert all(item.strip() for item in case.basis)


def test_hand_crafted_final_policy_matches_ground_truth() -> None:
    policy = AllocationPolicy(
        weight_closure=1.0,
        weight_hazard=0.25,
        transfer_penalty=0.1,
    )
    evaluation = evaluate_policy(policy, load_cases())
    assert evaluation.deviation == 0.0, [
        (item.case_id, item.notes) for item in evaluation.case_results if item.deviation
    ]
    assert evaluation.missed_transfers == 0


def test_baseline_policy_deviates_and_metrics_count() -> None:
    evaluation = evaluate_policy(default_policy(), load_cases())
    assert evaluation.deviation > 0.0
    assert evaluation.missed_transfers == evaluation.expected_transfers
    assert evaluation.critical_unmet_units > evaluation.expected_critical_unmet_units


def test_deviation_dimensions_are_bounded() -> None:
    for case in load_cases():
        deviation = case_deviation(decide(case.signals, default_policy()), case)
        for name, value in deviation.dimensions.items():
            assert 0.0 <= value <= 1.0, (case.case_id, name, value)
        assert 0.0 <= deviation.deviation <= 1.0


def test_case_digest_changes_when_feedback_changes() -> None:
    cases = load_cases()
    digest = cases_digest(cases)
    tampered = list(cases)
    tampered[0] = tampered[0].model_copy(
        update={"expect": tampered[0].expect.model_copy(update={"priority_order": ["B-01", "A-03"]})}
    )
    assert cases_digest(tampered) != digest
