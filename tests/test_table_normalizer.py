"""T3 table normalisation tests: typing, row locators, encoding, bad cells."""

from decision_poc.data_asset import identify_asset
from decision_poc.table_normalizer import (
    decode_bytes,
    normalize_table,
    normalize_table_file,
    table_fingerprint,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

ROADS = DEFAULT_DATASET_ROOT / "road_network.csv"
PIPES = DEFAULT_DATASET_ROOT / "pipeline_ledger.csv"


def _asset(path, doc_type):
    return identify_asset(path, doc_type=doc_type, equipment_id="A-03", version="v1")


def test_road_table_is_typed() -> None:
    asset = _asset(ROADS, "road_network")
    table = normalize_table_file(asset, ROADS)
    assert len(table.rows) == 7
    assert table.column_names()[:4] == ["road_id", "name", "district", "road_class"]
    lanes = table.columns[table.column_names().index("lanes")]
    elevation = table.columns[table.column_names().index("elevation_m")]
    assert lanes.dtype == "int"
    assert elevation.dtype == "float"
    assert elevation.unit == "m"
    first = table.rows[0]
    assert first["road_id"] == "R-101"
    assert isinstance(first["lanes"], int)
    assert abs(float(first["elevation_m"]) - 3.2) < 1e-9
    assert table.unmapped_columns == []


def test_row_locators_match_source_lines() -> None:
    asset = _asset(ROADS, "road_network")
    table = normalize_table_file(asset, ROADS)
    lines = ROADS.read_text(encoding="utf-8").splitlines()
    for row, locator in zip(table.rows, table.row_locators):
        line_no = int(locator.split(":")[1])
        assert row["road_id"] in lines[line_no - 1]


def test_pipeline_datetime_and_numbers() -> None:
    asset = _asset(PIPES, "pipeline_ledger")
    table = normalize_table_file(asset, PIPES)
    depth = table.columns[table.column_names().index("depth_m")]
    diameter = table.columns[table.column_names().index("diameter_mm")]
    assert depth.dtype == "float" and diameter.dtype == "int"
    row = next(item for item in table.rows if item["pipeline_id"] == "PL-GAS-101")
    assert row["owner_org"] == "ORG-GAS"
    assert abs(float(row["depth_m"]) - 2.5) < 1e-9


def test_bad_number_keeps_raw_text_and_warns() -> None:
    asset = _asset(ROADS, "road_network")
    text = "road_id,elevation_m\nR-900,深\n"
    table = normalize_table(asset, text)
    assert table.rows[0]["elevation_m"] == "深"
    assert any("elevation_m" in warning for warning in table.warnings)


def test_unmapped_columns_are_reported_not_dropped() -> None:
    asset = _asset(ROADS, "road_network")
    text = "road_id,神秘字段\nR-900,值\n"
    table = normalize_table(asset, text)
    assert table.unmapped_columns == ["神秘字段"]
    assert table.rows[0]["神秘字段"] == "值"
    assert any("未识别列" in warning for warning in table.warnings)


def test_gb18030_and_bom_are_decoded() -> None:
    raw = "道路编号,片区\nR-101,A-03\n".encode("gb18030")
    assert "道路编号" in decode_bytes(raw)
    assert "道路编号" in decode_bytes(b"\xef\xbb\xbf" + "道路编号".encode("utf-8"))


def test_empty_table_is_reported() -> None:
    asset = _asset(ROADS, "road_network")
    table = normalize_table(asset, "")
    assert table.rows == []
    assert table.warnings


def test_fingerprint_is_stable() -> None:
    asset = _asset(ROADS, "road_network")
    first = table_fingerprint(normalize_table_file(asset, ROADS))
    second = table_fingerprint(normalize_table_file(asset, ROADS))
    assert first == second
