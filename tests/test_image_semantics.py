"""图像语义抽取：像素可复算的峰值/色块/积水范围，以及区域候选。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc import image_semantics as img
from decision_poc.region_annotation import RegionSpec

ROOT = Path(__file__).resolve().parents[1]
SPECTRUM = ROOT / "samples" / "assets" / "vibration_spectrum.png"
MAP = ROOT / "samples" / "assets" / "urban" / "pipeline_map.png"


def test_read_png_dimensions() -> None:
    spectrum = img.read_png(SPECTRUM)
    assert (spectrum.width, spectrum.height, spectrum.channels) == (900, 500, 3)
    schematic = img.read_png(MAP)
    assert (schematic.width, schematic.height, schematic.channels) == (800, 520, 3)


def test_detect_kind_by_pixels_not_filename() -> None:
    assert img.detect_kind(img.read_png(SPECTRUM)) == "spectrum"
    assert img.detect_kind(img.read_png(MAP)) == "schematic"


def test_spectrum_peaks_match_generator_ground_truth() -> None:
    facts = img.analyze(SPECTRUM)
    assert facts["kind"] == "spectrum"
    freqs = [peak["freq_hz"] for peak in facts["peaks"]]
    assert len(freqs) == 4
    for expected, actual in zip((120, 240, 360, 480), freqs):
        assert abs(actual - expected) <= 6, f"期望 ~{expected}Hz，实际 {actual}Hz"


def test_spectrum_amplitudes_are_descending() -> None:
    amps = [peak["rel_amplitude"] for peak in img.analyze(SPECTRUM)["peaks"]]
    assert amps == sorted(amps, reverse=True)
    # 基频显著高于二次/三次谐波（生成器：1.4 vs 0.55 vs 0.32）
    assert amps[0] > amps[1] > amps[2]


def test_spectrum_reports_its_boundaries_honestly() -> None:
    facts = img.analyze(SPECTRUM)
    assert facts["frequency_axis"]["source"] == "generator_contract"
    assert facts["plot"]["x"] == [80, 860]
    assert "不是 mm/s" in facts["note"]
    for peak in facts["peaks"]:
        x, y, width, height = peak["bbox"]
        assert 0 <= x and 0 <= y
        assert x + width <= 900 and y + height <= 500


def test_schematic_segments_and_orientations() -> None:
    facts = img.analyze(MAP)
    assert facts["kind"] == "schematic"
    segments = facts["segments"]
    assert facts["segment_count"] >= 4
    by_color = {tuple(item["color"]): item for item in segments}
    # 生成器固定色：排涝横线、燃气横线、电力竖线、通信竖线、给水竖线
    assert by_color[(40, 110, 190)]["orientation"] == "horizontal"
    assert by_color[(200, 60, 40)]["orientation"] == "horizontal"
    assert by_color[(215, 150, 30)]["orientation"] == "vertical"
    assert by_color[(60, 150, 100)]["orientation"] == "vertical"
    assert by_color[(120, 90, 170)]["orientation"] == "vertical"
    for item in segments:
        x, y, width, height = item["bbox"]
        assert x + width <= 800 and y + height <= 520
        assert item["pixels"] >= 150


def test_schematic_flood_area() -> None:
    flood = img.analyze(MAP)["flood_area"]
    assert flood is not None
    assert flood["pixels"] > 1000
    x, y, width, height = flood["bbox"]
    assert x + width <= 800 and y + height <= 520
    # 浅蓝积水带是横向的（宽 >> 高）
    assert width > 5 * height


def test_region_specs_are_validatable() -> None:
    specs = img.to_region_specs(img.analyze(SPECTRUM))
    assert specs and all(item["label"] for item in specs)
    for item in specs:
        spec = RegionSpec.model_validate(item)  # bbox 合法性由模型校验
        assert spec.bbox[2] > 0 and spec.bbox[3] > 0
    schematic_specs = img.to_region_specs(img.analyze(MAP))
    labels = " ".join(item["label"] for item in schematic_specs)
    assert "积水范围" in labels


def test_unsupported_file_raises(tmp_path) -> None:
    fake = tmp_path / "not_a_png.png"
    fake.write_bytes(b"not a png at all")
    with pytest.raises(img.UnsupportedImage):
        img.read_png(fake)


def test_cli_writes_json_and_regions(tmp_path, capsys) -> None:
    out_json = tmp_path / "semantics.json"
    out_regions = tmp_path / "regions.json"
    code = img.main(
        [
            "--image",
            str(SPECTRUM),
            "--json",
            str(out_json),
            "--regions",
            str(out_regions),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "spectrum"
    assert summary["peaks"] == sorted(summary["peaks"])
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["peak_count"] == 4
    regions = json.loads(out_regions.read_text(encoding="utf-8"))["regions"]
    assert len(regions) == len(payload["peaks"])
