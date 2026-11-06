"""前池库容假设数据：假设值必须有标注，且不得被当成实测值或已注册资产引用。

为什么要单独测
-------------
`pump_forebay_curves.csv` 是"假设值"台账：它的数字会直接进 `level_pool_route` 产出
"停机后多久到警戒"这类对外可能引用的结论。因此在资产化之前就要有守卫：

1. 曲线自洽：等截面假设下 `storage = plan_area × stage`，水位/容积严格递增，警戒水位在曲线内；
2. 逐行标注：`value_basis=assumption`、`source_ref` 写明"待补"来源，第一条带依据指针；
3. 引用完整：`facility_id` 必须是 `drainage_facilities.csv` 里真实存在的泵站；
4. **未注册资产**：它现在故意不在 `URBAN_ASSETS` 里——注册会让资产数 +1 并触发一次对外数字重算。
   资产化那一轮要连同资产数、指纹、data_gate 事实数一起改，并把这条测试反过来写。

这一组测试只读，不写任何文件；数据被误标成 `real` 或悄悄注册时立即失败。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from decision_poc.urban_dataset import URBAN_ASSETS

ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = ROOT / "samples" / "assets" / "urban"
CURVE_PATH = DATASET_ROOT / "pump_forebay_curves.csv"
LEDGER_PATH = DATASET_ROOT / "drainage_facilities.csv"


def _curve_rows() -> list[dict[str, str]]:
    with CURVE_PATH.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _pump_station_ids() -> set[str]:
    with LEDGER_PATH.open(encoding="utf-8", newline="") as handle:
        return {
            row["facility_id"].strip()
            for row in csv.DictReader(handle)
            if (row.get("facility_type") or "").strip() == "pump_station"
        }


def test_curve_is_linear_with_declared_plan_area() -> None:
    """等截面矩形前池假设下，容积必须严格等于面积×水位。"""

    rows = _curve_rows()
    assert len(rows) >= 3
    stages = [float(row["stage_m"]) for row in rows]
    storages = [float(row["storage_m3"]) for row in rows]
    assert stages == sorted(stages) and len(set(stages)) == len(stages)
    assert storages == sorted(storages) and len(set(storages)) == len(storages)
    for row in rows:
        expected = float(row["plan_area_m2"]) * float(row["stage_m"])
        assert float(row["storage_m3"]) == pytest.approx(expected, abs=1e-6)
    warning = float(rows[0]["warning_stage_m"])
    assert 0.0 < warning <= stages[-1]


def test_every_curve_row_is_labeled_as_assumption() -> None:
    """假设值不得冒充实测：每行都要 assumption + 可追的"待补"来源。"""

    rows = _curve_rows()
    for row in rows:
        assert row["value_basis"] == "assumption"
        assert row["source_ref"].strip().startswith("待补")
    assert rows[0]["note"].strip(), "首个曲线点必须带假设依据指针"


def test_curve_facility_is_a_real_pump_station() -> None:
    """曲线挂靠的设施必须在排水台账里真实存在且是泵站。"""

    station_ids = _pump_station_ids()
    facilities = {row["facility_id"].strip() for row in _curve_rows()}
    assert facilities
    assert facilities <= station_ids


def test_curve_is_not_registered_as_dataset_asset_yet() -> None:
    """故意未注册：注册要走"资产数 + 指纹 + data_gate 事实"一次改全的流程。"""

    registered = {spec.file_name for spec in URBAN_ASSETS}
    assert CURVE_PATH.name not in registered
