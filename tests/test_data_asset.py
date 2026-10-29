import json
from pathlib import Path

import pytest

from decision_poc.data_asset import (
    AssetModality,
    AssetSpec,
    DataAsset,
    ParseStatus,
    asset_lookup,
    build_manifest,
    detect_modality,
    identify_asset,
    manifest_from_mapping,
    manifest_payload,
    resolve_asset,
    to_nexent_metadata,
)

ASSETS = Path(__file__).resolve().parents[1] / "samples" / "assets"


def test_table_asset_reports_shape_and_headers():
    asset = identify_asset(
        ASSETS / "inspection_log.csv", doc_type="巡检记录", equipment_id="A-101"
    )

    assert asset.modality is AssetModality.TABLE
    assert asset.parse_status is ParseStatus.PARSED
    assert asset.structure["rows"] == 8
    assert "设备编号" in asset.structure["headers"]
    assert "8 行" in asset.summary
    assert asset.locator == "A-101/巡检记录/v1"


def test_text_asset_keeps_standard_refs_and_lead_paragraph():
    refs = ["ISO 10816-3:2009 §7.2", "Q/XX-2024-11 §5.3"]
    asset = identify_asset(
        ASSETS / "maintenance_procedure.md",
        doc_type="检修规程",
        equipment_id="A-101",
        standard_refs=refs,
    )

    assert asset.modality is AssetModality.TEXT
    assert asset.standard_refs == refs
    assert asset.structure["lines"] > 10
    assert "适用于 L2 产线主传动减速机" in asset.summary


def test_png_asset_reads_pixel_size():
    asset = identify_asset(
        ASSETS / "vibration_spectrum.png", doc_type="频谱图", equipment_id="A-101"
    )

    assert asset.modality is AssetModality.IMAGE
    assert asset.structure == {"width": 900, "height": 500}
    assert "900×500" in asset.summary


def test_unknown_suffix_is_metadata_only(tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.7 fake")

    asset = identify_asset(target, doc_type="检测报告")

    assert asset.modality is AssetModality.DOCUMENT
    assert asset.parse_status is ParseStatus.METADATA_ONLY
    assert asset.structure == {}
    assert asset.equipment_id is None
    assert asset.locator == "检修报告/v1" or asset.locator == "检测报告/v1"


def test_asset_id_and_checksum_are_stable_and_content_sensitive(tmp_path):
    target = tmp_path / "log.csv"
    target.write_text("设备编号,振动\nA-101,7.8\n", encoding="utf-8")
    first = identify_asset(target, doc_type="巡检记录", equipment_id="A-101")
    second = identify_asset(target, doc_type="巡检记录", equipment_id="A-101")

    assert first.asset_id == second.asset_id
    assert first.checksum_sha256 == second.checksum_sha256
    assert first.asset_id.startswith("AS-A-101-巡检记录-")

    target.write_text("设备编号,振动\nA-101,9.1\n", encoding="utf-8")
    changed = identify_asset(target, doc_type="巡检记录", equipment_id="A-101")

    assert changed.asset_id != first.asset_id
    assert changed.checksum_sha256 != first.checksum_sha256


def test_gbk_encoded_table_is_decoded(tmp_path):
    target = tmp_path / "ledger_gbk.csv"
    target.write_bytes("设备编号,温度\nA-101,85\n".encode("gb18030"))

    asset = identify_asset(target, doc_type="设备台账")

    assert asset.structure["headers"] == ["设备编号", "温度"]
    assert asset.structure["rows"] == 1


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        identify_asset(ASSETS / "nope.csv", doc_type="巡检记录")


def test_blank_doc_type_is_rejected():
    with pytest.raises(ValueError):
        identify_asset(ASSETS / "inspection_log.csv", doc_type="   ")


def test_manifest_is_sorted_and_round_trips():
    specs = [
        AssetSpec(ASSETS / "inspection_log.csv", "巡检记录", "A-101"),
        AssetSpec(
            ASSETS / "maintenance_procedure.md",
            "检修规程",
            "A-101",
            standard_refs=("ISO 10816-3:2009 §7.2",),
        ),
        AssetSpec(ASSETS / "equipment_ledger.csv", "设备台账"),
        AssetSpec(ASSETS / "vibration_spectrum.png", "频谱图", "A-101"),
    ]
    manifest = build_manifest(specs)

    assert [asset.asset_id for asset in manifest] == sorted(
        asset.asset_id for asset in manifest
    )
    payload = manifest_payload(manifest)
    assert payload["asset_count"] == 4
    assert payload["modality_counts"] == {"table": 2, "text": 1, "image": 1}

    restored = manifest_from_mapping(payload["assets"])
    assert [asset.asset_id for asset in restored] == [
        asset.asset_id for asset in manifest
    ]


def test_lookup_and_resolve():
    manifest = build_manifest(
        [AssetSpec(ASSETS / "equipment_ledger.csv", "设备台账")]
    )
    index = asset_lookup(manifest)
    target = manifest[0]

    assert index[target.asset_id] is target
    assert resolve_asset(manifest, target.asset_id) == target
    with pytest.raises(KeyError):
        resolve_asset(manifest, "AS-nope")


def test_nexent_metadata_is_compact():
    manifest = build_manifest(
        [
            AssetSpec(ASSETS / "equipment_ledger.csv", "设备台账"),
            AssetSpec(ASSETS / "vibration_spectrum.png", "频谱图", "A-101"),
        ]
    )
    metadata = to_nexent_metadata(manifest)

    assert len(metadata["data_assets"]) == 2
    first = metadata["data_assets"][0]
    assert set(first) == {"asset_id", "doc_type", "equipment_id", "modality", "standard_refs"}
    assert json.dumps(metadata, ensure_ascii=False)


def test_modality_detection_covers_common_suffixes():
    assert detect_modality(Path("a.CSV")) is AssetModality.TABLE
    assert detect_modality(Path("a.md")) is AssetModality.TEXT
    assert detect_modality(Path("a.jpg")) is AssetModality.IMAGE
    assert detect_modality(Path("a.xlsx")) is AssetModality.DOCUMENT
    assert detect_modality(Path("a.unknown")) is AssetModality.DOCUMENT


def test_data_asset_rejects_blank_standard_ref():
    with pytest.raises(ValueError):
        DataAsset(
            asset_id="AS-1",
            modality=AssetModality.TEXT,
            parse_status=ParseStatus.PARSED,
            file_name="a.md",
            doc_type="规程",
            version="v1",
            standard_refs=["  "],
            locator="A-101/规程/v1",
            checksum_sha256="0" * 64,
            byte_size=1,
            summary="s",
        )
