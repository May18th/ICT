"""P4 provenance: claim gate, hash chain, trace, replay.

The point of these tests is not "the ledger stores things". It is:

- a claim that cannot be located is refused at the door, not filtered later;
- tampering is *located*, not merely detected;
- a conclusion can be rebuilt from a minimal evidence set.
"""

from __future__ import annotations

import pytest

from decision_poc.provenance import (
    Claim,
    ClaimKind,
    ClaimStatus,
    ProvenanceLedger,
    RuleRef,
    UnsupportedReason,
    build_ledger,
    split_ref,
)

FP = {"AS-a": "a" * 64, "AS-b": "b" * 64, "AS-plan": "c" * 64}
SNAP = "f" * 64


def _ledger(**kwargs) -> ProvenanceLedger:
    payload = {"asset_fingerprints": FP, "knowledge_version": "kb-v1.0.0"}
    payload.update(kwargs)
    return ProvenanceLedger(**payload)


def _fact(claim_id: str = "CL-1", refs=("AS-a#/row:2",)) -> Claim:
    return Claim(
        claim_id=claim_id,
        text="R-101 积水 30cm",
        kind=ClaimKind.FACT,
        evidence_refs=list(refs),
        produced_by="city_extract:work_orders",
    )


# --- claim gate ----------------------------------------------------------


def test_fact_without_evidence_is_unsupported() -> None:
    ledger = _ledger()
    [check] = ledger.register([_fact(refs=())])
    assert check.status is ClaimStatus.UNSUPPORTED
    assert UnsupportedReason.NO_EVIDENCE.value in check.reasons


def test_fact_with_unknown_asset_is_unsupported() -> None:
    ledger = _ledger()
    [check] = ledger.register([_fact(refs=("AS-ghost#/row:1",))])
    assert check.status is ClaimStatus.UNSUPPORTED
    assert any(reason.startswith(UnsupportedReason.UNKNOWN_ASSET.value) for reason in check.reasons)


def test_malformed_ref_is_unsupported() -> None:
    ledger = _ledger()
    [check] = ledger.register([_fact(refs=("AS-a",))])
    assert check.status is ClaimStatus.UNSUPPORTED
    assert any(reason.startswith(UnsupportedReason.MALFORMED_REF.value) for reason in check.reasons)


def test_split_ref_rejects_incomplete_refs() -> None:
    with pytest.raises(ValueError):
        split_ref("AS-a")
    with pytest.raises(ValueError):
        split_ref("#/row:1")
    assert split_ref("AS-a#/row:1") == ("AS-a", "/row:1")


def test_inference_needs_two_refs() -> None:
    ledger = _ledger()
    claim = Claim(
        claim_id="CL-inf",
        text="排水能力不足导致积水",
        kind=ClaimKind.INFERENCE,
        evidence_refs=["AS-a#/row:2"],
        premise_claim_ids=["CL-1"],
        produced_by="deduction:rule-7",
    )
    ledger.register([_fact(), claim])
    verdict = ledger.check_of("CL-inf")
    assert verdict.status is ClaimStatus.UNSUPPORTED
    assert UnsupportedReason.INFERENCE_TOO_FEW_REFS.value in verdict.reasons


def test_inference_must_carry_all_premise_refs() -> None:
    ledger = _ledger()
    premise = _fact("CL-1", ("AS-a#/row:2",))
    inference = Claim(
        claim_id="CL-inf",
        text="因此需要移动泵",
        kind=ClaimKind.INFERENCE,
        evidence_refs=["AS-a#/row:9", "AS-b#/row:3"],
        premise_claim_ids=["CL-1"],
        produced_by="deduction:rule-7",
    )
    ledger.register([premise, inference])
    verdict = ledger.check_of("CL-inf")
    assert verdict.status is ClaimStatus.UNSUPPORTED
    assert any(
        reason.startswith(UnsupportedReason.INFERENCE_MISSING_PREMISE_REFS.value)
        for reason in verdict.reasons
    )


def test_inference_cannot_reuse_one_ref_twice() -> None:
    """同一 locator 写两遍不算两条证据 —— 否则"至少 2 条"这条闸门形同虚设。"""
    ledger = _ledger()
    premise = _fact("CL-1", ("AS-a#/row:2",))
    inference = Claim(
        claim_id="CL-inf",
        text="因此需要移动泵",
        kind=ClaimKind.INFERENCE,
        evidence_refs=["AS-a#/row:2", "AS-a#/row:2"],
        premise_claim_ids=["CL-1"],
        produced_by="deduction:rule-7",
    )
    ledger.register([premise, inference])
    verdict = ledger.check_of("CL-inf")
    assert verdict.status is ClaimStatus.UNSUPPORTED
    assert UnsupportedReason.INFERENCE_TOO_FEW_REFS.value in verdict.reasons


def test_inference_with_premise_refs_is_supported() -> None:
    ledger = _ledger()
    premise = _fact("CL-1", ("AS-a#/row:2",))
    inference = Claim(
        claim_id="CL-inf",
        text="因此需要移动泵",
        kind=ClaimKind.INFERENCE,
        evidence_refs=["AS-a#/row:2", "AS-b#/row:3"],
        premise_claim_ids=["CL-1"],
        produced_by="deduction:rule-7",
    )
    checks = ledger.register([premise, inference])
    assert [check.status for check in checks] == [ClaimStatus.SUPPORTED, ClaimStatus.SUPPORTED]
    assert ledger.check_of("CL-inf").resolved_asset_ids == ["AS-a", "AS-b"]


def test_inference_with_unknown_premise_is_unsupported() -> None:
    ledger = _ledger()
    inference = Claim(
        claim_id="CL-inf",
        text="x",
        kind=ClaimKind.INFERENCE,
        evidence_refs=["AS-a#/row:1", "AS-b#/row:1"],
        premise_claim_ids=["CL-missing"],
        produced_by="deduction:rule-7",
    )
    ledger.register([inference])
    assert any(
        reason.startswith(UnsupportedReason.UNKNOWN_PREMISE.value)
        for reason in ledger.check_of("CL-inf").reasons
    )


def test_rule_output_requires_known_rule_and_locatable_source() -> None:
    rules = {"RULE-1": RuleRef(rule_id="RULE-1", source_ref="AS-plan#/line:44")}
    ledger = _ledger(rules=rules)

    unknown = Claim(
        claim_id="CL-r1",
        text="按预案须先隔离风险源",
        kind=ClaimKind.RULE_OUTPUT,
        rule_id="RULE-404",
        produced_by="deduction:rule-404",
    )
    known = Claim(
        claim_id="CL-r2",
        text="按预案须先隔离风险源",
        kind=ClaimKind.RULE_OUTPUT,
        rule_id="RULE-1",
        produced_by="deduction:RULE-1",
    )
    ledger.register([unknown, known])

    assert any(
        reason.startswith(UnsupportedReason.UNKNOWN_RULE.value)
        for reason in ledger.check_of("CL-r1").reasons
    )
    assert ledger.check_of("CL-r2").status is ClaimStatus.SUPPORTED
    assert ledger.check_of("CL-r2").resolved_asset_ids == ["AS-plan"]


def test_rule_source_asset_must_exist_in_ledger() -> None:
    rules = {"RULE-9": RuleRef(rule_id="RULE-9", source_ref="AS-withdrawn#/line:1")}
    ledger = _ledger(rules=rules)
    claim = Claim(
        claim_id="CL-r9",
        text="x",
        kind=ClaimKind.RULE_OUTPUT,
        rule_id="RULE-9",
        produced_by="deduction:RULE-9",
    )
    ledger.register([claim])
    verdict = ledger.check_of("CL-r9")
    assert verdict.status is ClaimStatus.UNSUPPORTED
    assert any(
        reason.startswith(UnsupportedReason.RULE_SOURCE_NOT_LOCATABLE.value)
        for reason in verdict.reasons
    )


def test_citation_completeness_is_guarded() -> None:
    ledger = _ledger()
    ledger.register([_fact(), _fact("CL-bad", refs=())])
    assert ledger.citation_completeness() == 0.5


# --- append and chain ----------------------------------------------------


def test_append_refuses_unsupported_claims() -> None:
    ledger = _ledger()
    ledger.register([_fact(), _fact("CL-bad", refs=())])
    with pytest.raises(ValueError, match="EVIDENCE_INSUFFICIENT"):
        ledger.append(
            decision_id="DEC-1", claim_ids=["CL-1", "CL-bad"], semantic_snapshot_hash=SNAP
        )
    assert ledger.records() == []


def test_append_requires_claim_ids() -> None:
    ledger = _ledger()
    with pytest.raises(ValueError):
        ledger.append(decision_id="DEC-1", claim_ids=[], semantic_snapshot_hash=SNAP)


def test_chain_links_and_verifies() -> None:
    ledger = _ledger()
    ledger.register([_fact("CL-1"), _fact("CL-2", ("AS-b#/row:1",))])
    first = ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)
    second = ledger.append(decision_id="DEC-2", claim_ids=["CL-2"], semantic_snapshot_hash=SNAP)

    assert first.upstream_record_hash is None
    assert second.upstream_record_hash == first.record_hash
    assert first.asset_fingerprints == {"AS-a": FP["AS-a"]}
    report = ledger.verify()
    assert report.ok and report.checked == 2


def test_record_hash_is_deterministic() -> None:
    hashes = []
    for _ in range(2):
        ledger = _ledger()
        ledger.register([_fact()])
        record = ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)
        hashes.append((record.record_id, record.record_hash))
    assert hashes[0] == hashes[1]


def test_verify_locates_tampered_field_without_raising() -> None:
    ledger = _ledger()
    ledger.register([_fact("CL-1"), _fact("CL-2", ("AS-b#/row:1",))])
    first = ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)
    ledger.append(decision_id="DEC-2", claim_ids=["CL-2"], semantic_snapshot_hash=SNAP)

    tampered = ledger._records[0].model_copy(update={"decision_id": "DEC-999"})
    ledger._records[0] = tampered

    report = ledger.verify()
    assert not report.ok
    assert report.first_broken_record_id == first.record_id
    assert first.record_id in report.broken_record_ids
    assert any(reason.startswith("HASH_MISMATCH") for reason in report.reasons)


def test_verify_detects_broken_link() -> None:
    ledger = _ledger()
    ledger.register([_fact("CL-1"), _fact("CL-2", ("AS-b#/row:1",))])
    ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)
    ledger.append(decision_id="DEC-2", claim_ids=["CL-2"], semantic_snapshot_hash=SNAP)

    # 重封第二条记录的哈希，使自身校验通过，只剩链式关系被破坏
    second = ledger._records[1].model_copy(update={"upstream_record_hash": "0" * 64})
    ledger._records[1] = second.model_copy(update={"record_hash": second.computed_hash()})

    report = ledger.verify()
    assert not report.ok
    assert report.broken_record_ids == [second.record_id]
    assert any(reason.startswith("CHAIN_BROKEN") for reason in report.reasons)


# --- trace and replay ----------------------------------------------------


def test_trace_claim_returns_assets_rules_and_records() -> None:
    rules = {"RULE-1": RuleRef(rule_id="RULE-1", source_ref="AS-plan#/line:44")}
    ledger = _ledger(rules=rules)
    ledger.register(
        [
            _fact(),
            Claim(
                claim_id="CL-rule",
                text="按预案先隔离风险源",
                kind=ClaimKind.RULE_OUTPUT,
                rule_id="RULE-1",
                produced_by="deduction:RULE-1",
            ),
        ]
    )
    record = ledger.append(
        decision_id="DEC-1", claim_ids=["CL-1", "CL-rule"], semantic_snapshot_hash=SNAP
    )

    trace = ledger.trace_claim("CL-rule")
    assert trace.rule_id == "RULE-1"
    assert trace.rule_source_ref == "AS-plan#/line:44"
    assert trace.record_ids == [record.record_id]
    assert trace.decision_ids == ["DEC-1"]
    assert trace.semantic_snapshot_hash == SNAP
    assert [asset.asset_id for asset in trace.assets] == ["AS-plan"]
    assert trace.assets[0].locator == "/line:44"
    assert trace.assets[0].changed_since_record is False


def test_trace_detects_data_changed_after_recording() -> None:
    ledger = _ledger()
    ledger.register([_fact()])
    ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)

    # 新的数据版本：同一资产换了指纹，旧记录用 adopt 原样载入
    changed = dict(FP)
    changed["AS-a"] = "9" * 64
    later = _ledger(asset_fingerprints=changed)
    later.adopt(ledger.records(), [_fact()])

    assert later.verify().ok, "载入既有记录不得改变其封印"
    assert later.trace_claim("CL-1").assets[0].changed_since_record is True
    assert ledger.trace_claim("CL-1").assets[0].changed_since_record is False


def test_replay_bundle_is_minimal_and_sufficient() -> None:
    rules = {"RULE-1": RuleRef(rule_id="RULE-1", source_ref="AS-plan#/line:44")}
    ledger = _ledger(rules=rules)
    ledger.register(
        [
            _fact(),
            Claim(
                claim_id="CL-rule",
                text="x",
                kind=ClaimKind.RULE_OUTPUT,
                rule_id="RULE-1",
                produced_by="deduction:RULE-1",
            ),
        ]
    )
    ledger.append(
        decision_id="DEC-1",
        claim_ids=["CL-1", "CL-rule"],
        semantic_snapshot_hash=SNAP,
        processors=["city_extract@1.0", "deduction@1.0"],
    )
    bundle = ledger.replay("DEC-1")
    assert bundle.claim_ids == ["CL-1", "CL-rule"]
    assert set(bundle.asset_fingerprints) == {"AS-a", "AS-plan"}
    assert bundle.knowledge_version == "kb-v1.0.0"
    assert bundle.semantic_snapshot_hash == SNAP
    assert bundle.rule_ids == ["RULE-1"]
    assert bundle.processors == ["city_extract@1.0", "deduction@1.0"]


def test_replay_unknown_decision_raises() -> None:
    with pytest.raises(KeyError):
        _ledger().replay("DEC-none")


def test_affected_decisions_maps_changed_assets_to_conclusions() -> None:
    ledger = _ledger()
    ledger.register([_fact("CL-1"), _fact("CL-2", ("AS-b#/row:1",))])
    ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=SNAP)
    ledger.append(decision_id="DEC-2", claim_ids=["CL-2"], semantic_snapshot_hash=SNAP)

    assert ledger.affected_decisions(["AS-a"]) == ["DEC-1"]
    assert ledger.affected_decisions(["AS-b"]) == ["DEC-2"]
    assert ledger.affected_decisions(["AS-untouched"]) == []


# --- real dataset --------------------------------------------------------


def test_real_dataset_claim_traces_to_file_and_line() -> None:
    """端到端：真实资产 id + 真实定位，必须能回到文件与行号。"""
    from decision_poc.semantic import build_urban_semantic_store

    store = build_urban_semantic_store()
    triple = next(t for t in store.triples() if t.predicate.value == "maintained_by")
    ref = triple.evidence_refs[0]
    asset_id, locator = split_ref(ref)
    assert locator.startswith("A-03/")

    ledger = build_ledger()
    ledger.register(
        [
            Claim(
                claim_id="CL-real",
                text=f"{triple.subject_id} 由 {triple.object_id} 养护",
                kind=ClaimKind.FACT,
                evidence_refs=[ref],
                produced_by="city_extract@1.0",
            )
        ]
    )
    record = ledger.append(
        decision_id="DEC-real", claim_ids=["CL-real"], semantic_snapshot_hash=store.snapshot_hash()
    )
    trace = ledger.trace_claim("CL-real")
    assert trace.assets[0].asset_id == asset_id
    assert trace.assets[0].fingerprint
    assert trace.record_ids == [record.record_id]
    assert record.knowledge_version == "kb-v1.0.0"
    assert ledger.verify().ok
