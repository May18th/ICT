"""P1 dataset tests: registration, governance binding, determinism."""

import json
from datetime import datetime

from decision_poc.governance import AssetStatus
from decision_poc.urban_dataset import (
    DEFAULT_DATASET_ROOT,
    URBAN_ASSETS,
    build_urban_manifest,
    load_urban_assets,
)

NOW = datetime.fromisoformat("2026-10-29T09:30:00+08:00")


def test_every_declared_file_exists() -> None:
    missing = [spec.file_name for spec in URBAN_ASSETS if not (DEFAULT_DATASET_ROOT / spec.file_name).is_file()]
    assert missing == []


def test_manifest_counts_match_spec() -> None:
    payload = build_urban_manifest()
    assert payload["asset_count"] == len(URBAN_ASSETS) == 15
    assert payload["modality_counts"]["image"] == 1
    assert payload["modality_counts"]["table"] == 8
    assert payload["modality_counts"]["text"] == 6
    assert len(payload["source_org_counts"]) >= 5


def test_governance_hash_equals_asset_checksum() -> None:
    for asset, governance in load_urban_assets():
        assert governance.content_hash == asset.checksum_sha256
        assert asset.asset_id.startswith("AS-")


def test_deprecated_plan_is_out_of_validity() -> None:
    rows = {asset.file_name: (asset, gov) for asset, gov in load_urban_assets()}
    _, gov = rows["plan_flood_emergency_v1.md"]
    assert gov.status is AssetStatus.DEPRECATED
    assert not gov.is_valid_at(NOW)


def test_current_plan_is_citable() -> None:
    rows = {asset.file_name: (asset, gov) for asset, gov in load_urban_assets()}
    _, gov = rows["plan_flood_emergency_v2.md"]
    assert gov.is_published()
    assert gov.is_valid_at(NOW)


def test_gas_procedure_hidden_from_drainage_caller() -> None:
    rows = {asset.file_name: (asset, gov) for asset, gov in load_urban_assets()}
    _, gov = rows["procedure_gas_protection.md"]
    assert not gov.is_visible_to(["ORG-DRAIN"])
    assert gov.is_visible_to(["ORG-GAS", "ORG-EMERG"])


def test_public_notice_visible_to_everyone() -> None:
    rows = {asset.file_name: (asset, gov) for asset, gov in load_urban_assets()}
    _, gov = rows["notice_road_closure.md"]
    assert gov.is_visible_to([])


def test_manifest_is_deterministic() -> None:
    first = json.dumps(build_urban_manifest(), ensure_ascii=False, sort_keys=True)
    second = json.dumps(build_urban_manifest(), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_committed_manifest_is_current() -> None:
    committed = json.loads((DEFAULT_DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    fresh = build_urban_manifest()
    assert committed["asset_count"] == fresh["asset_count"]
    assert [a["asset_id"] for a in committed["assets"]] == [a["asset_id"] for a in fresh["assets"]]
