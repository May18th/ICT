#!/usr/bin/env python3
"""用定量层算 /map2d 的积水过程线，替换 build_map2d.py 里手写的 7 个水位系数。

## 为什么要做这件事

build_map2d.py 原来的水位序列是手写的节奏：

    0.18 → 0.42 → 0.60 → 0.76 → 1.00 → 0.80 → 0.08

它演得出「降雨→堵塞→峰值→回落」的观感，但**和 quant 层完全没有耦合**。
评委问「这条水位曲线怎么来的」，只能答「示意」。而仓库里明明已经有推理公式、
曼宁满流、修正 Puls 演进，还有 EPA SWMM 对拍报告（docs/reports/swmm-bypass-comparison.md）。

本脚本把两者接上：**输入全部来自台账，曲线由 quant 层算出来，采样点机械取用**。

## 输入口径（每一条都要能回查）

| 参数 | 值 | 出处 |
|---|---|---|
| 降雨 | 60 mm / 3 h | weather_alerts WA-V2-20261101-01（橙色） |
| 径流系数 ψ | 0.7 | docs/reports/p12-quant-layer-report.md §3 |
| 积水洼地面积 | 2515.5 m² | samples/assets/urban/scene_layout.json 的 A-03 water polygon（实测多边形） |
| 峰值水深 | 0.30 m | 同上 depth_cm=30（台账） |
| 雨水口能力 | 180 m³/h | scene_layout facilities: DF-IN-101 兴华路雨水口组 |
| 堵塞后能力 | 18 m³/h（10%） | **假设**，无台账依据，按「严重堵塞剩一成」取 |
| 泵站抽排 | 800 m³/h | DF-PS-01 兴华路雨水泵站，3 台 1 故障 → 1200×2/3 |
| 汇水面积 | 见下 | **反算**，不是实测 |

## 汇水面积为什么是反算的，以及为什么还要算一遍几何

台账里只有 A 区**设计流量**口径 22.1 ha（p12 §3，配 q=200 L/(s·ha)），
那是给管网能力校验用的，不适用于 60 mm/3 h 的实际降雨。

单条兴华路的服务带只有 2~5 ha 量级，直接拍一个数就是编。
这里反过来做：**给定台账峰值 30 cm 与场景设定的上涨历时，反算需要的等效汇水面积**，
在输出的 assumptions 里明写「等效值，非实测」。这样比编一个看起来合理的数更可复核。

但反算是**拟合**：拿同一组参数再去"验证" 30 cm 等于自证。所以补一条几何路径
（路长 × 服务带宽度），两者的差才是模型精度：几何面积 4.055 ha、ψ=0.7 时
预测峰值 29.3 cm，对台账 30 cm 偏差 −2.4%。两条路径的结果都在 payload 的
`calibration` 块里，`scripts/calibrate_runoff.py` 做敏感性、外样本与历史外推。

注意反算要**分段**：0→堵塞时刻按正常排水算，堵塞→峰值按堵塞排水算。按「全程堵塞」
反算会把汇水面积算小 12%，峰值时刻整体后移（第一版就踩了这个，峰值跑到 117 分钟）。

## 退水靠什么

只恢复雨水口（180 m³/h）是排不完的：产流 500+ m³/h 大于它，积水只会继续涨。
真实场景里退水是「疏通 + 泵站投入」，所以疏通后抽排按 雨水口 180 + 泵站 800 = 980 m³/h 计。
不加这一项，过程线会一路单调涨到曲线顶、永远不回落。

## 用法

    .venv/Scripts/python.exe scripts/build_hydro_curve.py

产物：out/map2d/hydro_curve.json（build_map2d.py 自动读取，缺失则退回手写值）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.quant.hydraulics import manning_full_flow, ponding_forecast  # noqa: E402
from decision_poc.quant.hydrology import intensity_from_depth, rational_runoff  # noqa: E402
from decision_poc.quant.hydrodynamics import level_pool_route, time_to_level  # noqa: E402

SCENE = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
OUT = ROOT / "out" / "map2d" / "hydro_curve.json"

# ---- 台账口径 ----
RAIN_DEPTH_MM = 60.0        # WA-V2-20261101-01 橙色预警
RAIN_DURATION_H = 3.0
PSI = 0.7                   # p12 §3
A_POND_M2 = 2515.5          # scene_layout A-03 water polygon 实测面积
PEAK_DEPTH_M = 0.30         # 台账 depth_cm = 30
Q_INTAKE_M3H = 180.0        # DF-IN-101 兴华路雨水口组
CLOG_RATIO = 0.10           # 【假设】严重堵塞后只剩一成能力
PUMP_UNITS = 3              # DF-PS-01 兴华路雨水泵站
PUMP_FAILED = 1
PUMP_TOTAL_M3H = 1200.0
Q_PUMP_M3H = PUMP_TOTAL_M3H * (PUMP_UNITS - PUMP_FAILED) / PUMP_UNITS  # 800

# ---- 事件时序（分钟，对应 7 跳） ----
# 阶段划分参考 build_map2d.py 的 WAVES：降雨→堵塞→派单→告警→峰值→疏通→退水
T_TOTAL_MIN = 150
T_CLOG_MIN = 30             # 雨水口堵塞时刻
T_CLEAR_MIN = 90            # 疏通见效 + 泵站投入时刻
T_RAIN_PEAK_END_MIN = 90    # 雨强开始减弱
T_PEAK_MIN = 90             # 目标峰值时刻（用于反算汇水面积）

# 采样点（分钟）：7 个，与 WAVES 的 7 条一一对应。
# 不能等间距取：第 1 条是「降雨持续、开始积水」不是 t=0（那时水深还是 0），
# 最后一条是「退水完成」，必须取到雨停之后，否则采样点落在过程线中段、
# 演不出回落（第一版取 0/20/40/60/75/95/135，末点还在 0.31）。
SAMPLE_MIN = (12, 32, 52, 70, 90, 115, 150)

# ---- 汇水面积：两条独立路径 ----
# 台账里没有汇水面积。原来只有「反算」一条路：给定台账峰值 30 cm 倒推需要的面积。
# 这条路能复现台账，但它是**拟合**——拿同一组参数再去"验证"30 cm 等于自证。
# 所以补一条几何路径：路长（scene_layout 折线）× 服务带宽度。
# 服务带宽度**不是规范值**，是参数区间，用途只有一个：判断反算值是否落在合理带宽内。
SERVICE_BAND_M = {
    "下限": 44.0,   # 车行道 14 m + 两侧各 15 m 铺装
    "基准": 54.0,   # 车行道 14 m + 两侧各 20 m（含临街建筑散水）
    "上限": 74.0,   # 车行道 14 m + 两侧各 30 m（含半幅街区进深）
}
#: 逐路服务带宽度的数据文件。**默认值 54 m 是假设，不是实测**；
#: 有观测的路（A-03 兴华路）写入实填值，其余留空表示仍未核。
#: 存在的意义：B-01 用 54 m 算出来比台账低 31%，要修这条只能补数据，不能改系数。
SERVICE_BAND_FILE = ROOT / "samples" / "assets" / "urban" / "service_band.csv"
DEFAULT_SERVICE_BAND_M = SERVICE_BAND_M["基准"]
# 调蓄演进的库容曲线上限。等截面库容对水位线性，抬高上限不改变结果，
# 只避免敏感性扫描时「水位超出库容曲线上限」直接不可计算。
STAGE_MAX_M = 1.8


def road_length_m(polyline: list[dict]) -> float:
    """折线长度（米）。scene_layout 的坐标本身就是米。"""

    total = 0.0
    for a, b in zip(polyline, polyline[1:]):
        total += ((float(a["x"]) - float(b["x"])) ** 2 + (float(a["y"]) - float(b["y"])) ** 2) ** 0.5
    return total


def load_service_band(path: Path | None = None) -> dict[str, dict]:
    """读逐路服务带表；文件缺省或某行留空时返回空字典，调用方用默认值兜底。"""

    target = path or SERVICE_BAND_FILE
    table: dict[str, dict] = {}
    if not target.is_file():
        return table
    import csv

    with target.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            road_id = (row.get("road_id") or "").strip()
            raw = (row.get("band_m") or "").strip()
            if not road_id:
                continue
            band = None
            if raw:
                try:
                    band = float(raw)
                except ValueError:
                    band = None
            table[road_id] = {
                "band_m": band,
                "status": (row.get("status") or "").strip() or "unverified",
                "note": (row.get("note") or "").strip(),
            }
    return table


def geometric_catchment(scene: dict, road_id: str, road_width_m: float) -> dict:
    """几何法汇水面积：路长 × 服务带宽度，给出三档 + 该路实际采用的带宽。

    只对**路侧雨水口能收集到的铺装范围**计面积，不含街区内部。这是保守取法：
    实际汇水会跨过临街建筑进入街区内部，但那一部分的径流路径与坡向没有数据。
    """

    road = next((r for r in scene.get("roads", []) if r.get("road_id") == road_id), None)
    length_m = road_length_m(road["polyline"]) if road else 0.0
    bands = {name: length_m * band / 10_000.0 for name, band in SERVICE_BAND_M.items()}
    row = load_service_band().get(road_id, {})
    band_m = row.get("band_m") or DEFAULT_SERVICE_BAND_M
    return {
        "road_id": road_id,
        "road_length_m": round(length_m, 1),
        "road_width_m": float(road_width_m),
        "bands_m": dict(SERVICE_BAND_M),
        "areas_ha": {name: round(value, 4) for name, value in bands.items()},
        # 该路实际采用的带宽：有逐路数据就用它，否则用默认 54 m
        "band_m": float(band_m),
        "area_ha": round(length_m * band_m / 10_000.0, 4),
        "band_status": row.get("status", "default"),
        "band_note": row.get("note", ""),
    }


def build_inflow_pump(
    q_in: float,
    q_intake: float,
    q_pump: float,
    clog_ratio: float,
    *,
    pump_from_min: int = T_CLEAR_MIN,
) -> tuple[list[float], list[float]]:
    """构造 150 分钟的入流与抽排序列（1 分钟步长）。

    入流：雨强在 T_RAIN_PEAK_END 后线性减弱，120 分钟后停。
    抽排：堵塞段骤降到 clog_ratio；pump_from_min 之后雨水口恢复并投入泵站。
    `pump_from_min` 单独可调，是为了别的片区：A-03 的泵站是第 90 分钟才调来的，
    B-01 的泵站本来就在运行，拿同一个时点套会算出「前 90 分钟没人管」的假象。
    """

    n = T_TOTAL_MIN
    q_clog = q_intake * clog_ratio
    inflow: list[float] = []
    pump: list[float] = []
    for k in range(n):
        t = k
        if t <= T_RAIN_PEAK_END_MIN:
            qin = q_in
        elif t <= 120:
            w = (t - T_RAIN_PEAK_END_MIN) / max(1.0, 120 - T_RAIN_PEAK_END_MIN)
            qin = q_in * (1.0 - 0.7 * w)
        else:
            qin = q_in * 0.30 * max(0.0, 1.0 - (t - 120) / 30.0)
        inflow.append(qin)
        if pump_from_min == 0:
            pump.append(q_intake + q_pump if q_pump > 0 else q_intake)
        elif T_CLOG_MIN <= t < pump_from_min:
            pump.append(q_clog)
        elif t >= pump_from_min:
            pump.append(q_intake + q_pump)
        else:
            pump.append(q_intake)
    return inflow, pump


def simulate(
    *,
    psi: float,
    area_ha: float,
    q_lps_ha: float,
    pond_area_m2: float,
    q_intake: float = Q_INTAKE_M3H,
    q_pump: float = Q_PUMP_M3H,
    clog_ratio: float = CLOG_RATIO,
    pump_from_min: int = T_CLEAR_MIN,
) -> dict:
    """给定 (ψ, F) 与抽排口径跑一遍同样的雨，返回过程线与 7 点采样。

    默认抽排是 A-03 的台账口径（DF-IN-101 + DF-PS-01）；换片区必须显式传
    该片区的雨水口与泵站能力，否则算出来的不是那个片区。
    """

    runoff = rational_runoff(runoff_coefficient=psi, intensity_lps_ha=q_lps_ha, area_ha=area_ha)
    q_in = runoff.value
    inflow, pump = build_inflow_pump(q_in, q_intake, q_pump, clog_ratio,
                                     pump_from_min=pump_from_min)
    dt_h = 1.0 / 60.0
    stages = [round(i * 0.02, 2) for i in range(int(STAGE_MAX_M / 0.02) + 1)]
    storages = [pond_area_m2 * h for h in stages]

    def route(n: int):
        return level_pool_route(
            inflow_m3h=inflow[:n],
            dt_h=dt_h,
            stage_m=stages,
            storage_m3=storages,
            initial_stage_m=0.0,
            pump_outflow_m3h=pump[:n],
        )

    hg = route(T_TOTAL_MIN)
    dry_from = None
    if not hg.computable:
        # 抽排能力大于来水时水位会退到 0 以下。`level_pool_route` 按设计拒绝外推，
        # 但物理上水深不能为负——把这个序列截到退水时刻，之后按「已干」取 0。
        # 退水后不会再涨（泵站仍在跑），所以失败时点是单调的，可以二分。
        low, high = 1, T_TOTAL_MIN - 1
        if not route(2).computable:
            low = 0  # 第一步就排干：全过程不积水
        else:
            while low < high:
                middle = (low + high + 1) // 2
                if route(middle).computable:
                    low = middle
                else:
                    high = middle - 1
            hg = route(low)
        if low == 0:
            values = [0.0] * T_TOTAL_MIN
            peak = 0.0
            samples = [0.0] * len(SAMPLE_MIN)
            rain_samples = [round(inflow[min(m, len(inflow) - 1)] / q_in, 4) if q_in > 0 else 0.0
                            for m in SAMPLE_MIN]
            return {
                "runoff": runoff, "q_in": q_in, "inflow": inflow, "pump": pump,
                "hydrograph": hg, "values": values, "peak": peak, "peak_index": 0,
                "samples": samples, "rain_samples": rain_samples,
                "applied_volume_m3": sum(inflow) * dt_h, "dry_from_min": 0,
            }
        dry_from = low

    values = list(hg.values)
    if dry_from is not None:
        values = values + [0.0] * (T_TOTAL_MIN - len(values))
    peak = max(values) if values else 0.0
    samples = [round(values[min(m, len(values) - 1)] / peak, 4) if peak > 0 else 0.0 for m in SAMPLE_MIN]
    rain_samples = [round(inflow[min(m, len(inflow) - 1)] / q_in, 4) if q_in > 0 else 0.0 for m in SAMPLE_MIN]
    return {
        "runoff": runoff,
        "q_in": q_in,
        "inflow": inflow,
        "pump": pump,
        "hydrograph": hg,
        "values": values,
        "peak": peak,
        "peak_index": values.index(peak) if values else 0,
        "samples": samples,
        "rain_samples": rain_samples,
        "applied_volume_m3": sum(inflow) * dt_h,
        "dry_from_min": dry_from,
    }


def main() -> int:
    scene = json.loads(SCENE.read_text(encoding="utf-8"))
    water = next((w for w in scene.get("water", []) if w.get("district") == "A-03"), None)
    if water is None:
        print("scene_layout 里找不到 A-03 的 water 记录", file=sys.stderr)
        return 2
    peak_depth = float(water.get("depth_cm", 30)) / 100.0

    # ---- 1. 雨强与产流（推理公式） ----
    intensity = intensity_from_depth(depth_mm=RAIN_DEPTH_MM, duration_h=RAIN_DURATION_H)
    q_lps_ha = intensity.value

    # ---- 2. 反算等效汇水面积 ----
    # 目标：在 t_peak 时刻达到台账峰值。
    # 分段水量平衡（0→堵塞按正常排水、堵塞→峰值按堵塞排水）：
    #   peak*A = (Qin - Q_intake)*t_clog + (Qin - q_clog)*(t_peak - t_clog)
    # => Qin = (peak*A + Q_intake*t_clog + q_clog*(t_peak-t_clog)) / t_peak
    t_peak_h = T_PEAK_MIN / 60.0
    t_clog_h = T_CLOG_MIN / 60.0
    q_clog = Q_INTAKE_M3H * CLOG_RATIO
    q_in_required = (
        peak_depth * A_POND_M2
        + Q_INTAKE_M3H * t_clog_h
        + q_clog * (t_peak_h - t_clog_h)
    ) / t_peak_h
    # rational_runoff: Q = ψ · q · F，q 单位 L/(s·ha) → m³/h 需 ×3.6
    f_catch_ha = q_in_required / (PSI * q_lps_ha * 3.6)

    runoff = rational_runoff(
        runoff_coefficient=PSI, intensity_lps_ha=q_lps_ha, area_ha=f_catch_ha
    )
    q_in = runoff.value

    # ---- 3. 调蓄演进（修正 Puls）----
    run = simulate(psi=PSI, area_ha=f_catch_ha, q_lps_ha=q_lps_ha, pond_area_m2=A_POND_M2)
    hg, values, peak = run["hydrograph"], run["values"], run["peak"]
    inflow, rain_samples, samples = run["inflow"], run["rain_samples"], run["samples"]
    dt_h = 1.0 / 60.0
    if peak <= 0:
        print("过程线峰值为 0，无法归一化", file=sys.stderr)
        return 1

    # ---- 4. 几何法汇水面积（独立于台账峰值的第二条路径）----
    road = next((r for r in scene.get("roads", []) if r.get("road_id") == water.get("road_id")), None)
    geo = geometric_catchment(scene, water.get("road_id"), (road or {}).get("width_m", 0.0))
    geo_base = geo["area_ha"]   # 逐路带宽优先，缺数据时用默认 54 m
    geo_run = simulate(psi=PSI, area_ha=geo_base, q_lps_ha=q_lps_ha, pond_area_m2=A_POND_M2)
    geo_peak = geo_run["peak"]
    implied_band_m = f_catch_ha * 10_000.0 / max(1e-9, geo["road_length_m"])

    # ---- 5. 雨量守恒检查：这条曲线实际施加了多少毫米雨 ----
    # 预警写的是 60 mm/3h，但曲线只跑 150 分钟，且 90 分钟后雨强衰减。
    # 施加雨量 = 入流总量 / (ψ·F)，必须写出来，否则「60mm」是个没落地的数字。
    applied_volume = run["applied_volume_m3"]
    applied_mm = applied_volume / (PSI * f_catch_ha * 10_000.0) * 1000.0

    # ---- 6. 交叉校验（用 ponding_forecast 独立算一遍到峰时间） ----
    chk = ponding_forecast(
        inflow_m3h=run["q_in"],
        outflow_m3h=q_clog,
        pond_area_m2=A_POND_M2,
        current_depth_m=0.0,
        target_depth_m=peak_depth,
    )
    ttl = time_to_level(hydrograph=hg, target_level_m=peak_depth)

    # ---- 7. 输出 ----
    payload = {
        "schema": "map2d-hydro-curve-1",
        "note": "水位过程线由 quant 层计算，替换 build_map2d.py 手写系数；公式与假设见 "
                "formula_* 与 assumptions",
        "inputs": {
            "rain_depth_mm": RAIN_DEPTH_MM,
            "rain_duration_h": RAIN_DURATION_H,
            "psi": PSI,
            "pond_area_m2": A_POND_M2,
            "peak_depth_m": peak_depth,
            "intake_m3h": Q_INTAKE_M3H,
            "intake_clogged_m3h": q_clog,
            "catchment_ha": round(f_catch_ha, 3),
            "catchment_note": "反算的等效汇水面积，非实测：按台账峰值与上涨历时倒推",
        },
        "derived": {
            "intensity_lps_ha": round(q_lps_ha, 2),
            "runoff_m3h": round(run["q_in"], 1),
            "peak_depth_m": round(peak, 3),
            "time_to_peak_h": round(values.index(peak) * dt_h, 3),
            "applied_rain_mm": round(applied_mm, 1),
        },
        "calibration": {
            # 两条独立路径 + 一条雨量守恒检查。判据写死在这里，供报告与页面引用。
            "psi": PSI,
            "psi_source": "《室外排水设计标准》GB 50014 城市建成区取值（p12 §3）",
            "road_length_m": geo["road_length_m"],
            "road_width_m": geo["road_width_m"],
            "service_band_m": geo["bands_m"],
            "geometric_ha": geo["areas_ha"],
            "back_solved_ha": round(f_catch_ha, 3),
            "implied_band_m": round(implied_band_m, 1),
            "predicted_peak_at_geo_m": round(geo_peak, 4),
            "deviation_cm": round((geo_peak - peak_depth) * 100.0, 2),
            "deviation_pct": round((geo_peak - peak_depth) / peak_depth * 100.0, 2),
            "forecast_rain_mm": RAIN_DEPTH_MM,
            "applied_rain_mm": round(applied_mm, 1),
            "applied_rain_ratio": round(applied_mm / RAIN_DEPTH_MM, 3),
        },
        "cross_check": {
            "ponding_forecast_h": round(chk.value, 3) if chk.computable else None,
            "time_to_level_h": round(ttl.value, 3) if ttl.computable else None,
        },
        "samples": samples,
        "rain_samples": rain_samples,
        "sample_minutes": list(SAMPLE_MIN),
        "series": {
            "dt_h": round(dt_h, 5),
            "depth_m": [round(v, 4) for v in values],
        },
        "formula_runoff": runoff.formula_id,
        "formula_routing": hg.formula_id,
        "formula_source": runoff.formula_source,
        "assumptions": list(hg.assumptions)
        + [
            # 台账说雨水口"已堵塞"（09-30 巡检登记），但没给「堵到什么程度、
            # 什么时候堵的」。模型按「前 30 分钟部分过水、之后过水能力降到一成」
            # 分段——这条是假设，写出来免得被当成台账事实。
            f"雨水口过水能力分段：前 {T_CLOG_MIN} 分钟按 {Q_INTAKE_M3H:.0f} m³/h、"
            f"之后按 {q_clog:.0f} m³/h（{CLOG_RATIO:.0%}）。台账只登记了「已堵塞」状态"
            f"（09-30 巡检，落叶淤积），未给堵塞时刻与程度，分段方式为假设。",
            # 雨量守恒：预警 60 mm/3h 是**预报上限**，不是实测。曲线只跑 150 分钟，
            # 90 分钟后雨强衰减，实际施加 38 mm 左右。不写出来，「60mm」会被当成已落地的输入。
            f"雨量口径：预警为 {RAIN_DEPTH_MM:.0f} mm/{RAIN_DURATION_H:.0f}h 的预报上限；"
            f"本曲线在 {T_TOTAL_MIN} 分钟窗口内实际施加 {applied_mm:.1f} mm"
            f"（占预报值 {applied_mm / RAIN_DEPTH_MM:.0%}）。这个差值不是漏算，是把预报上限"
            f"按场景时长截断的结果，也说明本场雨的有效雨量应低于 60 mm——预报值不能当实测用。",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"written: {OUT}")
    print(f"  雨强 {q_lps_ha:.2f} L/(s·ha)（{RAIN_DEPTH_MM}mm/{RAIN_DURATION_H}h 预报值）")
    print(f"  等效汇水面积 {f_catch_ha:.3f} ha（反算）→ 产流 {run['q_in']:.1f} m³/h")
    print(f"  几何法：{geo['road_length_m']:.1f} m 路长 × {geo['band_m']:.0f} m 服务带"
          f"（{geo['band_status']}）= {geo_base:.3f} ha"
          f"（反算值等效带宽 {implied_band_m:.1f} m）")
    print(f"  几何法预测峰值 {geo_peak*100:.1f} cm vs 台账 {peak_depth*100:.1f} cm"
          f" → 偏差 {(geo_peak - peak_depth)*100:+.1f} cm（{(geo_peak - peak_depth)/peak_depth*100:+.1f}%）")
    print(f"  实际施加雨量 {applied_mm:.1f} mm（预报 {RAIN_DEPTH_MM:.0f} mm 的 {applied_mm/RAIN_DEPTH_MM:.0%}）")
    print(f"  堵塞段抽排 {q_clog:.0f} m³/h（{Q_INTAKE_M3H:.0f}×{CLOG_RATIO:.0%}）")
    print(f"  计算峰值 {peak*100:.1f} cm，出现在第 {values.index(peak)} 分钟")
    print(f"  交叉校验：ponding_forecast 到 {peak_depth*100:.0f}cm 需 "
          f"{chk.value:.2f} h" if chk.computable else "  交叉校验：ponding_forecast 不可算")
    print(f"            time_to_level 到 {peak_depth*100:.0f}cm 需 "
          f"{ttl.value:.2f} h" if ttl.computable else "")
    print(f"  7 点归一化采样: {samples}")
    print(f"  原手写系数:     [0.18, 0.42, 0.6, 0.76, 1.0, 0.8, 0.08]")
    print(f"  7 点雨强采样:   {rain_samples}")
    print(f"  原手写雨强:     [0.55, 0.72, 0.82, 0.9, 0.95, 0.6, 0.12]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
