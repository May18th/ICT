"""数据门禁：来源分级、基准一致性、跨字段一致性。

这组测试要证明的不只是"能跑通"，而是三件事：
1. 每条检查都**真的会拦**——每条 error 都有一个把数据改坏到触发它的用例；
2. 判据不靠猜——缺字段时跳过而不是拿默认值硬算；
3. 门禁只读不写——跑完 `samples/assets/urban/` 的 CSV 一个字节都不变。
   这条最容易被忽略：一旦门禁开始"顺手修正"数据，manifest 指纹就会跟着漂，
   而 `evidence_check` 的冻结口径会对不上。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from decision_poc.data_gate import (
    DEFAULT_DATASET_ROOT,
    DataFact,
    Datum,
    ProvenanceLevel,
    Severity,
    build_facts,
    check_cross_field,
    check_datum,
    check_magnitude,
    check_provenance,
    main,
    run_gate,
)
from decision_poc.governance import ErrorCode

#: 冻结的事实构成。数据或标注有意变更时同步改这里，并说明原因——
#: 不允许为了让测试过而改断言。
FACTS_TOTAL = 70
BY_PROVENANCE = {"real": 4, "derived": 3, "assumption": 3, "synthetic": 60}
PLAN = "plan_flood_emergency_chengdu_2020.md"


def _fact(
    entity_id: str,
    field: str,
    value: float,
    *,
    level: ProvenanceLevel = ProvenanceLevel.SYNTHETIC,
    basis: str = "测试用默认依据",
    **overrides: object,
) -> DataFact:
    payload: dict[str, object] = {
        "fact_id": f"{entity_id}/{field}",
        "entity_id": entity_id,
        "field": field,
        "value": value,
        "provenance": level,
        "basis": basis,
    }
    payload.update(overrides)
    return DataFact.model_validate(payload)


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "urban"
    shutil.copytree(DEFAULT_DATASET_ROOT, target)
    return target


# --- 真实数据集 -----------------------------------------------------------


def test_live_dataset_passes() -> None:
    report = run_gate()
    assert report.ok, [item.message for item in report.errors]
    assert report.facts_total == FACTS_TOTAL


def test_provenance_counts_are_honest() -> None:
    """分级必须如实计数：synthetic 占多数就是占多数，不许美化。"""
    report = run_gate()
    assert report.by_provenance == BY_PROVENANCE
    assert any(item.check_id == "DG-SRC-006" for item in report.warns)


def test_threshold_facts_are_locatable() -> None:
    """真实口径四条都能定位到市级预案原文，且关键词真的在那行。"""
    facts, _ = build_facts()
    real = [item for item in facts if item.provenance is ProvenanceLevel.REAL]
    assert len(real) == 4
    assert check_provenance(real, DEFAULT_DATASET_ROOT) == []
    red = next(item for item in real if item.field == "response_red_24h_mm")
    assert red.value == pytest.approx(250.0)
    assert red.source_ref == f"{PLAN}#line:41"


def test_district_closure_threshold_is_not_marked_real() -> None:
    """区级合成预案的 25cm 封路阈值不得冒充公开口径。

    市级预案第 107 行自己声明它「来源于合成设定，不属于本文件的真实条款」；
    这条测试把那个声明固化成断言，防止日后被“顺手”归到 real 里充数。
    """
    facts, _ = build_facts()
    closure = next(item for item in facts if item.field == "closure_depth_cm")
    assert closure.provenance is ProvenanceLevel.SYNTHETIC
    assert "合成" in closure.basis


def test_gate_does_not_modify_the_dataset() -> None:
    """只读承诺：跑一遍不能动数据集里任何 CSV。"""
    before = {path.name: path.stat().st_mtime_ns for path in DEFAULT_DATASET_ROOT.glob("*.csv")}
    run_gate()
    after = {path.name: path.stat().st_mtime_ns for path in DEFAULT_DATASET_ROOT.glob("*.csv")}
    assert before == after


# --- 来源分级 -------------------------------------------------------------


def test_real_without_source_ref_is_error() -> None:
    findings = check_provenance(
        [_fact("threshold:x", "closure_depth_cm", 25.0, level=ProvenanceLevel.REAL)],
        DEFAULT_DATASET_ROOT,
    )
    assert [item.check_id for item in findings] == ["DG-SRC-001"]
    assert findings[0].code == ErrorCode.EVIDENCE_INSUFFICIENT
    assert findings[0].severity is Severity.ERROR


def test_real_source_file_missing_is_error() -> None:
    bad = _fact(
        "threshold:x",
        "closure_depth_cm",
        25.0,
        level=ProvenanceLevel.REAL,
        source_ref="no_such_plan.md#line:1",
    )
    findings = check_provenance([bad], DEFAULT_DATASET_ROOT)
    assert [item.check_id for item in findings] == ["DG-SRC-002"]


def test_real_source_line_out_of_range_is_error() -> None:
    bad = _fact(
        "threshold:x",
        "closure_depth_cm",
        25.0,
        level=ProvenanceLevel.REAL,
        source_ref=f"{PLAN}#line:9999",
    )
    findings = check_provenance([bad], DEFAULT_DATASET_ROOT)
    assert [item.check_id for item in findings] == ["DG-SRC-003"]


def test_real_keyword_absent_from_source_line_is_error() -> None:
    """行号对但正文没有这个值——等于引用了一条不存在的口径。"""
    bad = _fact(
        "threshold:x",
        "closure_depth_cm",
        99.0,
        level=ProvenanceLevel.REAL,
        source_ref=f"{PLAN}#line:29",
        keyword="99cm",
    )
    findings = check_provenance([bad], DEFAULT_DATASET_ROOT)
    assert [item.check_id for item in findings] == ["DG-SRC-004"]


def test_asset_locator_prefix_is_tolerated() -> None:
    """兼容规则集的 `file.md#A-03/xx/v2.0#line:29` 写法，取最后一段 line。"""
    good = _fact(
        "threshold:x",
        "response_red_24h_mm",
        250.0,
        level=ProvenanceLevel.REAL,
        source_ref=f"{PLAN}#A-03/emergency_plan/v2020#line:41",
        keyword="250毫米",
    )
    assert check_provenance([good], DEFAULT_DATASET_ROOT) == []


def test_assumption_without_basis_warns() -> None:
    bad = _fact("underpass:X", "close_threshold_m", 0.25, level=ProvenanceLevel.ASSUMPTION, basis="  ")
    findings = check_provenance([bad], DEFAULT_DATASET_ROOT)
    assert [item.check_id for item in findings] == ["DG-SRC-005"]
    assert findings[0].severity is Severity.WARN


def test_derived_without_upstream_warns() -> None:
    bad = _fact("pipeline:X", "invert_elevation_m", 0.4, level=ProvenanceLevel.DERIVED)
    findings = check_provenance([bad], DEFAULT_DATASET_ROOT)
    assert [item.check_id for item in findings] == ["DG-SRC-005"]


# --- 基准一致性 -----------------------------------------------------------


def test_mixed_datum_is_error() -> None:
    findings = check_datum(
        [
            _fact("road:A-1", "elevation_m", 3.2, datum=Datum.SITE_RELATIVE),
            _fact("road:A-2", "elevation_m", 501.8, datum=Datum.CITY_ABSOLUTE),
        ]
    )
    assert any(item.check_id == "DG-DAT-001" for item in findings)
    assert all(item.code == ErrorCode.EVIDENCE_CONFLICT for item in findings)


def test_site_relative_with_city_magnitude_is_error() -> None:
    findings = check_datum([_fact("road:A-1", "elevation_m", 501.8, datum=Datum.SITE_RELATIVE)])
    assert [item.check_id for item in findings] == ["DG-DAT-003"]


def test_missing_datum_on_elevation_warns() -> None:
    findings = check_datum([_fact("road:A-1", "elevation_m", 3.2)])
    assert [item.check_id for item in findings] == ["DG-DAT-002"]


def test_water_depth_is_not_treated_as_elevation() -> None:
    """水深是相对深度，不应被要求标基准——误报比漏报更烦人。"""
    assert check_datum([_fact("work_order:W-1", "water_depth_cm", 30.0)]) == []


# --- 量级合理性 -----------------------------------------------------------


def test_magnitude_ceiling_is_error() -> None:
    findings = check_magnitude([_fact("work_order:W-1", "water_depth_cm", 500.0)])
    assert [item.check_id for item in findings] == ["DG-MAG-001"]


def test_non_positive_value_on_positive_field_is_error() -> None:
    findings = check_magnitude([_fact("pump:P-1", "capacity_m3h", 0.0)])
    assert [item.check_id for item in findings] == ["DG-MAG-002"]


# --- 跨字段一致性 ---------------------------------------------------------


def test_failed_units_over_total_is_error() -> None:
    findings = check_cross_field(
        [
            _fact("pump:P-1", "pump_units", 3.0),
            _fact("pump:P-1", "failed_units", 5.0),
        ]
    )
    assert any(item.check_id == "DG-XF-003" for item in findings)


def test_underpass_close_threshold_below_sill_is_error() -> None:
    findings = check_cross_field(
        [
            _fact("underpass:U-1", "entry_sill_height_m", 0.15, datum=Datum.SITE_RELATIVE),
            _fact("underpass:U-1", "close_threshold_m", 0.10),
        ]
    )
    assert any(item.check_id == "DG-XF-002" for item in findings)


def test_response_thresholds_must_increase_with_level() -> None:
    """响应阈值倒挂是最隐蔽的一类错：照旧能跑，但更高级别反而更容易触发。"""
    findings = check_cross_field(
        [
            _fact("threshold:chengdu-2020", "response_blue_6h_mm", 50.0, unit="mm/6h"),
            _fact("threshold:chengdu-2020", "response_yellow_12h_mm", 40.0, unit="mm/12h"),
        ]
    )
    assert any(item.check_id == "DG-XF-004" for item in findings)


def test_cross_field_skips_when_pipe_missing() -> None:
    """没有管线就别算高差：跳过的假阴性看得见，硬算的假阳性看不出来。"""
    findings = check_cross_field(
        [
            _fact("work_order:W-1", "water_depth_cm", 80.0, parent_id="road:R-1"),
            _fact("road:R-1", "elevation_m", 3.2, datum=Datum.SITE_RELATIVE),
        ]
    )
    assert [item for item in findings if item.check_id == "DG-XF-001"] == []


# --- 端到端：改坏数据必须拦下 ---------------------------------------------


def test_water_depth_beyond_pipe_depth_is_error(tmp_path) -> None:
    dataset = _copy_dataset(tmp_path)
    path = dataset / "work_orders.csv"
    text = path.read_text(encoding="utf-8-sig").replace("积水深约 30cm", "积水深约 350cm")
    path.write_text(text, encoding="utf-8")

    report = run_gate(dataset)
    assert not report.ok
    [finding] = [item for item in report.errors if item.check_id == "DG-XF-001"]
    assert "350" in finding.message
    assert "R-101" in finding.message


def test_cli_exit_code_follows_errors(tmp_path, capsys) -> None:
    assert main([]) == 0
    capsys.readouterr()

    dataset = _copy_dataset(tmp_path)
    path = dataset / "work_orders.csv"
    path.write_text(
        path.read_text(encoding="utf-8-sig").replace("积水深约 30cm", "积水深约 350cm"),
        encoding="utf-8",
    )
    assert main(["--dataset", str(dataset)]) == 1
