"""P11 锚定包的测试：承诺能不能被复核、原文能不能被伪造、链断没断。

锚定最常见的退化是"写了一个哈希就算审计"：链上哈希与链下原文各自漂移，谁也发现不了。
这里的测试全部围绕"改一个字节就必须被发现"，以及"没有墙钟时间，同一决策两次提交同哈希"。
"""

from __future__ import annotations

import json

import pytest

from decision_poc.anchoring import (
    ANCHOR_SCHEMA,
    AnchorLog,
    bundle_from_report,
    canonical_hash,
)
from decision_poc.skills import PipelineReport, SkillPipeline, SkillRuntime

EVENT = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
OTHER_EVENT = "WO-20261026-013"
OTHER_AS_OF = "2026-10-26T16:00:00+08:00"


@pytest.fixture(scope="module")
def runtime() -> SkillRuntime:
    return SkillRuntime()


@pytest.fixture(scope="module")
def report(runtime: SkillRuntime) -> PipelineReport:
    return SkillPipeline(runtime).run(EVENT, AS_OF, caller_labels=["ORG-EMERG"], persist=False)


@pytest.fixture()
def anchored(report: PipelineReport) -> tuple[AnchorLog, str]:
    log = AnchorLog()
    record = log.commit(bundle_from_report(report))
    return log, record.anchor_id


def test_bundle_requires_an_audited_decision(runtime: SkillRuntime) -> None:
    unaudited = SkillPipeline(runtime, stages=["understand", "deduce"]).run(
        EVENT, AS_OF, caller_labels=["ORG-EMERG"], persist=False
    )
    with pytest.raises(ValueError, match="audited"):
        bundle_from_report(unaudited)


def test_anchor_records_only_hashes_and_metadata(report: PipelineReport) -> None:
    bundle = bundle_from_report(report)
    log = AnchorLog()
    record = log.commit(bundle)
    dumped = record.model_dump(mode="json")
    # 链上只有承诺：不应出现结论正文、动作名或证据定位
    text = json.dumps(dumped, ensure_ascii=False)
    for action in bundle.action.action_order:
        assert action not in text
    assert set(dumped) == {
        "anchor_id",
        "decision_id",
        "observation_hash",
        "evidence_hash",
        "action_hash",
        "metadata",
        "upstream_anchor_hash",
        "anchor_hash",
    }
    assert record.metadata["schema"] == ANCHOR_SCHEMA
    # 元数据里不能有墙钟时间，否则同一决策两次提交哈希不同
    assert not any(key in record.metadata for key in ("timestamp", "created_at", "time"))


def test_commit_then_verify_and_prove(report: PipelineReport) -> None:
    log = AnchorLog()
    record = log.commit(bundle_from_report(report))
    verify = log.verify_offchain()
    assert verify.ok and verify.offchain_ok and verify.checked == 1
    assert set(log.prove(record.anchor_id).values()) == {True}


def test_two_decisions_form_a_chain(runtime: SkillRuntime, anchored: tuple[AnchorLog, str]) -> None:
    log, first_id = anchored
    other = SkillPipeline(runtime).run(
        OTHER_EVENT, OTHER_AS_OF, caller_labels=["ORG-DRAIN"], persist=False
    )
    second = log.commit(bundle_from_report(other))
    assert second.upstream_anchor_hash == log.records()[0].anchor_hash
    assert log.verify().checked == 2
    assert log.verify().ok


def test_offchain_tampering_is_detected(anchored: tuple[AnchorLog, str]) -> None:
    log, anchor_id = anchored
    bundle = log.bundle(anchor_id)
    bundle.action.action_order = [*bundle.action.action_order, "未获批的高影响动作"]
    log.replace_bundle(anchor_id, bundle)
    verify = log.verify_offchain()
    assert verify.ok, "链本身没断：被改的是链下原文"
    assert not verify.offchain_ok
    assert verify.tamper_detected
    assert any("OFFCHAIN_MISMATCH" in reason for reason in verify.reasons)
    assert log.prove(anchor_id)["action"] is False


def test_chain_tampering_is_localised(anchored: tuple[AnchorLog, str]) -> None:
    log, anchor_id = anchored
    tampered = log.records()[0].model_copy(update={"action_hash": "0" * 64})
    log._records[0] = tampered
    verify = log.verify()
    assert not verify.ok
    assert verify.first_broken_anchor_id == anchor_id
    assert any("HASH_MISMATCH" in reason for reason in verify.reasons)


def test_verified_after_restore(anchored: tuple[AnchorLog, str]) -> None:
    log, anchor_id = anchored
    original = log.bundle(anchor_id)
    broken = original.model_copy(deep=True)
    broken.evidence.claims = []
    log.replace_bundle(anchor_id, broken)
    assert not log.verify_offchain().offchain_ok
    log.replace_bundle(anchor_id, original)
    assert log.verify_offchain().offchain_ok


def test_hashes_are_deterministic(report: PipelineReport) -> None:
    bundle = bundle_from_report(report)
    first = AnchorLog().commit(bundle)
    again = bundle_from_report(report)
    second = AnchorLog().commit(again)
    assert first.anchor_hash == second.anchor_hash
    assert first.observation_hash == second.observation_hash
    assert canonical_hash(again.observation.model_dump(mode="json")) == first.observation_hash


def test_save_and_load_roundtrip(tmp_path, report: PipelineReport) -> None:
    log = AnchorLog()
    record = log.commit(bundle_from_report(report))
    log.save(tmp_path)
    assert (tmp_path / "anchors.jsonl").is_file()
    assert (tmp_path / "offchain" / f"{record.anchor_id}.json").is_file()
    reloaded = AnchorLog.load(tmp_path)
    assert len(reloaded.records()) == 1
    verify = reloaded.verify_offchain()
    assert verify.ok and verify.offchain_ok
    assert set(reloaded.prove(record.anchor_id).values()) == {True}


def test_missing_offchain_bundle_is_reported(report: PipelineReport) -> None:
    log = AnchorLog()
    record = log.commit(bundle_from_report(report))
    log._bundles.clear()  # 模拟链下原文丢失
    verify = log.verify_offchain()
    assert verify.ok, "链没断"
    assert not verify.offchain_ok
    assert record.anchor_id in verify.offchain_mismatches
    assert any("OFFCHAIN_MISSING" in reason for reason in verify.reasons)
