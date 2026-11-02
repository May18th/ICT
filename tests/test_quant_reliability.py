"""可靠性：泵站不可用概率是一个可复核的并联故障树，不是形容词。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.quant.reliability import (
    FaultTreeResult,
    pump_station_unavailable,
)


def test_station_without_backup_keeps_the_present_risk_level() -> None:
    result = pump_station_unavailable(
        station_id="DF-PS-01",
        running_units=2,
        backup_power=False,
        p_pump_failure=0.05,
        p_grid_failure=0.20,
        p_backup_failure=0.10,
    )
    # 泵组全失效 0.05² = 0.0025；无备电时电源失效 0.20
    assert result.p_pumps_all_fail == pytest.approx(0.0025)
    assert result.p_power_unavailable == pytest.approx(0.20)
    assert result.p_unavailable == pytest.approx(0.202, rel=1e-6)


def test_backup_power_lowers_unavailability() -> None:
    without = pump_station_unavailable(
        station_id="DF-PS-01", running_units=2, backup_power=False
    )
    with_backup = pump_station_unavailable(
        station_id="DF-PS-02", running_units=2, backup_power=True
    )
    assert with_backup.p_unavailable < without.p_unavailable
    assert with_backup.p_power_unavailable == pytest.approx(0.02)


def test_station_with_no_running_pump_is_certainly_unavailable() -> None:
    result = pump_station_unavailable(
        station_id="DF-PS-01", running_units=0, backup_power=False
    )
    assert result.p_pumps_all_fail == 1.0
    assert result.p_unavailable == 1.0


def test_more_running_units_lower_the_risk() -> None:
    one = pump_station_unavailable(station_id="X", running_units=1, backup_power=True)
    three = pump_station_unavailable(station_id="X", running_units=3, backup_power=True)
    assert three.p_unavailable < one.p_unavailable


def test_cut_sets_name_the_backup_power_path() -> None:
    no_backup = pump_station_unavailable(station_id="X", running_units=2, backup_power=False)
    with_backup = pump_station_unavailable(station_id="X", running_units=2, backup_power=True)
    assert ["泵组全失效"] in no_backup.cut_sets
    assert any("无备用电源" in item for cut in no_backup.cut_sets for item in cut)
    assert any("备用电源启动失败" in item for cut in with_backup.cut_sets for item in cut)


def test_assumptions_are_declared() -> None:
    result = pump_station_unavailable(station_id="X", running_units=2, backup_power=True)
    assert len(result.assumptions) >= 3
    assert any("量级占位" in item for item in result.assumptions)


def test_probability_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        pump_station_unavailable(
            station_id="X", running_units=2, backup_power=True, p_grid_failure=1.5
        )


def test_negative_running_units_are_rejected() -> None:
    with pytest.raises(ValueError):
        pump_station_unavailable(station_id="X", running_units=-1, backup_power=True)


def test_inconsistent_probability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FaultTreeResult(
            station_id="X",
            running_units=2,
            backup_power=True,
            p_pumps_all_fail=0.0,
            p_power_unavailable=0.0,
            p_unavailable=0.5,
            cut_sets=[["泵组全失效"]],
            assumptions=["测试"],
        )
