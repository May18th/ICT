"""前池库容假设数据：假设值必须有标注，且不得被当成实测值或已注册资产引用。

为什么要单独测
-------------
`pump_forebay_curves.csv` 是"假设值"台账：它的数字会直接进 `level_pool_route` 产出
"停机后多久到警戒"这类对外可能引用的结论。因此在资产化之前就要有守卫：

1. 曲线自洽：等截面假设下 `storage = plan_area × stage`，水位/容积严格递增，警戒水位在曲线内；
2. 逐行标注：`value_basis=assumption`、`source_ref` 写明"待补"来源，第一条带依据指针；
3. 引用完整：`facility_id` 必须是 `drainage_facilities.csv` 里真实存在的泵站；
4. **已注册资产**：注册后资产数是 19；来源级别由 loader 从 CSV 直接读出，
   `data_gate` 登记的事实级别必须等于 CSV 的 `value_basis`——两处真源不许漂。

这一组测试只读，不写任何文件；数据被误标成 `real` 或注册后计数没同步时立即失败。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from decision_poc.contracts import ProvenanceLevel
from decision_poc.data_gate import build_facts
from decision_poc.quant import level_pool_route, time_to_level
from decision_poc.urban_dataset import URBAN_ASSETS, load_forebay_curves

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


def test_curve_is_registered_as_dataset_asset() -> None:
    """已注册：资产数必须同时是 19，别只加 spec 不同步计数。"""

    registered = {spec.file_name for spec in URBAN_ASSETS}
    assert CURVE_PATH.name in registered
    assert len(URBAN_ASSETS) == 19, "注册前池曲线后资产数应为 19（原 18 + 1）"


def test_loader_returns_provenance_without_caller_input() -> None:
    """贯通的关键：调用方只给目录，来源自己出来。

    如果哪天 loader 又变回"返回裸数组、让调用方自己写 basis"，这条会立刻红——
    那正是"多了个 loader 但没贯通"的形态。
    """

    curves = load_forebay_curves()
    assert set(curves) == {"DF-PS-01"}
    curve = curves["DF-PS-01"]
    assert curve.provenance.basis is ProvenanceLevel.ASSUMPTION
    assert curve.provenance.ref.startswith("待补")
    assert curve.effective_volume_m3 == 300.0
    assert curve.warning_stage_m == 2.4
    assert curve.stage_m[0] == 0.0 and curve.stage_m[-1] == 3.0


def test_data_gate_fact_level_follows_csv_basis() -> None:
    """两处真源不许漂：data_gate 登记的来源级别必须等于 CSV 的 value_basis。"""

    facts, _ = build_facts()
    [fact] = [item for item in facts if item.field == "forebay_effective_volume_m3"]
    basis = {row["value_basis"].strip() for row in _curve_rows()}
    assert len(basis) == 1
    assert fact.provenance.value == basis.pop()
    assert fact.value == 300.0
    assert fact.entity_id == "pump:DF-PS-01"


def test_loader_provenance_reaches_water_level_chain() -> None:
    """最后一跳：只给设施号，来源要一路走到水位过程线与到达时刻。

    "贯通"不是"loader 能返回来源"，而是"算出来的数也带着来源"。调用方在这里只做一件事：
    把 `curve.provenance` 原样传给 `level_pool_route`，不自己写 assumption/real。
    如果哪天 loader 不再返回来源、或 quant 把字段丢掉、或下游不再继承前缀行，这条立刻红。
    """

    curve = load_forebay_curves()["DF-PS-01"]
    hydrograph = level_pool_route(
        inflow_m3h=[1200.0] * 21,
        dt_h=0.01,
        stage_m=curve.stage_m,
        storage_m3=curve.storage_m3,
        initial_stage_m=0.0,
        pump_outflow_m3h=0.0,
        storage_provenance=curve.provenance,
    )
    assert hydrograph.storage_provenance is curve.provenance
    assert any(line.startswith("库容来源：assumption") for line in hydrograph.assumptions)

    reached = time_to_level(hydrograph=hydrograph, target_level_m=curve.warning_stage_m)
    assert reached.computable, "10 分钟窗口内应能到警戒水位"
    assert any(line.startswith("库容来源：assumption") for line in reached.assumptions)
