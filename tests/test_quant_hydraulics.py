"""水力学：满流能力、流速、积水提前量、倒灌流量。

对拍基准：`blender-ue-scene/out/hydraulics-report.md` §1 的满流能力表
（DN600 i=0.30% → 1211 m³/h / 1.19 m/s；DN1200 i=0.15% → 5436 m³/h；DN1400 → 8200 m³/h）。
"""

from __future__ import annotations

import pytest

from decision_poc.quant.hydraulics import (
    manning_full_flow,
    manning_velocity,
    orifice_inflow,
    ponding_forecast,
    weir_inflow,
)


def test_manning_dn600_matches_report() -> None:
    result = manning_full_flow(diameter_m=0.6, slope=0.003)
    assert result.computable is True
    assert result.unit == "m³/h"
    assert result.value == pytest.approx(1211.0, rel=1e-3)


def test_manning_dn1200_matches_report() -> None:
    result = manning_full_flow(diameter_m=1.2, slope=0.0015)
    assert result.value == pytest.approx(5436.0, rel=1e-3)


def test_manning_dn1400_matches_report() -> None:
    result = manning_full_flow(diameter_m=1.4, slope=0.0015)
    assert result.value == pytest.approx(8200.0, rel=1e-3)


def test_manning_velocity_dn600_matches_report() -> None:
    result = manning_velocity(diameter_m=0.6, slope=0.003)
    assert result.computable is True
    assert result.unit == "m/s"
    assert result.value == pytest.approx(1.19, rel=1e-3)


def test_manning_rejects_flat_slope() -> None:
    result = manning_full_flow(diameter_m=0.6, slope=0.0)
    assert result.computable is False
    assert result.value == 0.0
    assert "坡度" in result.uncomputable_reason


def test_manning_rejects_zero_diameter() -> None:
    result = manning_full_flow(diameter_m=0.0, slope=0.003)
    assert result.computable is False


def test_ponding_forecast_gives_hours_to_threshold() -> None:
    # A 区：来水 11138.4 m³/h、DF-PS-01 可用 800 m³/h、积水面积 22.1 ha
    result = ponding_forecast(
        inflow_m3h=11138.4,
        outflow_m3h=800.0,
        pond_area_m2=221000.0,
        current_depth_m=0.0,
        target_depth_m=0.25,
    )
    assert result.computable is True
    assert result.unit == "h"
    assert result.value == pytest.approx(5.344, rel=1e-3)


def test_ponding_forecast_returns_zero_when_already_over_threshold() -> None:
    result = ponding_forecast(
        inflow_m3h=1000.0,
        outflow_m3h=500.0,
        pond_area_m2=10000.0,
        current_depth_m=0.30,
        target_depth_m=0.25,
    )
    assert result.computable is True
    assert result.value == 0.0
    assert any("已达成" in item for item in result.assumptions)


def test_ponding_forecast_refuses_when_pumping_covers_inflow() -> None:
    result = ponding_forecast(
        inflow_m3h=1000.0,
        outflow_m3h=1200.0,
        pond_area_m2=10000.0,
        current_depth_m=0.0,
        target_depth_m=0.25,
    )
    assert result.computable is False
    assert "不再上涨" in result.uncomputable_reason


def test_ponding_forecast_rejects_zero_area() -> None:
    result = ponding_forecast(
        inflow_m3h=1000.0,
        outflow_m3h=0.0,
        pond_area_m2=0.0,
        current_depth_m=0.0,
        target_depth_m=0.25,
    )
    assert result.computable is False


def test_weir_inflow_for_underpass_portal() -> None:
    # 天府广场下穿隧道东端洞口宽 34 m、外部积水高出入口 0.15 m
    result = weir_inflow(width_m=34.0, head_m=0.15)
    assert result.computable is True
    assert result.value == pytest.approx(12088.3, rel=1e-3)


def test_weir_inflow_zero_head_means_no_flow() -> None:
    result = weir_inflow(width_m=34.0, head_m=0.0)
    assert result.computable is True
    assert result.value == 0.0


def test_weir_inflow_rejects_negative_head() -> None:
    result = weir_inflow(width_m=34.0, head_m=-0.1)
    assert result.computable is False


def test_orifice_inflow_for_submerged_opening() -> None:
    result = orifice_inflow(area_m2=10.0, head_m=0.5)
    assert result.computable is True
    assert result.value == pytest.approx(67653.2, rel=1e-3)


def test_orifice_inflow_rejects_zero_area() -> None:
    result = orifice_inflow(area_m2=0.0, head_m=0.5)
    assert result.computable is False
