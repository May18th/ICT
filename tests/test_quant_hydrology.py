"""水文学：推理公式与雨强换算，数字对齐 blender-ue-scene 的稳态校核报告。

对拍基准：`blender-ue-scene/out/hydraulics-report.md` §3 的四区设计流量表
（A 区 22.1 ha、ψ=0.7、q=200 L/(s·ha) → 3.09 m³/s）。
"""

from __future__ import annotations

import pytest

from decision_poc.quant.hydrology import (
    MM_PER_H_TO_LPS_HA,
    RAINFALL_SCENARIOS_MM_2H,
    intensity_from_depth,
    rational_runoff,
    runoff_from_rainfall,
)


def test_mm_per_hour_to_lps_ha_constant() -> None:
    # 1 mm/h × 1 ha = 10 m³/h = 10/3.6 L/s
    assert MM_PER_H_TO_LPS_HA == pytest.approx(2.777778, rel=1e-6)


def test_intensity_from_depth_uses_mean_rainfall() -> None:
    result = intensity_from_depth(depth_mm=80.0, duration_h=2.0)
    assert result.computable is True
    assert result.unit == "L/(s·ha)"
    assert result.value == pytest.approx(111.1111, rel=1e-4)


def test_intensity_matches_equivalent_single_hour() -> None:
    a = intensity_from_depth(depth_mm=80.0, duration_h=2.0)
    b = intensity_from_depth(depth_mm=40.0, duration_h=1.0)
    assert a.value == pytest.approx(b.value, rel=1e-9)


def test_intensity_rejects_zero_duration() -> None:
    result = intensity_from_depth(depth_mm=80.0, duration_h=0.0)
    assert result.computable is False
    assert result.value == 0.0
    assert "历时" in result.uncomputable_reason


def test_rational_runoff_matches_hydraulics_report() -> None:
    # 报告 §3：A 区 22.1 ha、ψ=0.7、q=200 → 3.09 m³/s
    result = rational_runoff(runoff_coefficient=0.7, intensity_lps_ha=200.0, area_ha=22.1)
    assert result.computable is True
    assert result.inputs["flow_m3s"] == pytest.approx(3.094, rel=1e-3)
    assert result.value == pytest.approx(11138.4, rel=1e-3)


def test_rational_runoff_rejects_out_of_range_coefficient() -> None:
    result = rational_runoff(runoff_coefficient=1.2, intensity_lps_ha=200.0, area_ha=22.1)
    assert result.computable is False
    assert "径流系数" in result.uncomputable_reason


def test_rational_runoff_rejects_negative_area() -> None:
    result = rational_runoff(runoff_coefficient=0.7, intensity_lps_ha=200.0, area_ha=-1.0)
    assert result.computable is False


def test_runoff_from_rainfall_chains_intensity_and_formula() -> None:
    # 80 mm/2h → 40 mm/h → 111.111 L/(s·ha)；0.7×111.111×22.1 = 1.7189 m³/s
    result = runoff_from_rainfall(
        depth_mm=80.0,
        duration_h=2.0,
        runoff_coefficient=0.7,
        area_ha=22.1,
    )
    assert result.computable is True
    assert result.value == pytest.approx(6188.0, rel=1e-3)
    assert result.formula_id == "Q-RUNOFF-RATIONAL"
    assert "depth_mm" in result.inputs


def test_scenarios_cover_forecast_error_range() -> None:
    assert RAINFALL_SCENARIOS_MM_2H == (60.0, 80.0, 100.0)
    flows = [
        runoff_from_rainfall(
            depth_mm=depth,
            duration_h=2.0,
            runoff_coefficient=0.7,
            area_ha=22.1,
        ).value
        for depth in RAINFALL_SCENARIOS_MM_2H
    ]
    assert flows == sorted(flows)
