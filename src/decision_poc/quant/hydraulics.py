"""水力学：管流能力、积水上涨、以及下穿通道倒灌流量。

四件事，四套公式，来源都在 `formula_source` 里写死，方便人工回查：

1. 满流输水能力 —— 曼宁公式（圆管满流，水力半径 R = D/4）；
2. 积水上涨 —— 水量平衡 `dV/dt = Q_in − Q_out`，`d = V/A`；
3. 倒灌（堰流）—— `Q = C·b·h^(3/2)`，C 取 1.7（宽顶堰，SI）；
4. 倒灌（孔口）—— `Q = Cd·A·√(2gh)`，Cd 取 0.60。

**不适用的范围**写在这里，避免被当成通用模型引用：

- 不是非恒定流（圣维南）模型，没有壅水与回水计算；
- 不计局部损失、跌水、倒虹与检查井壅水；
- 堰流/孔口系数是经验值，未按本工程率定，只用于量级判断与敏感度分析。
"""

from __future__ import annotations

import math

from decision_poc.quant.contracts import QuantResult, uncomputable

SOURCE_MANNING = "《室外排水设计标准》GB 50014 曼宁公式 Q = (1/n)·A·R^(2/3)·i^(1/2)"
SOURCE_PONDING = "水量平衡 dV/dt = Q_in − Q_out 与 d = V/A"
SOURCE_WEIR = "宽顶堰流量公式 Q = C·b·h^(3/2)，C = 1.7（SI，未按本工程率定）"
SOURCE_ORIFICE = "孔口出流公式 Q = Cd·A·√(2gh)，Cd = 0.60（未按本工程率定）"

DEFAULT_ROUGHNESS = 0.013
DEFAULT_WEIR_COEFFICIENT = 1.7
DEFAULT_ORIFICE_COEFFICIENT = 0.60
GRAVITY = 9.81


def _circular_full_area(diameter_m: float) -> float:
    return math.pi * diameter_m**2 / 4.0


def _circular_full_hydraulic_radius(diameter_m: float) -> float:
    # 圆管满流：R = A / P = (πD²/4) / (πD) = D/4
    return diameter_m / 4.0


def manning_full_flow(
    *,
    diameter_m: float,
    slope: float,
    roughness_n: float = DEFAULT_ROUGHNESS,
) -> QuantResult:
    """圆管满流输水能力，输出 m³/h。"""

    inputs = {
        "diameter_m": float(diameter_m),
        "slope": float(slope),
        "roughness_n": float(roughness_n),
    }
    assumptions = [
        "圆管满流，水力半径按 R = D/4 计算",
        "管道坡度沿程不变，不计局部损失与跌水",
        "粗糙系数取定值，忽略管龄与淤积差异",
    ]
    if diameter_m <= 0:
        return uncomputable(
            formula_id="Q-MANNING",
            formula_source=SOURCE_MANNING,
            assumptions=assumptions,
            inputs=inputs,
            reason="管径必须大于 0",
        )
    if slope <= 0:
        return uncomputable(
            formula_id="Q-MANNING",
            formula_source=SOURCE_MANNING,
            assumptions=assumptions,
            inputs=inputs,
            reason="坡度必须大于 0；平坡与倒坡需按非恒定流处理，本层不算",
        )
    if roughness_n <= 0:
        return uncomputable(
            formula_id="Q-MANNING",
            formula_source=SOURCE_MANNING,
            assumptions=assumptions,
            inputs=inputs,
            reason="粗糙系数必须大于 0",
        )
    area = _circular_full_area(float(diameter_m))
    radius = _circular_full_hydraulic_radius(float(diameter_m))
    flow_m3s = (1.0 / roughness_n) * area * radius ** (2.0 / 3.0) * slope**0.5
    return QuantResult(
        value=flow_m3s * 3600.0,
        unit="m³/h",
        formula_id="Q-MANNING",
        formula_source=SOURCE_MANNING,
        assumptions=assumptions,
        inputs={**inputs, "flow_m3s": flow_m3s},
        error_bound="未率定；对 n 与 i 分别近似 −1 次与 0.5 次幂敏感",
    )


def manning_velocity(
    *,
    diameter_m: float,
    slope: float,
    roughness_n: float = DEFAULT_ROUGHNESS,
) -> QuantResult:
    """圆管满流流速，输出 m/s（自清流速 >0.6、冲蚀上限 <3 的校核依据）。"""

    flow = manning_full_flow(diameter_m=diameter_m, slope=slope, roughness_n=roughness_n)
    if not flow.computable:
        return flow
    area = _circular_full_area(float(diameter_m))
    return flow.model_copy(
        update={
            "value": flow.inputs["flow_m3s"] / area,
            "unit": "m/s",
            "formula_id": "V-MANNING",
            "error_bound": "未率定；随流量同比例变化",
        }
    )


def ponding_forecast(
    *,
    inflow_m3h: float,
    outflow_m3h: float,
    pond_area_m2: float,
    current_depth_m: float,
    target_depth_m: float,
) -> QuantResult:
    """按水量平衡预测"再过多久到目标水深"，输出 h。

    这是决策价值最高的一项：封控提前量不是拍出来的，是 `(目标−当前)/(净上涨速率)`。
    """

    inputs = {
        "inflow_m3h": float(inflow_m3h),
        "outflow_m3h": float(outflow_m3h),
        "pond_area_m2": float(pond_area_m2),
        "current_depth_m": float(current_depth_m),
        "target_depth_m": float(target_depth_m),
    }
    assumptions = [
        "准静态：场次内来水与抽排能力按常量处理，不随水深变化",
        "积水面积在上涨过程中不变，忽略漫流边界扩展",
        "不计管网调蓄、下渗与蒸发",
    ]
    if pond_area_m2 <= 0:
        return uncomputable(
            formula_id="Q-PONDING-TIME",
            formula_source=SOURCE_PONDING,
            assumptions=assumptions,
            inputs=inputs,
            unit="h",
            reason="积水面积必须大于 0",
        )
    net_m3h = float(inflow_m3h) - float(outflow_m3h)
    if target_depth_m <= current_depth_m:
        return QuantResult(
            value=0.0,
            unit="h",
            formula_id="Q-PONDING-TIME",
            formula_source=SOURCE_PONDING,
            assumptions=[*assumptions, "目标阈值不高于当前水深，判定为已达成"],
            inputs=inputs,
            error_bound="已达成，无需外推",
        )
    if net_m3h <= 0:
        return uncomputable(
            formula_id="Q-PONDING-TIME",
            formula_source=SOURCE_PONDING,
            assumptions=assumptions,
            inputs={**inputs, "net_m3h": net_m3h},
            unit="h",
            reason="抽排能力不低于来水，水深不再上涨；给不出到达时刻而不是给一个假时刻",
        )
    rise_rate_mh = net_m3h / float(pond_area_m2)
    return QuantResult(
        value=(float(target_depth_m) - float(current_depth_m)) / rise_rate_mh,
        unit="h",
        formula_id="Q-PONDING-TIME",
        formula_source=SOURCE_PONDING,
        assumptions=assumptions,
        inputs={**inputs, "net_m3h": net_m3h, "rise_rate_m_per_h": rise_rate_mh},
        error_bound="未率定；对净流量与积水面积近似线性敏感，面积估计偏差 20% 则时间偏差约 20%",
    )


def weir_inflow(
    *,
    width_m: float,
    head_m: float,
    coefficient: float = DEFAULT_WEIR_COEFFICIENT,
) -> QuantResult:
    """堰流倒灌：通道入口被外部积水漫过时的进水流量，输出 m³/h。"""

    inputs = {"width_m": float(width_m), "head_m": float(head_m), "coefficient": float(coefficient)}
    assumptions = [
        "按宽顶堰自由出流处理，堰上水头等于外部积水高出入口的深度",
        "入口宽度沿程不变，忽略侧向收缩",
        "流量系数取经验值，未按本工程率定",
    ]
    if width_m <= 0:
        return uncomputable(
            formula_id="Q-WEIR",
            formula_source=SOURCE_WEIR,
            assumptions=assumptions,
            inputs=inputs,
            reason="堰宽必须大于 0",
        )
    if head_m < 0:
        return uncomputable(
            formula_id="Q-WEIR",
            formula_source=SOURCE_WEIR,
            assumptions=assumptions,
            inputs=inputs,
            reason="堰上水头不能为负",
        )
    flow_m3s = coefficient * float(width_m) * float(head_m) ** 1.5
    return QuantResult(
        value=flow_m3s * 3600.0,
        unit="m³/h",
        formula_id="Q-WEIR",
        formula_source=SOURCE_WEIR,
        assumptions=assumptions,
        inputs={**inputs, "flow_m3s": flow_m3s},
        error_bound="量级判断；C 在 1.6–1.9 区间变化时结果同比例变化",
    )


def orifice_inflow(
    *,
    area_m2: float,
    head_m: float,
    coefficient: float = DEFAULT_ORIFICE_COEFFICIENT,
) -> QuantResult:
    """孔口出流倒灌：通道洞口部分淹没时的进水流量，输出 m³/h。"""

    inputs = {"area_m2": float(area_m2), "head_m": float(head_m), "coefficient": float(coefficient)}
    assumptions = [
        "按薄壁孔口出流处理，孔口面积取净过水面积",
        "水头取洞口内外水位差，不考虑行近流速",
        "流量系数取经验值，未按本工程率定",
    ]
    if area_m2 <= 0:
        return uncomputable(
            formula_id="Q-ORIFICE",
            formula_source=SOURCE_ORIFICE,
            assumptions=assumptions,
            inputs=inputs,
            reason="孔口面积必须大于 0",
        )
    if head_m < 0:
        return uncomputable(
            formula_id="Q-ORIFICE",
            formula_source=SOURCE_ORIFICE,
            assumptions=assumptions,
            inputs=inputs,
            reason="作用水头不能为负",
        )
    flow_m3s = coefficient * float(area_m2) * math.sqrt(2.0 * GRAVITY * float(head_m))
    return QuantResult(
        value=flow_m3s * 3600.0,
        unit="m³/h",
        formula_id="Q-ORIFICE",
        formula_source=SOURCE_ORIFICE,
        assumptions=assumptions,
        inputs={**inputs, "flow_m3s": flow_m3s},
        error_bound="量级判断；对水头呈 0.5 次幂敏感",
    )
