"""协同层契约：Agent 编不出断言，也发不出指向空气的交接。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.collab.contracts import (
    ActionCard,
    CardStatus,
    CollabResult,
    RoleHandoff,
    kernel_fingerprint,
)


def _card(**overrides: object) -> ActionCard:
    data: dict[str, object] = {
        "card_id": "AC-S1-ORG-DRAIN",
        "agent_id": "ORG-DRAIN",
        "stage": "action",
        "action": "清掏雨水口",
        "claim_ids": ["CL-S1"],
        "evidence_refs": ["drainage_facilities.csv#row:2"],
    }
    data.update(overrides)
    return ActionCard(**data)  # type: ignore[arg-type]


def test_card_defaults_to_proposed() -> None:
    card = _card()
    assert card.status is CardStatus.PROPOSED
    assert card.quantity is None


def test_card_requires_claim() -> None:
    with pytest.raises(ValidationError):
        _card(claim_ids=[])


def test_card_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        _card(evidence_refs=[])


def test_card_rejects_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        _card(stage="freeform")


def test_no_action_requires_reason() -> None:
    with pytest.raises(ValidationError):
        _card(status=CardStatus.NO_ACTION, claim_ids=[], evidence_refs=[])


def test_no_action_must_not_carry_claim() -> None:
    with pytest.raises(ValidationError):
        _card(
            status=CardStatus.NO_ACTION,
            no_action_reason="无职责",
            claim_ids=["CL-S1"],
        )


def test_rejected_requires_reason() -> None:
    with pytest.raises(ValidationError):
        _card(status=CardStatus.REJECTED)


def test_only_rejected_carries_rejection_reason() -> None:
    with pytest.raises(ValidationError):
        _card(status=CardStatus.ACCEPTED, rejection_reason="不该有")


def _collab(**overrides: object) -> CollabResult:
    data: dict[str, object] = {
        "event_id": "WO-20261029-007",
        "cards": [_card()],
        "merge_order": ["AC-S1-ORG-DRAIN"],
        "kernel_fingerprint": "deadbeefdeadbeef",
        "snapshot_hash": "s" * 64,
        "rules_version": "urban-rules-1.1",
    }
    data.update(overrides)
    return CollabResult(**data)  # type: ignore[arg-type]


def test_merge_order_must_name_real_cards() -> None:
    with pytest.raises(ValidationError):
        _collab(merge_order=["AC-NOT-EXIST"])


def test_handoff_must_name_real_cards() -> None:
    with pytest.raises(ValidationError):
        _collab(
            handoffs=[
                RoleHandoff(
                    handoff_id="HO-1",
                    from_agent="EMERG-COORD",
                    to_agent="ORG-DRAIN",
                    card_ids=["AC-NOT-EXIST"],
                )
            ]
        )


def test_fingerprint_is_order_and_duplicate_insensitive() -> None:
    first = kernel_fingerprint(
        [
            ("S1", "observed", "supported", {"depth_cm": "30"}, ["a.csv#row:1", "b.csv#row:2"]),
            ("S2", "action", "supported", {"order": "2"}, ["b.csv#row:2"]),
        ]
    )
    second = kernel_fingerprint(
        [
            ("S1", "observed", "supported", {"depth_cm": "30"}, ["b.csv#row:2", "a.csv#row:1"]),
            ("S2", "action", "supported", {"order": "2"}, ["b.csv#row:2"]),
        ]
    )
    assert first == second
    assert len(first) == 16
