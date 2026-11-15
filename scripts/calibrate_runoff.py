#!/usr/bin/env python3
"""产流参数标定：回答「模型哪来的」，并把不可辨识的部分说清楚。

## 为什么需要这一轮

`build_hydro_curve.py` 的汇水面积是**反算**的：给定台账峰值 30 cm 倒推需要多大面积。
这条路能复现台账，但它是拟合——用同一组参数再去「验证」30 cm 等于自证。
评委问「模型哪来的」，答「反算的」是站不住的。

本脚本把参数拆成三类，逐条给结论：

| 类别 | 判据 | 处理 |
|---|---|---|
| 台账值 | 能在 CSV/JSON 里指到行 | 直接用，写明文件名与行号 |
| 规范值 | 有公开标准条文 | 直接用，写明标准号 |
| 假设值 | 两者都不是 | 显式列出，做敏感性，不伪装成实测 |

## 三条检查（不是三条验证，区别很重要）

1. **几何路径**：汇水面积 = 路长 × 服务带宽度。路长来自 scene_layout 折线（实测几何），
   服务带宽度是参数区间。用几何面积 + 规范 ψ 去**预测**台账峰值，得到的偏差才是模型精度；
2. **外样本**：B-01 滨河路的 18 cm 工单。它没参与标定，用同一套 ψ 与服务带规则算，
   看能不能复现——这是唯一一个真正的样本外检查；
3. **历史外推**：2020-08-11 成都真实记录的最大小时雨强 108 mm/h，看模型会给出什么量级。
   这场雨没有积水深度观测，所以只能做量级检查，**不构成验证**。

## 已知不可辨识

一场雨、一个深度观测，同时要定 ψ、F、堵塞率、有效雨量 —— 四个未知数一个方程，
数学上无解。本脚本固定 ψ（规范）、F（几何）、堵塞率（假设），反推**有效雨量**，
再与预警的 60 mm 上限对比。这条链上任何一环被质疑，结论都要重算，所以全部写出来。

## 用法

    .venv/Scripts/python.exe scripts/calibrate_runoff.py

产物：out/map2d/runoff_calibration.json（报告与页面引用，不参与出图）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_hydro_curve as bhc  # noqa: E402

from decision_poc.quant.hydraulics import ponding_forecast  # noqa: E402
from decision_poc.quant.hydrodynamics import level_pool_route  # noqa: E402
from decision_poc.quant.hydrology import intensity_from_depth  # noqa: E402

SCENE = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
OUT = ROOT / "out" / "map2d" / "runoff_calibration.json"

#: 封路阈值（cm）。来源：属地预案 YJ-FX-2025-002 第 8 节，属合成设定，非市级预案条款。
CLOSE_THRESHOLD_CM = 25.0

#: 2020-08-11 真实记录。出处：samples/scenarios/public_osm/response_events_chengdu_20200811.csv
#: 第 3 行 description 字段「金牛区九里堤最大小时雨强108毫米」，媒体转载（红星新闻/环球网）。
HISTORY = {
    "date": "2020-08-11",
    "peak_hour_mm": 108.0,
    "peak_hour_site": "金牛区九里堤",
    "forecast_3h_mm": 50.0,
    "level_from": "Ⅳ级（蓝色）14:00",
    "level_to": "Ⅲ级（黄色）17:40",
    "source": "samples/scenarios/public_osm/response_events_chengdu_20200811.csv row 3",
    "note": "定级依据是雨量判据（8 区县 12h>50mm / >100mm），不是积水深度；该记录无深度观测",
}


def solve_psi(*, area_ha: float, q_lps_ha: float, target_m: float, pond_area_m2: float) -> float:
    """固定汇水面积，反解能命中目标峰值的 ψ。用来判断规范值 0.7 偏离多远。"""

    low, high = 0.05, 1.0
    for _ in range(60):
        middle = (low + high) / 2.0
        peak = bhc.simulate(psi=middle, area_ha=area_ha, q_lps_ha=q_lps_ha,
                            pond_area_m2=pond_area_m2)["peak"]
        if peak < target_m:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def sim_full_rain(*, q_in: float, q_lps_ha: float) -> list[float]:
    """按预警的 60 mm/3h 全程施加（雨窗 180 分钟），不衰减。

    用途只有一个：验证「把 60 mm 都下完，150 分钟时还演不演得出路面见底」。
    与 `build_hydro_curve` 的主曲线不是同一口径，不能拿它的输出替换主曲线。
    """

    n = 180
    inflow = [q_in] * n
    q_clog = bhc.Q_INTAKE_M3H * bhc.CLOG_RATIO
    pump = []
    for t in range(n):
        if bhc.T_CLOG_MIN <= t < bhc.T_CLEAR_MIN:
            pump.append(q_clog)
        elif t >= bhc.T_CLEAR_MIN:
            pump.append(bhc.Q_INTAKE_M3H + bhc.Q_PUMP_M3H)
        else:
            pump.append(bhc.Q_INTAKE_M3H)
    stages = [round(i * 0.02, 2) for i in range(int(bhc.STAGE_MAX_M / 0.02) + 1)]
    hg = level_pool_route(
        inflow_m3h=inflow,
        dt_h=1.0 / 60.0,
        stage_m=stages,
        storage_m3=[bhc.A_POND_M2 * h for h in stages],
        initial_stage_m=0.0,
        pump_outflow_m3h=pump,
    )
    return list(hg.values) if hg.computable else []


def main() -> int:
    scene = json.loads(SCENE.read_text(encoding="utf-8"))
    water_a = next(w for w in scene["water"] if w["district"] == "A-03")
    water_b = next(w for w in scene["water"] if w["district"] == "B-01")
    peak_a = float(water_a["depth_cm"]) / 100.0
    peak_b = float(water_b["depth_cm"]) / 100.0
    pond_a = bhc.A_POND_M2
    pond_b = 2523.1  # B-01 积水多边形实测面积（shoelace）

    q_lps_ha = intensity_from_depth(
        depth_mm=bhc.RAIN_DEPTH_MM, duration_h=bhc.RAIN_DURATION_H
    ).value

    span_row = next(r for r in scene["roads"] if r["road_id"] == water_a["road_id"])
    geo_a = bhc.geometric_catchment(scene, water_a["road_id"], span_row["width_m"])
    geo_b = bhc.geometric_catchment(scene, water_b["road_id"], 14.0)

    print("=" * 78)
    print("A-03 兴华路：两条独立的汇水面积路径")
    print("=" * 78)
    back = bhc.simulate(psi=bhc.PSI, area_ha=4.108, q_lps_ha=q_lps_ha, pond_area_m2=pond_a)
    print(f"  反算法：4.108 ha（按台账 {peak_a*100:.0f} cm 倒推）→ 峰值 {back['peak']*100:.1f} cm"
          f"，等效服务带 {4.108*10000/geo_a['road_length_m']:.1f} m")
    print(f"  几何法：路长 {geo_a['road_length_m']:.1f} m × 服务带")
    for name, band in geo_a["bands_m"].items():
        area = geo_a["areas_ha"][name]
        run = bhc.simulate(psi=bhc.PSI, area_ha=area, q_lps_ha=q_lps_ha, pond_area_m2=pond_a)
        dev = (run["peak"] - peak_a) * 100
        print(f"           {name} {band:>4.0f} m → {area:.3f} ha → 峰值 {run['peak']*100:5.1f} cm"
              f"（对台账 {dev:+.1f} cm / {dev/peak_a/100*100:+.1f}%）")

    base_area = geo_a["area_ha"]
    base_run = bhc.simulate(psi=bhc.PSI, area_ha=base_area, q_lps_ha=q_lps_ha, pond_area_m2=pond_a)
    psi_solved = solve_psi(area_ha=base_area, q_lps_ha=q_lps_ha, target_m=peak_a, pond_area_m2=pond_a)
    print(f"  固定几何面积 {base_area:.3f} ha，反解 ψ = {psi_solved:.3f}"
          f"（规范值 {bhc.PSI}，差 {(psi_solved-bhc.PSI)/bhc.PSI*100:+.1f}%）")
    print("  两句读法：反解 ψ 与规范 ψ 的差、几何面积与反算面积的差，是同一个 2% 级偏差的两种写法——")
    print("  一个方程解不出两个未知数，只能说明这组参数在两三个百分点内自洽，不能说明它们各自正确。")

    # ---- 敏感性：ψ × 服务带 → 峰值深度 ----
    print()
    print("=" * 78)
    print("敏感性：ψ × 服务带宽度 → 峰值水深（cm），括号内为对台账 30.0 cm 的偏差")
    print("=" * 78)
    bands = [44.0, 54.0, 64.0, 74.0]
    psis = [0.55, 0.65, 0.70, 0.80, 0.90]
    print("  ψ \\ 带宽 " + "".join(f"{b:>15.0f} m" for b in bands))
    grid = {}
    for psi in psis:
        cells = []
        for band in bands:
            area = geo_a["road_length_m"] * band / 10_000.0
            peak = bhc.simulate(psi=psi, area_ha=area, q_lps_ha=q_lps_ha, pond_area_m2=pond_a)["peak"]
            grid[f"{psi:.2f}|{band:.0f}"] = round(peak, 4)
            flag = "*" if abs(peak - peak_a) <= 0.03 else " "
            cells.append(f"{peak*100:6.1f}{flag}({(peak-peak_a)*100:+5.1f})")
        print(f"  {psi:.2f}   " + "".join(f"{c:>17}" for c in cells))
    print("  * = 落在台账 ±3 cm 内。整片可行域是一条斜带：ψ 涨可以靠缩面积抵消，")
    print("    所以「峰值对上了」不能证明任何一个参数对，只能证明乘积 ψ·F 落在带内。")

    # ---- 逐路服务带：把「哪条路缺数据」变成一张可派的表 ----
    print()
    print("=" * 78)
    print("逐路服务带：哪条路有观测、哪条没有（数据缺口清单）")
    print("=" * 78)
    obs: dict[str, float] = {}
    orders = ROOT / "samples" / "assets" / "urban" / "work_orders.csv"
    if orders.is_file():
        import csv
        import re

        with orders.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("order_type") != "waterlogging":
                    continue
                match = re.search(r"(\d+(?:\.\d+)?)\s*cm", row.get("description") or "")
                if match:
                    obs[row["road_id"]] = float(match.group(1)) / 100.0
    bands_on_disk = bhc.load_service_band()
    # 每条路按自己的台账口径算：雨水口能力 / 泵站是否投入 / 堵塞折减都不同，
    # 拿 A-03 的抽排去套 B-01 会算出错误的带宽（第一版就是这么错的）。
    road_scenario = {
        "R-101": dict(q_intake=180.0, q_pump=bhc.Q_PUMP_M3H, clog_ratio=bhc.CLOG_RATIO, pond=pond_a),
        "R-201": dict(q_intake=150.0, q_pump=0.0, clog_ratio=1.0, pond=pond_b),
    }
    print(f"  {'路段':<8}{'带宽':>7}  {'状态':<12}{'台账水深':>9}{'反推带宽':>9}")
    for road in scene.get("roads", []):
        rid = road.get("road_id")
        geo = bhc.geometric_catchment(scene, rid, road.get("width_m") or 0.0)
        depth = obs.get(rid)
        implied = "—"
        scenario = road_scenario.get(rid)
        if depth and scenario:
            for band in range(30, 121):
                peak = bhc.simulate(psi=bhc.PSI, area_ha=geo["road_length_m"] * band / 10_000.0,
                                    q_lps_ha=q_lps_ha, pond_area_m2=scenario["pond"],
                                    q_intake=scenario["q_intake"], q_pump=scenario["q_pump"],
                                    clog_ratio=scenario["clog_ratio"])["peak"]
                if peak >= depth:
                    implied = f"{band} m"
                    break
        print(f"  {rid:<8}{geo['band_m']:>5.0f} m  {geo['band_status']:<12}"
              f"{(str(round(depth * 100)) + ' cm') if depth else '—':>9}{implied:>9}")
    pending = [rid for rid, row in bands_on_disk.items() if row.get("band_m") is None]
    print(f"  空值 {len(pending)} 条 = 用默认 54 m、未核实。表在 `samples/assets/urban/service_band.csv`，")
    print("  填一列就能改口径，不用动代码；反推值只说明「要多宽才够」，不是实测，不能直接填回去。")

    # ---- 外样本：B-01 滨河路 18 cm ----
    print()
    print("=" * 78)
    print("外样本：B-01 滨河路 18 cm（未参与标定）")
    print("=" * 78)
    area_b = geo_b["area_ha"]
    q_b = bhc.PSI * q_lps_ha * area_b * 3.6

    def b01(**kw) -> float:
        return bhc.simulate(psi=bhc.PSI, area_ha=area_b, q_lps_ha=q_lps_ha,
                            pond_area_m2=pond_b, q_intake=150.0, clog_ratio=1.0, **kw)["peak"]

    b_never = b01(q_pump=0.0)
    b_late = b01(q_pump=1800.0)
    b_early = b01(q_pump=1800.0, pump_from_min=0)
    # 要复现 18 cm 需要多大汇水面积（即多宽的服务带）
    need_area = None
    for band in range(40, 131):
        candidate = geo_b["road_length_m"] * band / 10_000.0
        peak = bhc.simulate(psi=bhc.PSI, area_ha=candidate, q_lps_ha=q_lps_ha, pond_area_m2=pond_b,
                            q_intake=150.0, q_pump=0.0, clog_ratio=1.0)["peak"]
        if peak >= peak_b:
            need_area = (band, candidate)
            break
    print(f"  几何：{geo_b['road_length_m']:.1f} m × 54 m = {area_b:.3f} ha → 产流 {q_b:.1f} m³/h")
    print("  台账：DF-IN-201 150 m³/h（in_service，不折减）；DF-PS-03 1800 m³/h（4 机组 0 故障）")
    print(f"  泵全程运行：峰值 {b_early*100:5.1f} cm（净入流为负，不积水）")
    print(f"  泵 90 分钟后投入：峰值 {b_late*100:5.1f} cm")
    print(f"  泵不投入：峰值 {b_never*100:5.1f} cm（台账 {peak_b*100:.0f} cm，偏差 "
          f"{(b_never-peak_b)*100:+.1f} cm / {(b_never-peak_b)/peak_b*100:+.0f}%）")
    if need_area:
        print(f"  要复现 {peak_b*100:.0f} cm，B-01 需要 {need_area[1]:.2f} ha（服务带 {need_area[0]} m），"
              f"而 A-03 只要 54 m")
    print("  三个边界全部低于台账，且需要的服务带宽度与 A-03 不一致 —— 结论不是「再调一个系数」，")
    print("  而是「服务带宽度应按道路/街区分别给」，台账里没有这个字段。这是模型结构问题，不是参数问题。")

    # ---- 历史外推：2020-08-11 ----
    print()
    print("=" * 78)
    print(f"历史外推：{HISTORY['date']} {HISTORY['peak_hour_site']} 最大小时雨强 "
          f"{HISTORY['peak_hour_mm']:.0f} mm/h")
    print("=" * 78)
    hist_rows = []
    for label, depth_mm, hours in (("2026-10-29（台账 20 mm/h）", bhc.RAIN_DEPTH_MM, bhc.RAIN_DURATION_H),
                                   (f"{HISTORY['date']}（实测最大小时）", HISTORY["peak_hour_mm"], 1.0)):
        q = intensity_from_depth(depth_mm=depth_mm, duration_h=hours).value
        q_in = bhc.PSI * q * 4.108 * 3.6
        to_close = ponding_forecast(inflow_m3h=q_in, outflow_m3h=18.0, pond_area_m2=pond_a,
                                    current_depth_m=0.0, target_depth_m=CLOSE_THRESHOLD_CM / 100.0)
        to_peak = ponding_forecast(inflow_m3h=q_in, outflow_m3h=18.0, pond_area_m2=pond_a,
                                   current_depth_m=0.0, target_depth_m=peak_a)
        hist_rows.append({
            "event": label, "intensity_lps_ha": round(q, 2), "runoff_m3h": round(q_in, 1),
            "to_close_min": round(to_close.value * 60, 1) if to_close.computable else None,
            "to_peak_min": round(to_peak.value * 60, 1) if to_peak.computable else None,
        })
        print(f"  {label:<26} 雨强 {q:6.1f} L/(s·ha)  产流 {q_in:7.1f} m³/h  "
              f"到 {CLOSE_THRESHOLD_CM:.0f}cm {to_close.value*60:5.1f} min  "
              f"到 {peak_a*100:.0f}cm {to_peak.value*60:5.1f} min")
    print(f"  记录只给定级（{HISTORY['level_from']} → {HISTORY['level_to']}，雨量判据）与雨量，")
    print("  没有积水深度。因此这一行只能确认「模型在 108 mm/h 下会迅速越过封路阈值」这个方向，")
    print("  不能算作偏差对账；把它当验证用，就是把量级检查冒充精度检查。")

    # ---- 雨量守恒 ----
    print()
    print("=" * 78)
    print("雨量守恒：这条曲线实际施加了多少毫米")
    print("=" * 78)
    applied = back["applied_volume_m3"] / (bhc.PSI * 4.108 * 10_000.0) * 1000.0
    print(f"  预警 {bhc.RAIN_DEPTH_MM:.0f} mm/{bhc.RAIN_DURATION_H:.0f}h（预报上限）→ "
          f"曲线 {bhc.T_TOTAL_MIN} 分钟窗口内施加 {applied:.1f} mm（{applied/bhc.RAIN_DEPTH_MM:.0%}）")
    full = sim_full_rain(q_in=back["q_in"], q_lps_ha=q_lps_ha)
    full_at_150 = full[150] * 100 if len(full) > 150 else None
    print(f"  如果按 60 mm 全程施加（雨窗 180 分钟）：150 分钟时水深 "
          f"{full_at_150:.1f} cm，180 分钟时 {full[-1]*100:.1f} cm")
    print("  也就是说，「路面见底」这一帧成立的前提是雨在 150 分钟前后停（本场景即如此），")
    print("  有效雨量约 38 mm。38 mm 是模型反推值，不是观测值；预报 60 mm 是上限，两者不能互相当证据。")

    payload = {
        "schema": "map2d-runoff-calibration-1",
        "params": {
            "psi": bhc.PSI,
            "psi_kind": "规范值",
            "psi_source": "GB 50014《室外排水设计标准》城市建成区取值",
            "catchment_ha_back_solved": 4.108,
            "catchment_kind": "反算（拟合），非实测",
            "service_band_m": geo_a["bands_m"],
            "pond_area_m2": pond_a,
            "pond_area_kind": "实测（scene_layout 积水多边形 shoelace 面积）",
            "intake_m3h": bhc.Q_INTAKE_M3H,
            "intake_kind": "台账（DF-IN-101）",
            "clog_ratio": bhc.CLOG_RATIO,
            "clog_kind": "假设（台账只登记「已堵塞」，无程度）",
            "pump_m3h": bhc.Q_PUMP_M3H,
            "pump_kind": "台账推算（1200×2/3，1# 机组故障）",
        },
        "a03": {
            "road_length_m": geo_a["road_length_m"],
            "geometric_ha": geo_a["areas_ha"],
            "back_solved_ha": 4.108,
            "implied_band_m": round(4.108 * 10_000 / geo_a["road_length_m"], 1),
            "predicted_peak_m_geo_base": round(base_run["peak"], 4),
            "ledger_peak_m": peak_a,
            "deviation_cm": round((base_run["peak"] - peak_a) * 100, 2),
            "deviation_pct": round((base_run["peak"] - peak_a) / peak_a * 100, 2),
            "psi_solved_for_geo_base": round(psi_solved, 4),
        },
        "b01_out_of_sample": {
            "road_length_m": geo_b["road_length_m"],
            "catchment_ha": round(area_b, 4),
            "runoff_m3h": round(q_b, 1),
            "ledger_peak_m": peak_b,
            "peak_pump_early_m": round(b_early, 4),
            "peak_pump_late_m": round(b_late, 4),
            "peak_pump_never_m": round(b_never, 4),
            "deviation_pump_never_cm": round((b_never - peak_b) * 100, 2),
            "band_needed_m": need_area[0] if need_area else None,
            "band_needed_ha": round(need_area[1], 3) if need_area else None,
            "missing_field": "服务带宽度（逐路）；泵站在本场是否启动，台账无记录",
            "verdict": "三个抽排边界全部低于台账，且所需服务带宽度与 A-03 不一致：结构问题，不是参数问题",
        },
        "sensitivity": {"band_m": bands, "psi": psis, "peak_m": grid},
        "history": {"meta": HISTORY, "rows": hist_rows},
        "rain_balance": {
            "forecast_mm": bhc.RAIN_DEPTH_MM,
            "applied_mm": round(applied, 1),
            "ratio": round(applied / bhc.RAIN_DEPTH_MM, 3),
            "window_min": bhc.T_TOTAL_MIN,
            "full_rain_depth_at_150min_m": None if full_at_150 is None else round(full_at_150 / 100, 4),
            "full_rain_depth_at_180min_m": None if not full else round(full[-1], 4),
        },
        "verdict": {
            "calibration_points": 1,
            "validation_points": 0,
            "out_of_sample_checks": 2,
            "statement": "1 个标定点、2 个样本外检查（B-01 与外推）、0 个验证点；"
                         "参数可追溯到规范与几何，但精度未被独立验证",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
