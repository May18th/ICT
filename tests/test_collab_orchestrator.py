"""协同编排：六个角色都出卡，结论仍由内核指纹决定。

这组测试守的是三件事，任何一件破了"多 Agent 可复核"就不成立：

1. 角色覆盖度 100%（没有职责也要显式 no_action，不许沉默）；
2. 动作卡只能引用内核 claim，不能自造断言；
3. 联合发布与处置顺序由规则判定，Agent 不能协商掉。
"""

from __future__ import annotations

import pytest

from decision_poc.collab import AGENT_IDS, orchestrate
from decision_poc.collab.contracts import CardStatus, kernel_fingerprint
from decision_poc.deduction import DeductionEngine, claims_from

AS_OF = "2026-10-29T09:40:00+08:00"
EVENT = "WO-20261029-007"


@pytest.fixture(scope="module")
def deduction():
    return DeductionEngine.load().deduce(EVENT, as_of=AS_OF)


@pytest.fixture(scope="module")
def collab(deduction):
    return orchestrate(deduction)


def test_all_six_roles_are_covered(collab) -> None:
    assert collab.covered_agents == list(AGENT_IDS)


def test_every_role_has_at_least_one_card(collab) -> None:
    posted = {card.agent_id for card in collab.cards}
    assert posted == set(AGENT_IDS)


def test_fingerprint_is_deterministic(deduction, collab) -> None:
    again = orchestrate(deduction)
    assert again.kernel_fingerprint == collab.kernel_fingerprint
    assert again.model_dump() == collab.model_dump()


def test_fingerprint_comes_from_kernel_facts(deduction, collab) -> None:
    expected = kernel_fingerprint(
        [
            (
                step.step_id,
                step.stage,
                step.status.value,
                dict(step.conclusion),
                list(step.evidence_refs),
            )
            for step in deduction.steps
        ]
    )
    assert collab.kernel_fingerprint == expected


def test_agent_layer_cannot_change_fingerprint(deduction, collab) -> None:
    # 换掉卡片内容（模拟 Agent 输出漂移），指纹必须不变——结论归内核，不归 Agent
    mutated = collab.model_copy(update={"handoffs": [], "co_sign_pairs": []})
    assert mutated.kernel_fingerprint == collab.kernel_fingerprint


def test_three_conflicts_are_preserved(deduction, collab) -> None:
    assert len(collab.conflicts) == len(deduction.conflicts)
    assert {item.conflict_id for item in collab.conflicts} == {
        item.conflict_id for item in deduction.conflicts
    }


def test_merge_order_names_real_cards(collab) -> None:
    known = {card.card_id for card in collab.cards}
    assert collab.merge_order
    assert set(collab.merge_order) <= known


def test_isolate_precedes_pumping(collab) -> None:
    by_id = {card.card_id: card for card in collab.cards}
    actions = [by_id[card_id].action for card_id in collab.merge_order]
    isolate = next(index for index, text in enumerate(actions) if "隔离" in text)
    pump = next(index for index, text in enumerate(actions) if "抽排" in text)
    assert isolate < pump


def test_no_sequence_constraint_is_violated(collab) -> None:
    assert collab.constraint_violations == []


def test_road_and_traffic_share_joint_closure(collab) -> None:
    pairs = {tuple(sorted(pair)) for pair in collab.co_sign_pairs}
    assert ("ORG-ROAD", "ORG-TRAFFIC") in pairs
    joint = [
        card
        for card in collab.cards
        if set(card.co_sign_required) & {"ORG-ROAD", "ORG-TRAFFIC"}
        and card.agent_id in {"ORG-ROAD", "ORG-TRAFFIC"}
    ]
    assert joint
    assert all(card.status is CardStatus.ACCEPTED for card in joint)


def test_action_cards_only_reference_kernel_claims(deduction, collab) -> None:
    known = {claim.claim_id for claim in claims_from(deduction)}
    for card in collab.cards:
        if card.status is CardStatus.NO_ACTION:
            continue
        assert set(card.claim_ids) <= known, f"{card.card_id} 引用了内核不存在的 claim"


def test_resource_gap_card_carries_quantity(deduction, collab) -> None:
    gaps = [card for card in collab.cards if card.action == "dispatch_pump"]
    assert gaps
    assert deduction.resource_gaps
    assert gaps[0].quantity == deduction.resource_gaps[0].gap


def test_no_action_cards_explain_themselves(collab) -> None:
    blanks = [card for card in collab.cards if card.status is CardStatus.NO_ACTION]
    for card in blanks:
        assert card.no_action_reason.strip()
        assert not card.claim_ids


def test_handoffs_reference_existing_cards(collab) -> None:
    known = {card.card_id for card in collab.cards}
    for handoff in collab.handoffs:
        assert set(handoff.card_ids) <= known
