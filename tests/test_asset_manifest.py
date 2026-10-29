import json
from pathlib import Path

from decision_poc.asset_manifest import build_catalog_manifest, main, write_manifest
from decision_poc.data_asset import AssetModality

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_manifest_covers_three_modalities():
    assets = build_catalog_manifest(ROOT)

    assert len(assets) == 4
    assert {asset.modality for asset in assets} == {
        AssetModality.TABLE,
        AssetModality.TEXT,
        AssetModality.IMAGE,
    }
    assert all(asset.asset_id.startswith("AS-") for asset in assets)


def test_write_manifest_is_valid_json(tmp_path):
    assets = build_catalog_manifest(ROOT)
    target = write_manifest(assets, tmp_path / "manifest.json")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["asset_count"] == 4
    assert payload["modality_counts"]["table"] == 2
    assert len(payload["assets"]) == 4


def test_cli_reports_missing_assets(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    assert "缺少样例资产" in capsys.readouterr().err
