"""场景 v2 口径：下穿隧道倒灌 + 泵站负荷。

这组测试有一个特别的目的：把"换成 v2 泵站口径会动多少数字"算成断言。
如果有人将来直接把 v2 接进六跳，这条测试会立刻告诉他缺口会从 1 台变成 0 台、
C3 资源争用会消失——那不是改一行能收场的事。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.scenario_v2 import (
    UnderpassSpec,
    backflow_state,
    compare_pump_requirement,
    load_pump_rows,
    load_underpasses,
    pump_load,
    underpass_closure_required,
    underpass_pump_demand,
)

UNIT_CAPACITY_M3H = 400.0
#: `samples/assets/urban/resource_inventory.csv`：A-03 4 台 + B-01 2 台
AVAILABLE_PUMPS = 6
#: `samples/assets/urban/procedure_waterlogging.md` §2.3：单处基坑 2 台
PUMPS_PER_PIT = 2
#: `procedure_waterlogging.md`：B-01 两处低洼点 × 每处 1 台
LOWSPOT_PUMPS_B01 = 2


def test_load_underpasses_reads_the_v2_ledger() -> None:
    specs = load_underpasses()
    assert set(specs) == {"UP-XH-101"}
    spec = specs["UP-XH-101"]
    assert spec.road_id == "R-101"
    assert spec.district == "A-03"
    assert spec.has_pump_room is False
    assert spec.entry_sill_height_m == pytest.approx(0.15)
    assert spec.close_threshold_m == pytest.approx(0.25)


def test_load_underpasses_rejects_duplicate_ids(tmp_path) -> None:
    header = (
        "underpass_id,name,road_id,district,entry_sill_height_m,lowest_point_z_m,"
        "ramp_length_m,opening_width_m,open_water_area_m2,has_pump_room,close_threshold_m,owner_org,note\n"
    )
    row = "UP-1,测试,R-101,A-03,0.15,-1.8,96,34,3264,false,0.25,,x\n"
    (tmp_path / "underpasses.csv").write_text(header + row + row, encoding="utf-8")
    with pytest.raises(ValueError):
        load_underpasses(tmp_path)


def _spec(**overrides: object) -> UnderpassSpec:
    data: dict[str, object] = {
        "underpass_id": "UP-T",
        "name": "测试通道",
        "road_id": "R-101",
        "district": "A-03",
        "entry_sill_height_m": 0.15,
        "lowest_point_z_m": -1.81,
        "ramp_length_m": 96.0,
        "opening_width_m": 34.0,
        "open_water_area_m2": 3264.0,
        "has_pump_room": False,
        "close_threshold_m": 0.25,
    }
    data.update(overrides)
    return UnderpassSpec(**data)  # type: ignore[arg-type]


def test_spec_requires_below_grade_low_point() -> None:
    with pytest.raises(ValidationError):
        _spec(lowest_point_z_m=0.2)


def test_spec_requires_closure_threshold_above_sill() -> None:
    with pytest.raises(ValidationError):
        _spec(close_threshold_m=0.10)


def test_backflow_high_when_water_over_tops_sill() -> None:
    result = backflow_state(external_depth_m=0.20, sill_height_m=0.15, opening_width_m=34.0)
    assert result.level == "high"
    assert result.overtopping_head_m == pytest.approx(0.05)
    assert result.inflow is not None
    assert result.inflow.value == pytest.approx(2326.3, rel=1e-3)


def test_backflow_medium_at_sixty_percent_of_sill() -> None:
    result = backflow_state(external_depth_m=0.10, sill_height_m=0.15, opening_width_m=34.0)
    assert result.level == "medium"
    assert result.inflow is None


def test_backflow_none_when_water_is_low() -> None:
    result = backflow_state(external_depth_m=0.05, sill_height_m=0.15, opening_width_m=34.0)
    assert result.level == "none"
    assert result.overtopping_head_m == 0.0


def test_backflow_rejects_negative_depth() -> None:
    with pytest.raises(ValueError):
        backflow_state(external_depth_m=-0.1, sill_height_m=0.15, opening_width_m=34.0)


def test_backflow_rejects_non_positive_width() -> None:
    with pytest.raises(ValueError):
        backflow_state(external_depth_m=0.20, sill_height_m=0.15, opening_width_m=0.0)


def test_closure_required_at_threshold() -> None:
    assert underpass_closure_required(water_depth_m=0.25, close_threshold_m=0.25) is True


def test_closure_not_required_below_threshold() -> None:
    assert underpass_closure_required(water_depth_m=0.20, close_threshold_m=0.25) is False


def test_closure_requires_positive_threshold() -> None:
    with pytest.raises(ValueError):
        underpass_closure_required(water_depth_m=0.30, close_threshold_m=0.0)


def test_pump_load_overload_for_faulty_station() -> None:
    load = pump_load(
        station_id="DF-PS-01",
        rated_m3h=1200.0,
        failed_unit_m3h=400.0,
        design_inflow_m3h=1200.0,
    )
    assert load.capacity_avail_m3h == pytest.approx(800.0)
    assert load.load_ratio == pytest.approx(1.5)
    assert load.state == "overload"


def test_pump_load_high_load_at_exactly_rated_inflow() -> None:
    load = pump_load(
        station_id="DF-PS-02",
        rated_m3h=2400.0,
        failed_unit_m3h=0.0,
        design_inflow_m3h=2400.0,
    )
    assert load.load_ratio == pytest.approx(1.0)
    assert load.state == "high_load"


def test_pump_load_rejects_failed_unit_above_rated() -> None:
    with pytest.raises(ValidationError):
        pump_load(
            station_id="DF-PS-X",
            rated_m3h=100.0,
            failed_unit_m3h=200.0,
            design_inflow_m3h=100.0,
        )


def test_pump_load_rejects_zero_capacity() -> None:
    with pytest.raises(ValidationError):
        pump_load(
            station_id="DF-PS-X",
            rated_m3h=100.0,
            failed_unit_m3h=100.0,
            design_inflow_m3h=100.0,
        )


def test_load_pump_rows_reads_three_stations() -> None:
    rows = load_pump_rows()
    assert set(rows) == {"DF-PS-01", "DF-PS-02", "DF-PS-03"}
    assert float(rows["DF-PS-01"]["failed_unit_m3h"]) == pytest.approx(400.0)


def test_v1_vs_v2_pump_requirement_for_faulty_station() -> None:
    comparison = compare_pump_requirement(
        station_id="DF-PS-01",
        rated_m3h=1200.0,
        failed_unit_m3h=400.0,
        design_inflow_m3h=1200.0,
        derate=0.8,
        unit_capacity_m3h=UNIT_CAPACITY_M3H,
    )
    assert comparison.v1_pumps == 3
    assert comparison.v2_pumps == 1
    assert comparison.delta == -2


def test_v1_vs_v2_pump_requirement_for_healthy_station() -> None:
    comparison = compare_pump_requirement(
        station_id="DF-PS-03",
        rated_m3h=1800.0,
        failed_unit_m3h=0.0,
        design_inflow_m3h=1800.0,
        derate=0.8,
        unit_capacity_m3h=UNIT_CAPACITY_M3H,
    )
    assert comparison.v1_pumps == 0
    assert comparison.v2_pumps == 0


def test_compare_rejects_non_positive_derate() -> None:
    with pytest.raises(ValueError):
        compare_pump_requirement(
            station_id="DF-PS-01",
            rated_m3h=1200.0,
            failed_unit_m3h=400.0,
            design_inflow_m3h=1200.0,
            derate=0.0,
            unit_capacity_m3h=UNIT_CAPACITY_M3H,
        )


def test_switching_to_v2_would_erase_the_resource_gap() -> None:
    """把两种口径各算一遍缺口：v1 缺口 1 台（现状），v2 缺口 0 台。

    结论：**v2 不能直接替换 v1 的需求折算**，否则 C3 资源争用与 P9 分配叙事
    同时失去事实基础。要么保留 v1 折算、把负荷做成风险信号；要么连同 12 个
    P9 分配案例一起重标。这条测试就是那个决策的证据。
    """

    rows = load_pump_rows()

    def station_pumps(station_id: str, *, v2: bool) -> int:
        row = rows[station_id]
        rated = float(row["rated_m3h"])
        failed = float(row["failed_unit_m3h"])
        inflow = float(row["design_inflow_m3h"])
        comparison = compare_pump_requirement(
            station_id=station_id,
            rated_m3h=rated,
            failed_unit_m3h=failed,
            design_inflow_m3h=inflow,
            derate=0.8,
            unit_capacity_m3h=UNIT_CAPACITY_M3H,
        )
        return comparison.v2_pumps if v2 else comparison.v1_pumps

    # A-03：故障泵站需求 + 1 处基坑；B-01：泵站正常 + 2 处低洼点
    required_v1 = station_pumps("DF-PS-01", v2=False) + PUMPS_PER_PIT
    required_v2 = station_pumps("DF-PS-01", v2=True) + PUMPS_PER_PIT
    required_v1 += LOWSPOT_PUMPS_B01
    required_v2 += LOWSPOT_PUMPS_B01

    assert required_v1 == 7
    assert required_v2 == 5
    assert required_v1 - AVAILABLE_PUMPS == 1
    assert max(0, required_v2 - AVAILABLE_PUMPS) == 0


def test_underpass_pump_demand_needs_one_mobile_pump() -> None:
    spec = load_underpasses()["UP-XH-101"]
    demand = underpass_pump_demand(spec)
    # 敞口 3264 m² × 平均水深 (1.81+0.15)/2 = 0.98 m ≈ 3199 m³；10 h 排空 ≈ 320 m³/h → 1 台
    assert demand.drain_volume_m3 == pytest.approx(3198.7, rel=1e-3)
    assert demand.demand_flow_m3h == pytest.approx(319.9, rel=1e-3)
    assert demand.pumps == 1
    assert len(demand.basis) == 2
    assert demand.assumption_note


def test_underpass_with_pump_room_takes_no_mobile_pump() -> None:
    spec = load_underpasses()["UP-XH-101"].model_copy(update={"has_pump_room": True})
    demand = underpass_pump_demand(spec)
    assert demand.pumps == 0
    assert "自带泵房" in demand.basis[0]


def test_underpass_pump_demand_rejects_non_positive_drain_hours() -> None:
    spec = load_underpasses()["UP-XH-101"]
    with pytest.raises(ValueError):
        underpass_pump_demand(spec, drain_hours=0.0)


def test_underpass_pump_demand_scales_with_drain_time() -> None:
    spec = load_underpasses()["UP-XH-101"]
    fast = underpass_pump_demand(spec, drain_hours=2.0)
    slow = underpass_pump_demand(spec, drain_hours=20.0)
    assert fast.pumps > slow.pumps
    assert slow.pumps == 1
