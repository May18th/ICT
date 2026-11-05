"""简化水动力演进：把"稳态能力"升级成"过程线"，但明确不碰圣维南。

与 `hydraulics.py` 的分工必须先看清：

- `hydraulics.py` 是**稳态**：满流能力、恒定净流量下的积水速率与提前量；
- 本模块是**过程线级别的简化演进**：给定入流过程线与参数，算出出流/水位过程线，
  从而回答"峰值削减多少、峰现滞后多久、泵站停机后水位怎么走、什么时候到警戒水位、超警多久"。

两种经典方法（参数全部由调用方给定，本层不做任何几何或率定假设）：

1. **马斯京根法**：线性槽蓄演进，`O₂ = C₀·I₂ + C₁·I₁ + C₂·O₁`，含稳定性条件；
2. **修正 Puls 法**：按库容曲线做调蓄演进，可叠加恒定抽排与溢流堰。

**它不是"水动力模型的替代品"**：

- 不是圣维南非恒定流完整方程，没有壅水/回水计算，不适用于强回水、超临界流与漫滩；
- 不含 2D 浅水与 1D/2D 耦合，不做地表漫流推演；
- K、X 与库容曲线都不是本工程实测/率定值，每个结果的 `assumptions` 与 `error_bound`
  必须写明"未率定"——**答辩里不许把本层输出说成 SWMM 级推演**。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel
from decision_poc.quant.contracts import (
    HydrographResult,
    QuantResult,
    uncomputable,
    uncomputable_hydrograph,
)
from decision_poc.quant.hydraulics import DEFAULT_WEIR_COEFFICIENT

SOURCE_MUSKINGUM = (
    "马斯京根法（Muskingum）线性槽蓄演进 S = K·[X·I + (1−X)·O]，"
    "O₂ = C₀·I₂ + C₁·I₁ + C₂·O₁（工程水文学 / USACE HEC-HMS 通用方法，K、X 未按本工程率定）"
)
SOURCE_LEVEL_POOL = (
    "修正 Puls 法（storage-indication）：S(h₂)/Δt + O(h₂)/2 = S(h₁)/Δt − O(h₁)/2 + (I₁+I₂)/2；"
    "溢流沿用宽顶堰 Q = C·b·h^(3/2)"
)
SOURCE_LEVEL_CROSS = "离散过程线线性插值：在相邻时段之间按水位线性插值求到达/回落时刻"

MUSKINGUM_STABILITY_NOTE = "稳定性条件 2·K·X ≤ Δt ≤ 2·K·(1−X)；不满足时系数为负，会演进出不物理的负出流"

SECONDS_PER_HOUR = 3600.0
_BISECTION_TOLERANCE_M = 1e-10
_FLOW_TOLERANCE_M3H = 1e-6


class MuskingumCoefficients(StrictModel):
    """马斯京根系数与稳定性判定：系数本身也要可复核，不能藏在函数里。"""

    travel_time_h: float = Field(gt=0)
    weighting_x: float = Field(ge=0.0, le=0.5)
    dt_h: float = Field(gt=0)
    c0: float
    c1: float
    c2: float
    stable: bool
    formula_source: str = SOURCE_MUSKINGUM
    assumptions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _coefficients_are_consistent(self) -> MuskingumCoefficients:
        if abs((self.c0 + self.c1 + self.c2) - 1.0) > 1e-9:
            raise ValueError("三个系数之和必须为 1")
        expected_stable = all(value >= 0.0 for value in (self.c0, self.c1, self.c2))
        if expected_stable != self.stable:
            raise ValueError("stable 必须与系数是否全为非负一致")
        if any(not item.strip() for item in self.assumptions):
            raise ValueError("assumptions 不允许出现空条目")
        return self


def muskingum_coefficients(
    *,
    travel_time_h: float,
    weighting_x: float,
    dt_h: float,
) -> MuskingumCoefficients:
    """算马斯京根系数，并判定计算时段是否落在稳定区间内。"""

    if travel_time_h <= 0:
        raise ValueError("传播时间 K 必须大于 0")
    if not 0.0 <= weighting_x <= 0.5:
        raise ValueError("楔蓄权重 X 必须在 [0, 0.5] 内")
    if dt_h <= 0:
        raise ValueError("计算时段 Δt 必须大于 0")

    denominator = travel_time_h * (1.0 - weighting_x) + dt_h / 2.0
    c0 = (dt_h / 2.0 - travel_time_h * weighting_x) / denominator
    c1 = (dt_h / 2.0 + travel_time_h * weighting_x) / denominator
    c2 = (travel_time_h * (1.0 - weighting_x) - dt_h / 2.0) / denominator
    stable = all(value >= 0.0 for value in (c0, c1, c2))
    assumptions = [
        "线性槽蓄假定：S 与 I、O 呈线性关系",
        MUSKINGUM_STABILITY_NOTE,
    ]
    return MuskingumCoefficients(
        travel_time_h=float(travel_time_h),
        weighting_x=float(weighting_x),
        dt_h=float(dt_h),
        c0=c0,
        c1=c1,
        c2=c2,
        stable=stable,
        assumptions=assumptions,
    )


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """分段线性插值；假定 xs 严格递增且 x 落在 [xs[0], xs[-1]] 内。"""

    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    ratio = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + ratio * (ys[hi] - ys[lo])


def _peak_and_time(times: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    peak = max(values)
    return peak, times[values.index(peak)]


def muskingum_route(
    *,
    inflow_m3h: Sequence[float],
    travel_time_h: float,
    weighting_x: float,
    dt_h: float,
) -> HydrographResult:
    """马斯京根汇流演进：给定入流过程线，给出出流过程线、峰值削减与峰现滞后。"""

    inflow = [float(value) for value in inflow_m3h]
    inputs = {
        "travel_time_h": float(travel_time_h),
        "weighting_x": float(weighting_x),
        "dt_h": float(dt_h),
        "inflow_peak_m3h": max(inflow) if inflow else 0.0,
    }
    assumptions = [
        "K（传播时间）与 X（楔蓄权重）由调用方给定，本工程未率定",
        "线性马斯京根：假定槽蓄与流量线性相关，不适用于强回水、超临界流与漫滩",
        "初始时段按稳态起算（O₁ = I₁），缺初始出流时无法更好处理",
        "不计区间入流、旁侧出流与沿程损失",
    ]
    if len(inflow) < 2:
        return uncomputable_hydrograph(
            formula_id="Q-MUSKINGUM",
            formula_source=SOURCE_MUSKINGUM,
            assumptions=assumptions,
            inputs=inputs,
            reason="入流过程线至少需要两个时段",
            unit="m³/h",
        )
    if any(value < 0.0 for value in inflow):
        return uncomputable_hydrograph(
            formula_id="Q-MUSKINGUM",
            formula_source=SOURCE_MUSKINGUM,
            assumptions=assumptions,
            inputs=inputs,
            reason="入流不能为负；反向流不在本层适用范围",
            unit="m³/h",
        )
    try:
        coefficients = muskingum_coefficients(
            travel_time_h=travel_time_h,
            weighting_x=weighting_x,
            dt_h=dt_h,
        )
    except ValueError as exc:
        return uncomputable_hydrograph(
            formula_id="Q-MUSKINGUM",
            formula_source=SOURCE_MUSKINGUM,
            assumptions=assumptions,
            inputs=inputs,
            reason=str(exc),
            unit="m³/h",
        )
    if not coefficients.stable:
        return uncomputable_hydrograph(
            formula_id="Q-MUSKINGUM",
            formula_source=SOURCE_MUSKINGUM,
            assumptions=assumptions,
            inputs=inputs,
            reason=f"计算时段不满足{MUSKINGUM_STABILITY_NOTE}；请调整 Δt 或 K、X",
            unit="m³/h",
        )

    outflow = [inflow[0]]
    for index in range(1, len(inflow)):
        outflow.append(
            coefficients.c0 * inflow[index]
            + coefficients.c1 * inflow[index - 1]
            + coefficients.c2 * outflow[-1]
        )
    times = [index * float(dt_h) for index in range(len(inflow))]
    peak, peak_time = _peak_and_time(times, outflow)
    inflow_peak, inflow_peak_time = _peak_and_time(times, inflow)
    attenuation = 0.0 if inflow_peak <= 0.0 else 1.0 - peak / inflow_peak
    summary = {
        "inflow_peak_m3h": inflow_peak,
        "peak_attenuation_ratio": attenuation,
        "peak_lag_h": peak_time - inflow_peak_time,
    }
    return HydrographResult(
        times_h=times,
        values=outflow,
        unit="m³/h",
        formula_id="Q-MUSKINGUM",
        formula_source=SOURCE_MUSKINGUM,
        assumptions=assumptions,
        inputs=inputs,
        summary=summary,
        error_bound="未率定；峰值削减与峰现滞后对 K、X 敏感，K 偏大则削减偏大、滞后偏长",
        peak=peak,
        peak_time_h=peak_time,
    )


def _outflow_m3h(
    level_m: float,
    pump_m3h: float,
    *,
    spill_crest_m: float | None,
    spill_width_m: float,
    spill_coefficient: float,
) -> float:
    flow = pump_m3h
    if spill_crest_m is not None and level_m > spill_crest_m:
        flow += (
            spill_coefficient
            * spill_width_m
            * (level_m - spill_crest_m) ** 1.5
            * SECONDS_PER_HOUR
        )
    return flow


def level_pool_route(
    *,
    inflow_m3h: Sequence[float],
    dt_h: float,
    stage_m: Sequence[float],
    storage_m3: Sequence[float],
    initial_stage_m: float,
    pump_outflow_m3h: float | Sequence[float] = 0.0,
    spill_crest_m: float | None = None,
    spill_width_m: float = 0.0,
    spill_coefficient: float = DEFAULT_WEIR_COEFFICIENT,
) -> HydrographResult:
    """修正 Puls 法调蓄演进：按库容曲线算水位过程线，可叠加抽排与溢流。

    语义约定：`pump_outflow_m3h` 给标量表示全程恒定；给序列时第 k 个值是
    **t_k 时刻（节点）的抽排能力**，修正 Puls 在每个时段用起止两节点的出流均值，
    因此"第 1 时段中途停机"会按 (Q, 0) 的均值计入，这是离散格式的固有近似。
    """

    inflow = [float(value) for value in inflow_m3h]
    stages = [float(value) for value in stage_m]
    storages = [float(value) for value in storage_m3]
    inputs = {
        "dt_h": float(dt_h),
        "initial_stage_m": float(initial_stage_m),
    }
    if stages:
        inputs["stage_min_m"] = stages[0]
        inputs["stage_max_m"] = stages[-1]
    if storages:
        inputs["storage_min_m3"] = storages[0]
        inputs["storage_max_m3"] = storages[-1]
    assumptions = [
        "库容曲线由调用方给定，本层不内置任何几何假设；曲线未率定",
        "抽排能力按给定值恒定（或逐时段序列），不随水位/扬程变化（缺泵特性曲线 H–Q）",
        "溢流按宽顶堰自由出流 Q = C·b·h^(3/2)，流量系数取经验值、未按本工程率定",
        "静水面假定：不计水面比降、侧向入流、下渗与蒸发",
        "修正 Puls 法按 Δt 离散，水位为时段末值",
    ]

    def fail(reason: str) -> HydrographResult:
        return uncomputable_hydrograph(
            formula_id="H-LEVEL-POOL",
            formula_source=SOURCE_LEVEL_POOL,
            assumptions=assumptions,
            inputs=inputs,
            reason=reason,
            unit="m",
        )

    if len(inflow) < 2:
        return fail("入流过程线至少需要两个时段")
    if any(value < 0.0 for value in inflow):
        return fail("入流不能为负；反向流不在本层适用范围")
    if dt_h <= 0:
        return fail("计算时段 Δt 必须大于 0")
    if len(stages) != len(storages) or len(stages) < 2:
        return fail("库容曲线至少两个点，且水位序列与库容序列必须等长")
    if any(later <= earlier for earlier, later in zip(stages, stages[1:])):
        return fail("水位序列必须严格递增")
    if any(later <= earlier for earlier, later in zip(storages, storages[1:])):
        return fail("库容序列必须严格递增")
    if storages[0] < 0:
        return fail("库容不能为负")
    if not stages[0] <= float(initial_stage_m) <= stages[-1]:
        return fail(f"初始水位 {initial_stage_m:g} m 超出库容曲线范围")

    if isinstance(pump_outflow_m3h, Sequence):
        pumps = [float(value) for value in pump_outflow_m3h]
        if len(pumps) != len(inflow):
            return fail("抽排能力序列长度必须与入流过程线一致")
    else:
        pumps = [float(pump_outflow_m3h)] * len(inflow)
    if any(value < 0.0 for value in pumps):
        return fail("抽排能力不能为负")
    inputs["pump_max_m3h"] = max(pumps)

    if spill_crest_m is not None:
        inputs["spill_crest_m"] = float(spill_crest_m)
        inputs["spill_width_m"] = float(spill_width_m)
        if spill_width_m <= 0:
            return fail("给出溢流堰顶时，堰宽必须大于 0")
        if spill_coefficient <= 0:
            return fail("溢流流量系数必须大于 0")

    def storage_of(level: float) -> float:
        return _interp(level, stages, storages)

    def indication(level: float, pump: float) -> float:
        return storage_of(level) / dt_h + _outflow_m3h(
            level,
            pump,
            spill_crest_m=spill_crest_m,
            spill_width_m=float(spill_width_m),
            spill_coefficient=float(spill_coefficient),
        ) / 2.0

    levels = [float(initial_stage_m)]
    for index in range(1, len(inflow)):
        previous_level = levels[-1]
        target = (
            storage_of(previous_level) / dt_h
            - _outflow_m3h(
                previous_level,
                pumps[index - 1],
                spill_crest_m=spill_crest_m,
                spill_width_m=float(spill_width_m),
                spill_coefficient=float(spill_coefficient),
            ) / 2.0
            + (inflow[index - 1] + inflow[index]) / 2.0
        )
        if indication(stages[-1], pumps[index]) < target - _FLOW_TOLERANCE_M3H:
            return fail(
                f"第 {index} 时段水位超出库容曲线上限 {stages[-1]:g} m："
                "需扩展库容曲线或增加出流能力，不截断、不外推"
            )
        if indication(stages[0], pumps[index]) > target + _FLOW_TOLERANCE_M3H:
            return fail(
                f"第 {index} 时段水位低于库容曲线下界 {stages[0]:g} m："
                "需把库容曲线补到更低水位，不截断、不外推"
            )
        low, high = stages[0], stages[-1]
        for _ in range(200):
            middle = (low + high) / 2.0
            if indication(middle, pumps[index]) < target:
                low = middle
            else:
                high = middle
            if high - low <= _BISECTION_TOLERANCE_M:
                break
        levels.append((low + high) / 2.0)

    times = [index * float(dt_h) for index in range(len(inflow))]
    peak, peak_time = _peak_and_time(times, levels)
    summary = {
        "final_stage_m": levels[-1],
        "max_pump_outflow_m3h": max(pumps),
    }
    return HydrographResult(
        times_h=times,
        values=levels,
        unit="m",
        formula_id="H-LEVEL-POOL",
        formula_source=SOURCE_LEVEL_POOL,
        assumptions=assumptions,
        inputs=inputs,
        summary=summary,
        error_bound="未率定；对库容曲线与出流能力近似 1 次幂敏感，曲线偏差 20% 则水位过程同量级偏差",
        peak=peak,
        peak_time_h=peak_time,
    )


def _rising_time(
    times: Sequence[float],
    values: Sequence[float],
    threshold_m: float,
) -> float | None:
    """首次上穿阈值水位的时间；线性插值，全程未达到返回 None。"""

    if values[0] >= threshold_m:
        return times[0]
    for index in range(1, len(values)):
        if values[index] >= threshold_m:
            below, above = values[index - 1], values[index]
            if above == below:
                return times[index]
            ratio = (threshold_m - below) / (above - below)
            return times[index - 1] + ratio * (times[index] - times[index - 1])
    return None


def _falling_time_after(
    times: Sequence[float],
    values: Sequence[float],
    threshold_m: float,
    start_index: int,
) -> float | None:
    """在上穿之后的节点里找首次下穿阈值水位的时间；找不到返回 None。"""

    for index in range(start_index + 1, len(values)):
        if values[index] < threshold_m:
            above, below = values[index - 1], values[index]
            if above == below:
                return times[index]
            ratio = (above - threshold_m) / (above - below)
            return times[index - 1] + ratio * (times[index] - times[index - 1])
    return None


def _rising_index(values: Sequence[float], threshold_m: float) -> int:
    """上穿发生所在区间的右端节点下标（水位首次达到阈值的节点）。"""

    for index, value in enumerate(values):
        if value >= threshold_m:
            return index
    return 0


def time_to_level(*, hydrograph: HydrographResult, target_level_m: float) -> QuantResult:
    """在给定水位过程线上求"第一次到达目标水位"的时刻，输出 h。"""

    inputs = {"target_level_m": float(target_level_m)}
    assumptions = [
        "到达时刻按离散过程线在相邻时段之间线性插值，误差不超过一个计算时段 Δt",
        "只求第一次到达；若过程线反复越过阈值，不在此处展开",
    ]
    if not hydrograph.computable:
        return uncomputable(
            formula_id="T-LEVEL-CROSS",
            formula_source=SOURCE_LEVEL_CROSS,
            assumptions=assumptions,
            inputs=inputs,
            unit="h",
            reason=f"输入过程线不可计算：{hydrograph.uncomputable_reason}",
        )
    inputs["initial_level_m"] = hydrograph.values[0]
    inputs["peak_level_m"] = hydrograph.peak
    if target_level_m <= hydrograph.values[0]:
        return QuantResult(
            value=0.0,
            unit="h",
            formula_id="T-LEVEL-CROSS",
            formula_source=SOURCE_LEVEL_CROSS,
            assumptions=[*assumptions, "目标水位不高于初始水位，判定为已达成"],
            inputs=inputs,
            error_bound="已达成，无需外推",
        )
    crossing = _rising_time(hydrograph.times_h, hydrograph.values, float(target_level_m))
    if crossing is None:
        return uncomputable(
            formula_id="T-LEVEL-CROSS",
            formula_source=SOURCE_LEVEL_CROSS,
            assumptions=assumptions,
            inputs=inputs,
            unit="h",
            reason=f"过程线内未达到目标水位（峰值为 {hydrograph.peak:g} m）",
        )
    return QuantResult(
        value=crossing,
        unit="h",
        formula_id="T-LEVEL-CROSS",
        formula_source=SOURCE_LEVEL_CROSS,
        assumptions=assumptions,
        inputs=inputs,
        error_bound="离散误差不超过一个计算时段 Δt",
    )


def level_exceedance_duration(
    *, hydrograph: HydrographResult, threshold_m: float
) -> QuantResult:
    """水位超过阈值水位的累计时长，输出 h（从首次上穿到随后首次下穿）。"""

    inputs = {"threshold_m": float(threshold_m)}
    assumptions = [
        "超阈时长按离散过程线线性插值求上穿与下穿时刻",
        "只统计第一次超越区间；若反复越阈，不在此处累加多段",
    ]
    if not hydrograph.computable:
        return uncomputable(
            formula_id="T-LEVEL-EXCEED",
            formula_source=SOURCE_LEVEL_CROSS,
            assumptions=assumptions,
            inputs=inputs,
            unit="h",
            reason=f"输入过程线不可计算：{hydrograph.uncomputable_reason}",
        )
    inputs["peak_level_m"] = hydrograph.peak
    rising = _rising_time(hydrograph.times_h, hydrograph.values, float(threshold_m))
    if rising is None:
        return QuantResult(
            value=0.0,
            unit="h",
            formula_id="T-LEVEL-EXCEED",
            formula_source=SOURCE_LEVEL_CROSS,
            assumptions=[*assumptions, "全程未超过阈值，判定为 0"],
            inputs=inputs,
            error_bound="未超阈，无需外推",
        )
    up_index = _rising_index(hydrograph.values, float(threshold_m))
    falling = _falling_time_after(
        hydrograph.times_h, hydrograph.values, float(threshold_m), up_index
    )
    if falling is None:
        duration = hydrograph.times_h[-1] - rising
        note = "过程线末尾仍未回落，超阈时长按到过程线末尾计，实际可能更长"
    else:
        duration = max(0.0, falling - rising)
        note = "已回落，超阈时长取首次上穿到首次下穿"
    return QuantResult(
        value=duration,
        unit="h",
        formula_id="T-LEVEL-EXCEED",
        formula_source=SOURCE_LEVEL_CROSS,
        assumptions=[*assumptions, note],
        inputs=inputs,
        error_bound="离散误差不超过一个计算时段 Δt",
    )
