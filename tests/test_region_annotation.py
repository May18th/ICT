"""T4 region annotation tests: bounds validation, locator format, adapters."""

import pytest

from decision_poc.adapters import parse_asset, parse_assets, parse_stats
from decision_poc.data_asset import identify_asset
from decision_poc.region_annotation import (
    RegionSpec,
    load_annotations,
    png_size,
    regions_for,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

MAP = DEFAULT_DATASET_ROOT / "pipeline_map.png"
ANNOTATIONS = DEFAULT_DATASET_ROOT / "annotations.json"


def _asset():
    return identify_asset(MAP, doc_type="schematic_map", equipment_id="A-03", version="v1")


def test_png_size_matches_generator() -> None:
    assert png_size(MAP) == (800, 520)


def test_regions_are_citable() -> None:
    asset = _asset()
    annotation = load_annotations(ANNOTATIONS)
    specs = annotation.annotations["pipeline_map.png"]
    regions = regions_for(asset, specs, size=png_size(MAP), annotator=annotation.annotator)
    assert len(regions) == 5
    labels = {region.label for region in regions}
    assert "积水区" in labels and "DF-PS-01 雨水泵站" in labels
    for region in regions:
        assert region.locator.startswith(asset.locator)
        assert "#region:" in region.locator
        x, y, width, height = region.bbox
        assert x + width <= 800 and y + height <= 520
        assert region.annotator == "manual:v1"


def test_out_of_bounds_bbox_is_rejected() -> None:
    asset = _asset()
    spec = RegionSpec(label="越界区", bbox=(700, 480, 200, 100))
    with pytest.raises(ValueError):
        regions_for(asset, [spec], size=(800, 520), annotator="manual:v1")


def test_negative_bbox_is_rejected_at_model_level() -> None:
    with pytest.raises(ValueError):
        RegionSpec(label="负坐标", bbox=(-1, 10, 10, 10))


def test_image_asset_without_annotations_warns() -> None:
    document = parse_asset(_asset(), MAP)
    assert document.parse_status.value == "metadata_only"
    assert document.regions == []
    assert any("标注" in warning for warning in document.warnings)


def test_all_assets_parse_and_locators_are_well_formed() -> None:
    assets = [asset for asset, _ in load_urban_assets()]
    documents = parse_assets(assets, DEFAULT_DATASET_ROOT, annotations_path=ANNOTATIONS)
    stats = parse_stats(documents)
    assert stats["documents"] == 15
    assert stats["tables"] == 8
    assert stats["regions"] == 5
    assert stats["metadata_only"] == 0
    for document in documents:
        for chunk in document.chunks:
            assert chunk.locators and all("#line:" in locator for locator in chunk.locators)
        for table in document.tables:
            assert all(locator.startswith("#row:") for locator in table.row_locators)
        for region in document.regions:
            assert "#region:" in region.locator


def test_unsupported_document_is_metadata_only() -> None:
    asset = _asset()
    document = parse_asset(asset, ANNOTATIONS)
    assert document.parse_status.value == "metadata_only"
    assert any("未实现" in warning or "未知后缀" in warning for warning in document.warnings)


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        parse_asset(_asset(), DEFAULT_DATASET_ROOT / "not-there.csv")
