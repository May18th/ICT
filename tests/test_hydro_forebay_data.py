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
    """等截面矩形前池假设下，容积必须严格等于面积×水位。

    线性只在 `value_basis=assumption` 时强制：等截面是**假设**的一部分，不是数据契约。
    实测曲线若有斜壁/台阶会非线性，那时应换数据、不动模型，不能因为这条测试红了就删测试。
    """

    rows = _curve_rows()
    assert len(rows) >= 3
    stages = [float(row["stage_m"]) for row in rows]
    storages = [float(row["storage_m3"]) for row in rows]
    assert stages == sorted(stages) and len(set(stages)) == len(stages)
    assert storages == sorted(storages) and len(set(storages)) == len(storages)
    for row in rows:
        area = float(row["plan_area_m2"])
        storage = float(row["storage_m3"])
        assert area > 0.0
        if row["value_basis"] == "assumption":
            assert storage == pytest.approx(area * float(row["stage_m"]), abs=1e-6)
        else:
            assert storage >= 0.0
    warning = float(rows[0]["warning_stage_m"])
    assert 0.0 < warning <= stages[-1]
    if rows[0]["value_basis"] == "assumption":
        # 假设值下警戒必须留出缓冲；等于池顶就等于没有警戒
        assert warning < stages[-1]


def test_every_curve_row_is_labeled_as_assumption() -> None:
    """假设值不得冒充实测：来源级别与出处必须自洽，且不允许同一曲线混用真/假设。

    这条守卫两个方向都要拦：假设行必须写「待补」，实测行必须给真实出处且不得再写「待补」。
    这样补齐图纸后换成 `real` + 图纸号可以直接通过，不需要为了变绿而改测试。
    """

    rows = _curve_rows()
    bases = {row["value_basis"] for row in rows}
    assert len(bases) == 1, f"同一库容曲线不允许混用来源级别：{sorted(bases)}"
    basis = bases.pop()
    assert basis in {"assumption", "derived", "real"}
    for row in rows:
        ref = row["source_ref"].strip()
        if basis == "assumption":
            assert ref.startswith("待补"), f"假设行必须写「待补：…」，实际是 {ref!r}"
        else:
            assert ref and not ref.startswith("待补"), "实测/推算来源必须给真实出处"
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
