"""组合候选：同时改动 ≥2 个参数的候选，如何被选优与门禁拦截。"""

from __future__ import annotations

import pytest

from decision_poc.allocation import default_policy
from decision_poc.deduction import DistrictSignal
from decision_poc.experience import ReviewDecision, ReviewRecord
from decision_poc.policy_evolution import (
    COMBINATION_RECIPES,
    combination_patches,
    evolve,
)

SIGNALS = [
    DistrictSignal(
        district="A-03",
        depth_cm=30.0,
        closure_required=True,
        has_hazard=True,
        demand=5,
        local_available=4,
    ),
    DistrictSignal(
        district="B-01",
        depth_cm=18.0,
        closure_required=False,
        has_hazard=False,
        demand=2,
        local_available=2,
    ),
]


def test_combination_patches_touch_at_least_two_parameters() -> None:
    policy = default_policy("v1")
    patches = combination_patches(policy)
    assert patches, "默认策略上应当有可用的组合候选"
    knob_names = {"weight_closure", "weight_hazard", "weight_depth", "transfer_penalty"}
    for patch in patches:
        assert len(patch) >= 2
        assert set(patch) <= knob_names
        assert all(value >= 0 for value in patch.values())
    # 每条组合候选都能直接套到策略上（值在合法范围内）
    for patch in patches:
        updated = policy.with_updates(patch)
        assert updated.policy_version == policy.policy_version

    # 已经把所有机制都打开的策略上，应没有"还要改两个参数"的组合候选
    tuned = policy.model_copy(
        update={
            "policy_version": "v-tuned",
            "weight_closure": 1.0,
            "weight_hazard": 0.25,
            "transfer_penalty": 0.1,
        }
    )
    assert combination_patches(tuned) == []


def test_combination_candidate_is_promoted_and_labelled() -> None:
    report = evolve(
        include_real_event=False,
        use_memory=False,
        allow_combinations=True,
        max_rounds=3,
    )
    targets = [(item.target_dimension, item.action) for item in report.rounds]
    assert ("combination", "promoted") in targets
    assert report.final_train.deviation < report.initial_train.deviation
    # 被采纳的组合候选确实改了两个以上参数
    combos = [item for item in report.rounds if item.target_dimension == "combination"]
    assert combos and all(len(item.params_delta) >= 2 for item in combos)


def test_combination_candidate_can_be_rejected_by_review_gate() -> None:
    # 评审要求 B-01 保留 2 台（与进化后的 A-03=5 / B-01=1 相反）：
    # 组合候选会让 B-01 只剩 1 台，门禁必须以"违反人工评审"拒绝它。
    review = ReviewRecord(
        review_id="REV-COMBO-BLOCK",
        reviewer_role="组合候选拦截演示",
        reviewer_note="角色扮演，用于演示门禁拦截组合候选，非真实部门批复",
        event_id="WO-20261029-007",
        as_of="2026-10-29T09:40:00+08:00",
        policy_version_reviewed="v1",
        decision=ReviewDecision.MODIFY,
        reason="要求 B-01 保留 2 台，不允许为了 A-03 把 B-01 抽空",
        signals=SIGNALS,
        expected_allocation={"A-03": 4, "B-01": 2},
    )
    report = evolve(
        reviews=[review],
        include_real_event=False,
        use_memory=False,
        allow_combinations=True,
        max_rounds=2,
    )
    assert report.rounds, "至少应有一轮尝试"
    rejected = [item for item in report.rounds if item.action == "rejected"]
    assert rejected, "组合候选应当被评审门禁拒绝"
    assert any("评审" in item.reason for item in rejected)
    assert report.final_label == "flood-response-v1.0"


def test_default_evolution_numbers_are_unchanged() -> None:
    """默认（不开组合候选）的 P9 数字保持冻结值，避免历史证据漂移。"""
    report = evolve(include_real_event=False, use_memory=False)
    assert report.final_label == "flood-response-v1.2"
    assert len(report.rounds) == 3
    assert [item.action for item in report.rounds] == ["promoted", "promoted", "rejected"]
    assert round(report.initial_train.deviation, 4) == 0.2264
    assert round(report.final_train.deviation, 4) == 0.0
    assert round(report.initial_holdout.deviation, 4) == 0.2182
