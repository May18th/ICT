"""P9.1 跨会话沉淀测试：场景签名、分层记忆、warm start、人工评审用例。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.allocation import (
    AllocationPolicy,
    case_deviation,
    decide,
    evaluate_policy,
    load_cases,
)
from decision_poc.experience import (
    DEFAULT_MEMORY_PATH,
    DEFAULT_REVIEWS_PATH,
    DecisionMemory,
    MemoryScope,
    ReviewDecision,
    ReviewRecord,
    ScenarioSignature,
    load_reviews,
    make_entry,
    signature_for,
)


def _entry(
    *,
    scope: MemoryScope = MemoryScope.AGENT,
    label: str = "flood-response-v1.2",
    deviation: float = 0.0,
    signature: ScenarioSignature | None = None,
    params: dict | None = None,
) -> object:
    cases = load_cases()
    return make_entry(
        scope=scope,
        signature=signature or signature_for(cases),
        label=label,
        policy_version="v3",
        params=params
        or {"weight_closure": 1.0, "weight_hazard": 0.25, "transfer_penalty": 0.1},
        outcome={"deviation": deviation, "missed_transfers": 0.0, "critical_unmet_units": 6.0},
        feedback_digest="d" * 64,
        event_ids=["T1", "T2"],
        review_ids=["REV-001"],
    )


# --- 场景签名 -------------------------------------------------------------


def test_signature_is_deterministic_and_self_similar() -> None:
    cases = load_cases()
    first = signature_for(cases)
    second = signature_for(load_cases())
    assert first.key() == second.key()
    assert first.similarity(second) == 1.0
    assert first.districts == ["A-03", "B-01"]
    assert first.cases == len(cases)
    assert 0.0 <= first.shortage_ratio <= 1.0


def test_signature_of_a_subset_is_less_similar() -> None:
    cases = load_cases()
    full = signature_for(cases)
    subset = signature_for([case for case in cases if case.case_id.startswith("H")])
    assert 0.0 < subset.similarity(full) < 1.0


# --- 分层记忆 -------------------------------------------------------------


def test_memory_append_only_and_idempotent() -> None:
    memory = DecisionMemory()
    entry = _entry()
    memory.remember(entry)
    memory.remember(entry)
    assert len(memory) == 1
    assert memory.entries[0].entry_id == entry.entry_id


def test_recall_filters_by_similarity_and_returns_deterministic_order() -> None:
    cases = load_cases()
    memory = DecisionMemory()
    memory.remember(_entry(deviation=0.2))
    memory.remember(_entry(label="flood-response-v1.1", deviation=0.1))
    hits = memory.recall(signature_for(cases), min_similarity=0.99)
    assert hits, "同一场景应能召回"
    assert all(item.similarity == 1.0 for item in hits)
    other = signature_for([case for case in cases if case.split == "holdout"])
    assert memory.recall(other, min_similarity=0.999) == []


def test_warm_start_prefers_the_lowest_verified_deviation() -> None:
    cases = load_cases()
    memory = DecisionMemory()
    memory.remember(_entry(label="v1.1", deviation=0.047143))
    best = _entry(label="v1.2", deviation=0.0)
    memory.remember(best)
    warm = memory.warm_start(signature_for(cases))
    assert warm is not None
    assert warm.entry.entry_id == best.entry_id


def test_warm_start_is_none_without_matching_experience() -> None:
    memory = DecisionMemory()
    assert memory.warm_start(signature_for(load_cases())) is None


def test_scope_precedence_prefers_agent_experience() -> None:
    cases = load_cases()
    memory = DecisionMemory()
    tenant = _entry(scope=MemoryScope.TENANT, deviation=0.0)
    agent = _entry(scope=MemoryScope.AGENT, deviation=0.2)
    memory.remember(tenant)
    memory.remember(agent)
    hits = memory.recall(signature_for(cases), min_similarity=0.99)
    # 相似度相同，Agent 层的经验优先（更具体）
    assert [item.entry.scope for item in hits][:2] == [MemoryScope.AGENT, MemoryScope.TENANT]


def test_memory_persistence_round_trip(tmp_path) -> None:
    memory = DecisionMemory()
    memory.remember(_entry(deviation=0.0))
    target = tmp_path / "memory.json"

    memory.save(target)
    loaded = DecisionMemory.load(target)

    assert len(loaded) == len(memory)
    assert loaded.entries[0].entry_id == memory.entries[0].entry_id
    assert loaded.warm_start(signature_for(load_cases())) is not None


def test_memory_load_of_missing_file_is_empty() -> None:
    assert len(DecisionMemory.load(".definitely-not-there.json")) == 0
    assert DEFAULT_MEMORY_PATH.name == "decision_memory.json"


# --- 人工评审 -------------------------------------------------------------


def test_reviews_are_loaded_from_the_sample_file() -> None:
    reviews = load_reviews()
    assert len(reviews) == 1
    review = reviews[0]
    assert review.review_id == "REV-001"
    assert review.decision is ReviewDecision.MODIFY
    assert review.expected_allocation == {"A-03": 5, "B-01": 1}
    # 口径必须写明是角色扮演，避免被当成真实部门批复
    assert "角色扮演" in review.reviewer_note
    assert DEFAULT_REVIEWS_PATH.is_file()


def test_modify_review_requires_an_allocation() -> None:
    review = load_reviews()[0]
    payload = review.model_dump(mode="json")
    payload.pop("expected_allocation")
    with pytest.raises(ValidationError):
        ReviewRecord.model_validate(payload)


def test_review_becomes_a_self_contained_case() -> None:
    review = load_reviews()[0]
    case = review.to_case()
    assert case.split == "review"
    assert case.case_id == "R-REV-001"
    assert case.expect.allocation_by_district == {"A-03": 5, "B-01": 1}
    assert case.expect.critical_unmet == {}
    assert any("角色扮演" in line for line in case.basis)
    assert [signal.district for signal in case.signals] == ["A-03", "B-01"]


def test_review_case_only_judges_allocation() -> None:
    """评审只对谁拿几台负责：transfers / order 两个维度在评审用例上必须为 0。"""
    review = load_reviews()[0]
    case = review.to_case()
    policy = AllocationPolicy(weight_closure=1.0, weight_hazard=0.25, transfer_penalty=0.1)
    deviation = case_deviation(decide(case.signals, policy), case)
    assert deviation.dimensions["transfer"] == 0.0
    assert deviation.dimensions["order"] == 0.0
    assert deviation.deviation == 0.0


def test_review_case_is_violated_by_the_baseline_policy() -> None:
    """评审必须真的能拦住 v1，否则门禁形同虚设。"""
    review = load_reviews()[0]
    evaluation = evaluate_policy(AllocationPolicy(), [review.to_case()])
    assert evaluation.deviation > 0.1
    assert evaluation.missed_transfers == 0
