"""P3: proposal -> review -> publish -> rollback.

The tests here are the evidence for innovation point 1 ("knowledge evolves with
incoming data"). They assert the mechanism, not the wording: a data edit must
produce a proposal that names exactly what changed, shows impact before
approval, allows per-item review, and can be rolled back to a byte-identical
snapshot hash.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from decision_poc.city_extract import build_city_bundle
from decision_poc.knowledge_evolution import (
    ChangeKind,
    KnowledgeRegistry,
    ProposalStatus,
    ReviewChoice,
    load_snapshot,
    snapshot_from_bundle,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

BASE_BUNDLE = build_city_bundle()
BASE_SNAPSHOT = snapshot_from_bundle(BASE_BUNDLE)
DRAINAGE_TRIGGER = [
    asset_id for asset_id in BASE_SNAPSHOT.source_asset_ids if "drainage_facility" in asset_id
]


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "urban"
    shutil.copytree(DEFAULT_DATASET_ROOT, target, dirs_exist_ok=True)
    return target


def _fill_pump_owner(root: Path) -> None:
    """Give DF-PS-01 an owner -- the responsibility gap, closed by new data."""
    path = root / "drainage_facilities.csv"
    text = path.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("DF-PS-01"))
    assert ",," in row, "DF-PS-01 的 owner_org 必须为空，否则这个测试失去意义"
    path.write_text(text.replace(row, row.replace(",,", ",ORG-DRAIN,", 1)), encoding="utf-8")


# --- baseline ------------------------------------------------------------


def test_snapshot_hash_is_deterministic() -> None:
    again = snapshot_from_bundle(build_city_bundle())
    assert again.snapshot_hash == BASE_SNAPSHOT.snapshot_hash
    assert len(again.entities) == len(BASE_SNAPSHOT.entities)


def test_empty_diff_yields_empty_proposal() -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal = registry.propose(BASE_BUNDLE, trigger_asset_ids=DRAINAGE_TRIGGER)
    assert proposal.changes == []
    assert proposal.impact.reason == "无变更"


# --- propose -------------------------------------------------------------


def test_new_data_proposes_responsibility_change(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    root = _copy_dataset(tmp_path)
    _fill_pump_owner(root)

    proposal = registry.propose(build_city_bundle(root), trigger_asset_ids=DRAINAGE_TRIGGER)

    kinds = {(change.kind, change.candidate_kind) for change in proposal.changes}
    assert (ChangeKind.MODIFY, "entity") in kinds
    assert (ChangeKind.ADD, "relation") in kinds

    relation = next(change for change in proposal.changes if change.candidate_kind == "relation")
    assert relation.label == "ORG-DRAIN -responsible_for-> DF-PS-01"
    assert "responsibility_change" in proposal.impact.risk_flags
    assert proposal.impact.affected_names == ["DF-PS-01", "ORG-DRAIN"]
    assert proposal.impact.reason.startswith("2 条候选变更")


def test_content_hash_churn_does_not_inflate_proposal(tmp_path: Path) -> None:
    """Regression: asset ids are content-addressed, so one cell edit must not
    mark every candidate from that file as MODIFY (was 17 changes for 1 edit)."""
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    root = _copy_dataset(tmp_path)
    _fill_pump_owner(root)
    modified = build_city_bundle(root)

    # 触发资产在本地的 asset_id 确实变了（内容寻址），这正是噪声来源
    changed_ids = [
        asset_id
        for asset_id in {item.asset_id for c in modified.entities for item in c.evidence}
        if "drainage_facility" in asset_id
    ]
    assert set(changed_ids) - set(DRAINAGE_TRIGGER), "前提不成立：asset_id 应随内容变化"

    proposal = registry.propose(modified, trigger_asset_ids=DRAINAGE_TRIGGER)
    assert len(proposal.changes) == 2, [change.label for change in proposal.changes]


def test_removal_only_fires_for_trigger_assets(tmp_path: Path) -> None:
    """删除一行只能影响该行自身的候选，不能波及其他资产。"""
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    root = _copy_dataset(tmp_path)
    path = root / "drainage_facilities.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(line for line in lines if not line.startswith("DF-IN-201")) + "\n",
        encoding="utf-8",
    )

    proposal = registry.propose(build_city_bundle(root), trigger_asset_ids=DRAINAGE_TRIGGER)
    removals = [change for change in proposal.changes if change.kind is ChangeKind.REMOVE]
    assert removals, "删掉资产内实体后必须产生 REMOVE"
    assert all("DF-IN-201" in change.label for change in removals)


def test_proposal_requires_trigger_asset() -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    with pytest.raises(ValueError):
        registry.propose(BASE_BUNDLE, trigger_asset_ids=[])


def test_stale_claims_come_from_claim_index() -> None:
    """P3 无 claim；钩子必须现在就通，否则 P4 会改契约。"""
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    index = {DRAINAGE_TRIGGER[0]: ["claim-a", "claim-b"]}
    proposal = registry.propose(
        BASE_BUNDLE, trigger_asset_ids=DRAINAGE_TRIGGER, claim_index=index
    )
    assert proposal.impact.stale_claim_ids == ["claim-a", "claim-b"]


# --- decide --------------------------------------------------------------


def _gap_proposal(registry: KnowledgeRegistry, tmp_path: Path):
    root = _copy_dataset(tmp_path)
    _fill_pump_owner(root)
    bundle = build_city_bundle(root)
    return registry.propose(bundle, trigger_asset_ids=DRAINAGE_TRIGGER), bundle


def test_publish_applies_kept_changes_only(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, bundle = _gap_proposal(registry, tmp_path)

    decisions = {
        change.change_id: (
            ReviewChoice.KEEP if change.candidate_kind == "relation" else ReviewChoice.DROP
        )
        for change in proposal.changes
    }
    snapshot = registry.publish(proposal, bundle, decisions=decisions, note="只采纳职责关系")

    assert snapshot.version == "kb-v1.0.1"
    assert snapshot.parent_version == "kb-v1.0.0"
    assert len(snapshot.relations) == len(BASE_SNAPSHOT.relations) + 1

    pump = next(entity for entity in snapshot.entities if entity.name == "DF-PS-01")
    assert pump.attributes.get("owner_org") is None, "被 DROP 的属性变更不得写入"
    assert any(
        relation.predicate == "responsible_for" and relation.object == "DF-PS-01"
        for relation in snapshot.relations
    )
    assert registry.proposal(proposal.proposal_id).status is ProposalStatus.PUBLISHED


def test_publish_twice_is_rejected(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, bundle = _gap_proposal(registry, tmp_path)
    registry.publish(proposal, bundle)
    with pytest.raises(ValueError):
        registry.publish(proposal, bundle)


def test_reject_keeps_version_untouched(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, _ = _gap_proposal(registry, tmp_path)

    rejected = registry.reject(proposal, note="证据不足，等工单闭环")
    assert rejected.status is ProposalStatus.REJECTED
    assert registry.current.version == "kb-v1.0.0"
    assert len(registry.versions()) == 1
    assert registry.history[-1].action == "reject"


def test_rollback_restores_exact_hash(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, bundle = _gap_proposal(registry, tmp_path)
    published = registry.publish(proposal, bundle)
    assert published.snapshot_hash != BASE_SNAPSHOT.snapshot_hash

    restored = registry.rollback("kb-v1.0.0", note="回退")
    assert restored.snapshot_hash == BASE_SNAPSHOT.snapshot_hash
    assert registry.version().value == "kb-v1.0.0"
    assert registry.history[-1].action == "rollback"


def test_version_numbers_sort_numerically(tmp_path: Path) -> None:
    """kb-v1.0.10 必须排在 kb-v1.0.9 之后 —— 字符串排序会反过来。"""
    registry = KnowledgeRegistry(snapshot_from_bundle(build_city_bundle(), version="kb-v1.0.9"))
    proposal, bundle = _gap_proposal(registry, tmp_path)
    published = registry.publish(proposal, bundle)

    assert published.version == "kb-v1.0.10"
    assert registry.versions() == ["kb-v1.0.9", "kb-v1.0.10"]


# --- persistence ---------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, bundle = _gap_proposal(registry, tmp_path)
    published = registry.publish(proposal, bundle)
    out = tmp_path / "kb"
    registry.save(out)

    restored = load_snapshot(out / published.version)
    assert restored.snapshot_hash == published.snapshot_hash
    assert len(restored.entities) == len(published.entities)
    assert (out / "history.jsonl").is_file()
    assert (out / published.version / "entities.jsonl").is_file()


def test_reload_preserves_pending_proposal(tmp_path: Path) -> None:
    """人工确认天然发生在另一个进程，未决提案必须能跨进程续存。"""
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    proposal, _ = _gap_proposal(registry, tmp_path)
    out = tmp_path / "kb"
    registry.save(out)

    reloaded = KnowledgeRegistry.load(out)
    assert reloaded.current.snapshot_hash == BASE_SNAPSHOT.snapshot_hash
    restored = reloaded.proposal(proposal.proposal_id)
    assert restored.status is ProposalStatus.PENDING
    assert restored.summary() == proposal.summary()
    assert restored.impact.risk_flags == ["responsibility_change"]


def test_cli_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from decision_poc.knowledge_evolution import main

    root = _copy_dataset(tmp_path)
    out = tmp_path / "kb"
    dataset = str(root)

    assert main(["init", "--dataset", dataset, "--out", str(out)]) == 0
    _fill_pump_owner(root)

    assert main(["propose", "--dataset", dataset, "--out", str(out)]) == 0
    reloaded = KnowledgeRegistry.load(out)
    proposal = next(iter(reloaded._proposals.values()))
    assert proposal.summary()["add"] == 1

    assert (
        main(
            [
                "publish",
                "--dataset",
                dataset,
                "--out",
                str(out),
                "--proposal",
                proposal.proposal_id,
            ]
        )
        == 0
    )
    assert KnowledgeRegistry.load(out).current.version == "kb-v1.0.1"

    assert main(["rollback", "--out", str(out), "--version", "kb-v1.0.0"]) == 0
    final = KnowledgeRegistry.load(out)
    assert final.current.snapshot_hash == BASE_SNAPSHOT.snapshot_hash
    assert [entry.action for entry in final.history] == ["publish", "rollback"]
    capsys.readouterr()


def test_publish_does_not_auto_accept_review_level_changes(tmp_path: Path) -> None:
    """过期来源的候选默认不得写入新版本，除非显式 KEEP。

    废止预案 v1 的动作降权到 review 级；若 publish 默认采纳全部变更，
    等于把"待人工确认"当成"已确认"，与安全处理要求直接冲突。
    """
    registry = KnowledgeRegistry(BASE_SNAPSHOT)
    root = _copy_dataset(tmp_path)
    plan = root / "plan_flood_emergency_v1.md"
    text = plan.read_text(encoding="utf-8")
    plan.write_text(text.replace("4.1 先组织抽排降低水位。", "4.1 先组织抽排并同步报备属地。"), encoding="utf-8")

    bundle = build_city_bundle(root)
    from decision_poc.urban_dataset import load_urban_assets

    trigger = [
        asset.asset_id
        for asset, _ in load_urban_assets(root)
        if "flood_emergency_v1" in asset.file_name
    ]
    proposal = registry.propose(bundle, trigger_asset_ids=trigger)
    assert proposal.changes, "改废止预案必须产生候选，否则用例失效"
    assert all(
        change.detail or change.kind for change in proposal.changes
    )  # 候选存在即可，具体类型由抽取器决定

    snapshot = registry.publish(proposal, bundle)  # 不做任何显式确认
    assert registry.last_deferred, "review 级变更必须被暂缓而不是静默采纳"
    labels = {
        change.label
        for change in proposal.changes
        if change.change_id in registry.last_deferred
    }
    written = {entity.name for entity in snapshot.entities}
    assert not any("抽排并同步报备" in name for name in written), labels
