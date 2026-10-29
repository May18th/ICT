"""P4 end-to-end: conclusion -> evidence -> version, and back again.

Two directions are proven here:

- forward（可追溯）: a conclusion recorded today can be walked back to a file,
  a locator, a fingerprint and a knowledge version;
- backward（影响分析）: after the data changes, the system names which recorded
  conclusions must be re-evaluated -- using locators, because content-addressed
  asset ids change with every edit.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from decision_poc.city_extract import build_city_bundle
from decision_poc.knowledge_evolution import (
    KnowledgeRegistry,
    get_knowledge_diff,
    snapshot_from_bundle,
)
from decision_poc.provenance import Claim, ClaimKind, build_ledger, split_ref
from decision_poc.semantic import SemanticQuery, build_urban_semantic_store
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

AS_OF = datetime.fromisoformat("2026-10-29T12:00:00+08:00")


@pytest.fixture(scope="module")
def store():
    return build_urban_semantic_store()


def _triple_of(store, twin_id: str, predicate: str):
    """按谓词取与某孪生对象相关的关系（方向不固定，如 maintained_by 主语是部门）。"""
    return next(
        triple
        for triple in store.triples()
        if triple.predicate.value == predicate and triple.involves(twin_id)
    )


def _record_two_decisions(store):
    """一条结论跨两个文件（工单 + 设施台账），另一条只依赖道路台账（对照组）。"""
    pump = next(o for o in store.objects() if o.name == "DF-PS-01")
    road = next(o for o in store.objects() if o.name == "R-102")

    incident = next(
        triple
        for triple in store.triples()
        if triple.predicate.value == "affects" and triple.object_id == pump.twin_id
    )
    location = _triple_of(store, pump.twin_id, "located_in")
    road_zone = _triple_of(store, road.twin_id, "located_in")

    incident_ref = incident.evidence_refs[0]
    facility_ref = location.evidence_refs[0]
    assert split_ref(incident_ref)[0] != split_ref(facility_ref)[0], "两条证据必须来自不同资产"

    ledger = build_ledger()
    ledger.register(
        [
            Claim(
                claim_id="CL-fact-incident",
                text="DF-PS-01 泵组故障待修",
                kind=ClaimKind.FACT,
                evidence_refs=[incident_ref],
                produced_by="city_extract@1.0",
            ),
            Claim(
                claim_id="CL-inf-gap",
                text="DF-PS-01 故障且运维主体未界定，抽排责任需临时指定",
                kind=ClaimKind.INFERENCE,
                evidence_refs=[incident_ref, facility_ref],
                premise_claim_ids=["CL-fact-incident"],
                produced_by="deduction@1.0",
            ),
            Claim(
                claim_id="CL-fact-road",
                text="R-102 位于 A-03 片区",
                kind=ClaimKind.FACT,
                evidence_refs=[road_zone.evidence_refs[0]],
                produced_by="city_extract@1.0",
            ),
        ]
    )
    ledger.append(
        decision_id="DEC-20261029-01",
        claim_ids=["CL-inf-gap"],
        semantic_snapshot_hash=store.snapshot_hash(),
        processors=["city_extract@1.0", "deduction@1.0"],
    )
    ledger.append(
        decision_id="DEC-20261029-02",
        claim_ids=["CL-fact-road"],
        semantic_snapshot_hash=store.snapshot_hash(),
        processors=["city_extract@1.0"],
    )
    return ledger


def test_conclusion_traces_back_to_file_line_and_version(store) -> None:
    ledger = _record_two_decisions(store)
    assert ledger.verify().ok, "落账后账本必须自洽"

    trace = ledger.trace_claim("CL-inf-gap")
    assert trace.decision_ids == ["DEC-20261029-01"]
    assert trace.knowledge_version == "kb-v1.0.0"
    assert trace.semantic_snapshot_hash == store.snapshot_hash()
    assert trace.premise_claim_ids == ["CL-fact-incident"]
    assert trace.produced_by == "deduction@1.0"

    assets = {asset.asset_id: asset for asset in trace.assets}
    assert len(assets) >= 2, "推理结论必须至少两条来源"
    for asset in trace.assets:
        assert asset.locator.startswith("A-03/")
        assert len(asset.fingerprint) == 64
        assert asset.changed_since_record is False


def test_replay_bundle_is_enough_to_rebuild(store) -> None:
    ledger = _record_two_decisions(store)
    bundle = ledger.replay("DEC-20261029-01")
    assert bundle.knowledge_version == "kb-v1.0.0"
    assert bundle.semantic_snapshot_hash == store.snapshot_hash()
    assert bundle.claim_ids == ["CL-inf-gap"]
    assert "deduction@1.0" in bundle.processors
    assert len(bundle.asset_fingerprints) >= 2


def test_data_change_names_the_conclusions_to_recheck(store, tmp_path: Path) -> None:
    """改一个单元格，系统必须点名哪条结论需要重算，且不误报无关结论。"""
    ledger = _record_two_decisions(store)

    root = tmp_path / "urban"
    shutil.copytree(DEFAULT_DATASET_ROOT, root, dirs_exist_ok=True)
    path = root / "drainage_facilities.csv"
    text = path.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("DF-PS-01"))
    assert ",," in row
    path.write_text(text.replace(row, row.replace(",,", ",ORG-DRAIN,", 1)), encoding="utf-8")

    registry = KnowledgeRegistry(snapshot_from_bundle(build_city_bundle()))
    after = build_city_bundle(root)
    trigger = [
        asset.asset_id
        for asset, _ in load_urban_assets(root)
        if asset.file_name == "drainage_facilities.csv"
    ]
    proposal = registry.propose(after, trigger_asset_ids=trigger)
    assert proposal.changes, "改泵站归属必须产生候选，否则血缘无从谈起"
    registry.publish(proposal, after)

    diff = get_knowledge_diff(registry, "kb-v1.0.0", "kb-v1.0.1")
    assert not diff.empty
    assert diff.affected_locators, "diff 必须给出稳定的血缘键"

    affected = ledger.affected_decisions(diff.affected_locators)
    assert affected == ["DEC-20261029-01"], "只应点名依赖排水台账的结论，不能牵连道路结论"


def test_untouched_data_affects_nothing(store) -> None:
    ledger = _record_two_decisions(store)
    assert ledger.affected_decisions(["A-03/pipeline_ledger/v1#row:9"]) == []
    assert ledger.affected_decisions(["AS-nonexistent"]) == []


def test_tampered_record_is_located(store) -> None:
    ledger = _record_two_decisions(store)
    first = ledger.records()[0]
    ledger._records[0] = first.model_copy(update={"semantic_snapshot_hash": "0" * 64})

    report = ledger.verify()
    assert not report.ok
    assert report.first_broken_record_id == first.record_id
    assert any(reason.startswith("HASH_MISMATCH") for reason in report.reasons)


def test_claim_ref_matches_semantic_triple_ref(store) -> None:
    """溯源引用与语义层引用必须同一套语法，否则两边对不上账。"""
    for triple in store.triples():
        for ref in triple.evidence_refs:
            asset_id, locator = split_ref(ref)
            assert asset_id.startswith("AS-")
            assert locator
