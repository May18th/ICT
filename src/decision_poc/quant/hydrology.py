"""水文学：把"下了多少毫米雨"换算成"片区来水多少立方米每小时"。

口径说明（必须先看清再看数）：

- 用**推理公式** `Q = ψ·q·F`（城市雨水设计流量的经典公式），**不是**非恒定流模型；
- 暴雨强度 `q` 取 100/150/200/250 L/(s·ha) 分档做量级与敏感度判断，
  **成都本地暴雨强度公式的系数没有核实，因此不内置**——需要时由调用方传入，
  并在 `assumptions` 里写明来源，不能靠默认值冒充地方规范；
- 径流系数 `ψ` 按用地类型取定值，不随降雨历时变化；
- 不含管网调蓄、下渗与地表二维漫流。
"""

from __future__ import annotations

from decision_poc.quant.contracts import QuantResult, uncomputable

#: 1 mm/h 的面雨强折算成 L/(s·ha)：1 mm/h × 1 ha = 10 m³/h = 10/3.6 L/s。
MM_PER_H_TO_LPS_HA = 1000.0 / 360.0

#: 情景分档用的面雨量（mm/2h）：低/中/高三档，对应"预报可能偏差"的量级范围。
RAINFALL_SCENARIOS_MM_2H = (60.0, 80.0, 100.0)

SOURCE_INTENSITY = "量纲换算：面雨强 1 mm/h = 2.7778 L/(s·ha)"
SOURCE_RATIONAL = "《室外排水设计标准》GB 50014 雨水设计流量（推理公式 Q = ψ·q·F）"

_ASSUMPTION_INTENSITY = [
    "按平均雨强计算，不含雨型（如 Chicago 雨型）的时程分配",
    "面雨量在汇水面积内均匀分布",
]
_ASSUMPTION_RATIONAL = [
    "假定汇水面积内产流均匀，且降雨历时内达到稳定产流",
    "径流系数按用地类型取定值，不随历时变化",
    "不含管网调蓄、下渗与地表二维漫流过程",
]


def intensity_from_depth(*, depth_mm: float, duration_h: float) -> QuantResult:
    """面雨量 → 平均雨强，单位 L/(s·ha)。"""

    inputs = {"depth_mm": float(depth_mm), "duration_h": float(duration_h)}
    if duration_h <= 0:
        return uncomputable(
            formula_id="Q-RAIN-INTENSITY",
            formula_source=SOURCE_INTENSITY,
            assumptions=_ASSUMPTION_INTENSITY,
            inputs=inputs,
            reason="降雨历时必须大于 0",
        )
    if depth_mm < 0:
        return uncomputable(
            formula_id="Q-RAIN-INTENSITY",
            formula_source=SOURCE_INTENSITY,
            assumptions=_ASSUMPTION_INTENSITY,
            inputs=inputs,
            reason="面雨量不能为负",
        )
    intensity_mm_h = float(depth_mm) / float(duration_h)
    return QuantResult(
        value=intensity_mm_h * MM_PER_H_TO_LPS_HA,
        unit="L/(s·ha)",
        formula_id="Q-RAIN-INTENSITY",
        formula_source=SOURCE_INTENSITY,
        assumptions=list(_ASSUMPTION_INTENSITY),
        inputs={**inputs, "intensity_mm_h": intensity_mm_h},
        error_bound="量纲换算无误差；误差全部来自面雨量与历时本身",
    )


def rational_runoff(
    *,
    runoff_coefficient: float,
    intensity_lps_ha: float,
    area_ha: float,
) -> QuantResult:
    """推理公式产流：`Q = ψ·q·F`，输出 m³/h。"""

    inputs = {
        "runoff_coefficient": float(runoff_coefficient),
        "intensity_lps_ha": float(intensity_lps_ha),
        "area_ha": float(area_ha),
    }
    if not 0.0 <= runoff_coefficient <= 1.0:
        return uncomputable(
            formula_id="Q-RUNOFF-RATIONAL",
            formula_source=SOURCE_RATIONAL,
            assumptions=_ASSUMPTION_RATIONAL,
            inputs=inputs,
            reason="径流系数必须在 [0, 1] 内",
        )
    if area_ha < 0 or intensity_lps_ha < 0:
        return uncomputable(
            formula_id="Q-RUNOFF-RATIONAL",
            formula_source=SOURCE_RATIONAL,
            assumptions=_ASSUMPTION_RATIONAL,
            inputs=inputs,
            reason="汇水面积与雨强不能为负",
        )
    flow_m3s = runoff_coefficient * intensity_lps_ha * area_ha / 1000.0
    return QuantResult(
        value=flow_m3s * 3600.0,
        unit="m³/h",
        formula_id="Q-RUNOFF-RATIONAL",
        formula_source=SOURCE_RATIONAL,
        assumptions=list(_ASSUMPTION_RATIONAL),
        inputs={**inputs, "flow_m3s": flow_m3s},
        error_bound="未率定；Q 对 q 与 ψ 近似线性，输入相对误差按同比例传播",
    )


def runoff_from_rainfall(
    *,
    depth_mm: float,
    duration_h: float,
    runoff_coefficient: float,
    area_ha: float,
) -> QuantResult:
    """一步到位：面雨量 + 汇水面积 → 来水 m³/h（内部串接雨强换算与推理公式）。"""

    intensity = intensity_from_depth(depth_mm=depth_mm, duration_h=duration_h)
    if not intensity.computable:
        return intensity
    runoff = rational_runoff(
        runoff_coefficient=runoff_coefficient,
        intensity_lps_ha=intensity.value,
        area_ha=area_ha,
    )
    if not runoff.computable:
        return runoff
    return runoff.model_copy(
        update={
            "inputs": {
                **runoff.inputs,
                "depth_mm": float(depth_mm),
                "duration_h": float(duration_h),
                "intensity_lps_ha": intensity.value,
            },
            "assumptions": [
                *_ASSUMPTION_RATIONAL,
                "雨强由面雨量按时长平均得到，未考虑雨型时程分配",
            ],
        }
    )
