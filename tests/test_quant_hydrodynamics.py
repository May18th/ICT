"""简化水动力演进：马斯京根汇流、修正 Puls 调蓄、过程线查询。

对拍方式：全部用手算值核对，不拿函数自己的输出当基准。
- 马斯京根系数与两步演进：按 C₀/C₁/C₂ 定义直接手算（K=2 h、X=0.2、Δt=2 h）；
- 线性库容调蓄：面积 1000 m²、Δt=0.1 h 时每步精确涨 0.4 m；
- 溢流稳定水位：由稳态条件 O(h) = I 反解，与演进结果对照；
- 插值时刻：按相邻两点线性插值手算。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.quant.contracts import HydrographResult
from decision_poc.quant.hydrodynamics import (
    level_exceedance_duration,
    level_pool_route,
    muskingum_coefficients,
    muskingum_route,
    time_to_level,
)


def _hydrograph(
    values: list[float],
    times: list[float] | None = None,
    *,
    unit: str = "m",
) -> HydrographResult:
    """手工构造一条过程线，用于隔离测试插值与超阈统计。"""

    if times is None:
        times = [round(index * 0.1, 10) for index in range(len(values))]
    peak = max(values)
    return HydrographResult(
        times_h=times,
        values=values,
        unit=unit,
        formula_id="H-TEST",
        formula_source="测试构造",
        assumptions=["测试用构造过程线"],
        inputs={},
        error_bound="测试",
        peak=peak,
        peak_time_h=times[values.index(peak)],
    )


def _linear_pool(**overrides: object):
    """1000 m² 平底调蓄空间：S(h) = 1000·h。"""

    arguments: dict[str, object] = {
        "inflow_m3h": [5000.0, 5000.0, 5000.0, 5000.0],
        "dt_h": 0.1,
        "stage_m": [0.0, 1.0, 2.0, 3.0, 4.0],
        "storage_m3": [0.0, 1000.0, 2000.0, 3000.0, 4000.0],
        "initial_stage_m": 0.0,
        "pump_outflow_m3h": 1000.0,
    }
    arguments.update(overrides)
    return level_pool_route(**arguments)  # type: ignore[arg-type]


# --- 马斯京根 -------------------------------------------------------------


def test_muskingum_coefficients_match_hand_calc() -> None:
    coefficients = muskingum_coefficients(travel_time_h=2.0, weighting_x=0.2, dt_h=2.0)
    # D = 2×0.8 + 1 = 2.6；c0 = (1−0.4)/2.6；c1 = (1+0.4)/2.6；c2 = (1.6−1)/2.6
    assert coefficients.c0 == pytest.approx(0.2307692308, rel=1e-9)
    assert coefficients.c1 == pytest.approx(0.5384615385, rel=1e-9)
    assert coefficients.c2 == pytest.approx(0.2307692308, rel=1e-9)
    assert coefficients.c0 + coefficients.c1 + coefficients.c2 == pytest.approx(1.0)
    assert coefficients.stable is True


def test_muskingum_coefficients_reject_nonphysical_parameters() -> None:
    with pytest.raises(ValueError):
        muskingum_coefficients(travel_time_h=0.0, weighting_x=0.2, dt_h=2.0)
    with pytest.raises(ValueError):
        muskingum_coefficients(travel_time_h=2.0, weighting_x=0.6, dt_h=2.0)
    with pytest.raises(ValueError):
        muskingum_coefficients(travel_time_h=2.0, weighting_x=0.2, dt_h=0.0)


def test_muskingum_flags_unstable_time_step() -> None:
    # 2KX = 0.8，2K(1−X) = 1.2，Δt = 5 超出上限 → c2 为负
    coefficients = muskingum_coefficients(travel_time_h=1.0, weighting_x=0.4, dt_h=5.0)
    assert coefficients.stable is False
    assert coefficients.c2 < 0.0


def test_muskingum_route_two_steps_match_hand_calc() -> None:
    result = muskingum_route(
        inflow_m3h=[0.0, 1000.0, 1000.0],
        travel_time_h=2.0,
        weighting_x=0.2,
        dt_h=2.0,
    )
    assert result.computable is True
    assert result.unit == "m³/h"
    assert result.values[0] == 0.0
    assert result.values[1] == pytest.approx(230.7692308, rel=1e-9)
    # O₃ = c0·1000 + c1·1000 + c2·O₂ = 230.7692 + 538.4615 + 53.2544
    assert result.values[2] == pytest.approx(822.4852071, rel=1e-9)


def test_muskingum_route_attenuates_and_lags_peak() -> None:
    # Δt = 1 h：c0 = 0.1/2.1，c1 = 0.9/2.1，c2 = 1.1/2.1
    result = muskingum_route(
        inflow_m3h=[0.0, 500.0, 1000.0, 1500.0, 1000.0, 500.0, 0.0],
        travel_time_h=2.0,
        weighting_x=0.2,
        dt_h=1.0,
    )
    assert result.computable is True
    assert result.peak == pytest.approx(1027.6633707, rel=1e-9)
    assert result.peak_time_h == pytest.approx(4.0)
    # 峰值削减 1 − 1027.6634/1500；峰现滞后 4 − 3 = 1 h
    assert result.summary["peak_attenuation_ratio"] == pytest.approx(0.3148911, rel=1e-6)
    assert result.summary["peak_lag_h"] == pytest.approx(1.0)


def test_muskingum_route_refuses_unstable_time_step() -> None:
    result = muskingum_route(
        inflow_m3h=[0.0, 500.0, 1000.0],
        travel_time_h=1.0,
        weighting_x=0.4,
        dt_h=5.0,
    )
    assert result.computable is False
    assert result.values == []
    assert "稳定" in result.uncomputable_reason


def test_muskingum_route_refuses_negative_inflow() -> None:
    result = muskingum_route(
        inflow_m3h=[0.0, -100.0, 200.0],
        travel_time_h=2.0,
        weighting_x=0.2,
        dt_h=1.0,
    )
    assert result.computable is False
    assert "不能为负" in result.uncomputable_reason


# --- 修正 Puls 调蓄 -------------------------------------------------------


def test_level_pool_route_matches_linear_hand_calc() -> None:
    result = _linear_pool()
    assert result.computable is True
    assert result.unit == "m"
    # 净流量 5000−1000 = 4000 m³/h × 0.1 h = 400 m³ → 每步 +0.4 m
    assert result.values == pytest.approx([0.0, 0.4, 0.8, 1.2], abs=1e-7)


def test_level_pool_route_with_pump_stopped() -> None:
    result = _linear_pool(pump_outflow_m3h=0.0)
    # 泵站停机：净流量 5000 m³/h → 每步 +0.5 m
    assert result.values == pytest.approx([0.0, 0.5, 1.0, 1.5], abs=1e-7)


def test_level_pool_route_accepts_pump_sequence() -> None:
    # 节点 0 仍在抽排、节点 1 起停机：第 1 时段按 (1000+0)/2 的均值算 → +0.45 m，
    # 之后每步净流量 5000 m³/h → +0.5 m
    result = _linear_pool(pump_outflow_m3h=[1000.0, 0.0, 0.0, 0.0])
    assert result.values == pytest.approx([0.0, 0.45, 0.95, 1.45], abs=1e-7)


def test_level_pool_route_spillway_reaches_steady_level() -> None:
    result = level_pool_route(
        inflow_m3h=[20000.0] * 120,
        dt_h=0.05,
        stage_m=[0.0, 1.0, 2.0, 3.0, 4.0],
        storage_m3=[0.0, 1000.0, 2000.0, 3000.0, 4000.0],
        initial_stage_m=0.0,
        pump_outflow_m3h=1000.0,
        spill_crest_m=1.0,
        spill_width_m=5.0,
    )
    assert result.computable is True
    # 稳态由 O(h) = I 反解：1.7×5×(h−1)^1.5×3600 = 19000 → h = 1.72780
    assert result.values[-1] == pytest.approx(1.72780, abs=0.01)
    assert result.peak < 2.0


def test_level_pool_route_refuses_overflow() -> None:
    result = level_pool_route(
        inflow_m3h=[1_000_000.0, 1_000_000.0],
        dt_h=0.1,
        stage_m=[0.0, 1.0, 2.0],
        storage_m3=[0.0, 1000.0, 2000.0],
        initial_stage_m=0.0,
        pump_outflow_m3h=0.0,
    )
    assert result.computable is False
    assert result.values == []
    assert "上限" in result.uncomputable_reason


def test_level_pool_route_refuses_drop_below_curve() -> None:
    result = level_pool_route(
        inflow_m3h=[0.0, 0.0],
        dt_h=0.1,
        stage_m=[0.0, 1.0, 2.0],
        storage_m3=[0.0, 1000.0, 2000.0],
        initial_stage_m=0.0,
        pump_outflow_m3h=1000.0,
    )
    assert result.computable is False
    assert "下界" in result.uncomputable_reason


def test_level_pool_route_refuses_mismatched_curve() -> None:
    result = level_pool_route(
        inflow_m3h=[5000.0, 5000.0],
        dt_h=0.1,
        stage_m=[0.0, 1.0],
        storage_m3=[0.0, 1000.0, 2000.0],
        initial_stage_m=0.0,
    )
    assert result.computable is False
    assert "等长" in result.uncomputable_reason


def test_level_pool_route_refuses_mismatched_pump_sequence() -> None:
    result = level_pool_route(
        inflow_m3h=[5000.0, 5000.0, 5000.0],
        dt_h=0.1,
        stage_m=[0.0, 1.0, 2.0],
        storage_m3=[0.0, 1000.0, 2000.0],
        initial_stage_m=0.0,
        pump_outflow_m3h=[1000.0, 0.0],
    )
    assert result.computable is False
    assert "长度" in result.uncomputable_reason


# --- 过程线查询 -----------------------------------------------------------


def test_time_to_level_interpolates_between_steps() -> None:
    result = time_to_level(hydrograph=_linear_pool(), target_level_m=1.0)
    # 水位 0.8 m 在 0.2 h、1.2 m 在 0.3 h → 1.0 m 在 0.25 h
    assert result.computable is True
    assert result.unit == "h"
    assert result.value == pytest.approx(0.25, abs=1e-9)


def test_time_to_level_returns_zero_when_target_already_met() -> None:
    result = time_to_level(hydrograph=_linear_pool(), target_level_m=0.0)
    assert result.computable is True
    assert result.value == 0.0
    assert any("已达成" in item for item in result.assumptions)


def test_time_to_level_refuses_unreachable_target() -> None:
    result = time_to_level(hydrograph=_linear_pool(), target_level_m=5.0)
    assert result.computable is False
    assert "未达到" in result.uncomputable_reason


def test_level_exceedance_duration_matches_hand_calc() -> None:
    hydrograph = _hydrograph([0.2, 0.6, 1.1, 0.9, 0.4])
    result = level_exceedance_duration(hydrograph=hydrograph, threshold_m=1.0)
    # 上穿：0.6→1.1 之间，t = 0.1 + 0.8×0.1 = 0.18；下穿：1.1→0.9 之间，t = 0.2 + 0.5×0.1 = 0.25
    assert result.computable is True
    assert result.value == pytest.approx(0.07, abs=1e-9)


def test_level_exceedance_duration_is_zero_when_never_exceeded() -> None:
    result = level_exceedance_duration(hydrograph=_hydrograph([0.1, 0.2, 0.3]), threshold_m=1.0)
    assert result.computable is True
    assert result.value == 0.0


def test_level_exceedance_duration_counts_to_series_end_when_still_high() -> None:
    result = level_exceedance_duration(hydrograph=_hydrograph([0.2, 1.1, 1.2]), threshold_m=1.0)
    # 上穿 t = (1.0−0.2)/(1.1−0.2) × 0.1；到末尾 0.2 h 仍未回落
    assert result.computable is True
    assert result.value == pytest.approx(0.2 - 0.8 / 0.9 * 0.1, abs=1e-9)


def test_time_to_level_refuses_uncomputable_hydrograph() -> None:
    broken = muskingum_route(
        inflow_m3h=[0.0, 500.0, 1000.0],
        travel_time_h=1.0,
        weighting_x=0.4,
        dt_h=5.0,
    )
    assert broken.computable is False
    result = time_to_level(hydrograph=broken, target_level_m=1.0)
    assert result.computable is False
    assert "不可计算" in result.uncomputable_reason


# --- 契约 -----------------------------------------------------------------


def test_hydrograph_contract_rejects_length_mismatch() -> None:
    with pytest.raises(ValidationError):
        HydrographResult(
            times_h=[0.0, 0.1, 0.2],
            values=[0.0, 0.1],
            unit="m",
            formula_id="H-TEST",
            formula_source="测试构造",
            assumptions=["测试"],
            error_bound="测试",
            peak=0.1,
            peak_time_h=0.1,
        )


def test_hydrograph_contract_rejects_blank_assumption() -> None:
    with pytest.raises(ValidationError):
        _hydrograph_values_with_assumption(["测试", "  "])


def _hydrograph_values_with_assumption(assumptions: list[str]) -> HydrographResult:
    return HydrographResult(
        times_h=[0.0, 0.1],
        values=[0.0, 0.1],
        unit="m",
        formula_id="H-TEST",
        formula_source="测试构造",
        assumptions=assumptions,
        error_bound="测试",
        peak=0.1,
        peak_time_h=0.1,
    )


def test_hydrograph_contract_rejects_series_when_uncomputable() -> None:
    with pytest.raises(ValidationError):
        HydrographResult(
            times_h=[0.0, 0.1],
            values=[0.0, 0.1],
            unit="m",
            formula_id="H-TEST",
            formula_source="测试构造",
            assumptions=["测试"],
            error_bound="测试",
            computable=False,
            uncomputable_reason="缺少库容曲线",
        )


def test_hydrograph_contract_rejects_wrong_peak() -> None:
    with pytest.raises(ValidationError):
        HydrographResult(
            times_h=[0.0, 0.1],
            values=[0.0, 0.1],
            unit="m",
            formula_id="H-TEST",
            formula_source="测试构造",
            assumptions=["测试"],
            error_bound="测试",
            peak=0.9,
            peak_time_h=0.1,
        )


def test_every_computable_result_carries_source_and_assumptions() -> None:
    results = [
        muskingum_route(
            inflow_m3h=[0.0, 500.0, 1000.0, 500.0, 0.0],
            travel_time_h=2.0,
            weighting_x=0.2,
            dt_h=1.0,
        ),
        _linear_pool(),
    ]
    for result in results:
        assert result.computable is True
        assert result.formula_id
        assert result.formula_source
        assert len(result.assumptions) >= 3
        assert all(item.strip() for item in result.assumptions)
