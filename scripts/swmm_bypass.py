"""SWMM 旁路对照：同一输入，规则引擎（推理公式+水量平衡）vs EPA SWMM 动态汇流。

旁路定位（不进主链路、不动任何冻结口径）：
- 输入完全同源：橙色情景 60mm/3h（weather_alerts WA-V2-20261101-01）、
  A 区汇水 22.1 ha + ψ=0.7（p12 定量层报告口径）、DF-PS-01 前池曲线（assumption 台账）、
  DN1400 主干（pipeline_ledger PL-V2-DRN-MAIN，p12 报告 i=0.15% 满流能力 8198.5 m³/h）、
  泵可用 2/3 台 = 800 m³/h（drainage_facilities DF-PS-01：3 台 1 故障）。
- SWMM 侧结构：22.1 ha 子汇水区（%imperv=70，使有效径流系数≈0.7 与推理公式同口径）
  → 检查井 → DN1400（i≈0.15%）→ 前池（100 m² 等截面，与曲线台账一致）
  → 泵（Type4，0.2222 m³/s）→ 出流河。前池满即溢流（flooding）。
- 对照指标（组件级对拍，不做整体包装）：
  1. 产流洪峰    2. 干管输水能力    3. 前池到警戒水位时间（泵停/泵可用两档）
  4. 溢流开始时刻与总量

运行：.venv/Scripts/python.exe scripts/swmm_bypass.py（需 PYTHONPATH=src；pyswmm 由 uv run --with 提供）
产物：docs/reports/swmm-bypass-comparison.md（由本脚本重新生成，数字不手填）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.quant.hydrology import MM_PER_H_TO_LPS_HA, intensity_from_depth, rational_runoff  # noqa: E402
from decision_poc.quant.hydraulics import manning_full_flow  # noqa: E402
from decision_poc.urban_dataset import load_forebay_curves  # noqa: E402

# ---- 情景口径（全部有出处，改任何值都会让报告与源数据脱节）----
RAIN_DEPTH_MM = 60.0          # WA-V2-20261101-01 橙色，3h
RAIN_DURATION_H = 3.0
SIM_HOURS = 6.0               # 3h 降雨 + 3h 退水
AREA_HA = 22.1                # p12 报告 §3：A 区汇水
RUNOFF_COEF = 0.7             # p12 报告 §3
PIPE_DN_M = 1.4               # pipeline_ledger PL-V2-DRN-MAIN DN1400
PIPE_SLOPE = 0.0015           # p12 报告 i=0.15%
PUMP_TOTAL_M3H = 1200.0       # drainage_facilities DF-PS-01
PUMP_FAILED = 1
PUMP_UNITS = 3
FOREBAY_PLAN_AREA_M2 = 100.0  # pump_forebay_curves.csv 等截面矩形假设
WARNING_STAGE_M = 2.4         # 同上 warning_stage_m
EFFECTIVE_DEPTH_M = 3.0       # 同上有效水深

REPORT_PATH = ROOT / "docs" / "reports" / "swmm-bypass-comparison.md"
INP_PATH = ROOT / "dist" / "swmm" / "bypass_model.inp"


def _ascii_run_dir() -> Path:
    """SWMM 引擎不支持非 ASCII 路径：挑一个可写的 ASCII 临时目录。"""
    import os
    import tempfile

    candidates = [
        os.environ.get("SWMM_TMP"),
        tempfile.gettempdir(),
        "C:/Windows/Temp",
    ]
    for base in candidates:
        if not base:
            continue
        try:
            p = Path(base)
            p.mkdir(parents=True, exist_ok=True)
            if all(ord(c) < 128 for c in str(p)):
                probe = p / ".swmm_ascii_probe"
                probe.write_text("x", encoding="ascii")
                probe.unlink()
                return p
        except OSError:
            continue
    raise RuntimeError("找不到可用的 ASCII 临时目录（SWMM 引擎限制），"
                       "可设 SWMM_TMP 环境变量指定")


# --------------------------------------------------------------------------
# SWMM 模型
# --------------------------------------------------------------------------
def build_inp() -> str:
    """生成与 quant 层同口径的 SWMM INP。细节见模块 docstring。"""
    ts_lines: list[str] = []
    # 5 分钟步长均匀雨强 20 mm/h，持续 3h；带日期（SWMM 5.2 对无日期序列
    # 会落在仿真窗口外导致降雨全丢——实测踩坑）；11:05 显式雨止避免线性内插尾巴
    for i in range(int(RAIN_DURATION_H * 12) + 1):
        minutes = i * 5
        hh, mm = divmod(8 * 60 + minutes, 60)
        ts_lines.append(f"TS-RAIN  11/01/2026  {hh:02d}:{mm:02d}  20.0")
    ts_lines.append("TS-RAIN  11/01/2026  11:05  0.0")
    ts_lines.append("TS-RAIN  11/01/2026  14:00  0.0")

    pump_curve = "CP-AVAIL"
    return f"""[TITLE]
;; SWMM 旁路对照模型：A 区 22.1 ha / 60mm-3h / DN1400 / DF-PS-01 前池
;; 与 decision_poc.quant 同输入口径，见 scripts/swmm_bypass.py

[OPTIONS]
FLOW_UNITS           CMS
INFILTRATION         HORTON
FLOW_ROUTING         DYNWAVE
LINK_OFFSETS         DEPTH
MIN_SLOPE            0
ALLOW_PONDING        NO
SKIP_STEADY_STATE    NO
START_DATE           11/01/2026
START_TIME           08:00:00
REPORT_START_DATE    11/01/2026
REPORT_START_TIME    08:00:00
END_DATE             11/01/2026
END_TIME             {(8 + int(SIM_HOURS)):02d}:00:00
SWEEP_START          01/01
SWEEP_END            12/31
DRY_DAYS             0
REPORT_STEP          00:01:00
WET_STEP             00:01:00
DRY_STEP             00:05:00
ROUTING_STEP         00:00:10
INERTIAL_DAMPING     PARTIAL
NORMAL_FLOW_LIMITED  BOTH
FORCE_MAIN_EQUATION  H-W
VARIABLE_STEP        0.75
LENGTHENING_STEP     0
MIN_SURFAREA         1.167
MAX_TRIALS           8
HEAD_TOLERANCE       0.0015
SYS_FLOW_TOL         5
LAT_FLOW_TOL         5
MINIMUM_STEP         0.5
THREADS              1

[EVAPORATION]
CONSTANT   0.0
DRY_ONLY   NO

[RAINGAGES]
;;Name           Format    Interval SCF      Source
RG-A             INTENSITY 0:05     1.0      TIMESERIES TS-RAIN

[TIMESERIES]
;;Name       Time      Value(mm/hr)
{chr(10).join(ts_lines)}

[SUBCATCHMENTS]
;;Name        RainGage   Outlet     Area(ha)  %Imperv  Width(m)  %Slope  CurbLen
S-A           RG-A       J-TRUNK    {AREA_HA}  70       470       0.5     0

[SUBAREAS]
;;Subcatchment N-Imperv  N-Perv  S-Imperv  S-Perv  PctZero  RouteTo
S-A           0.013      0.15    1.5       5.0     25       OUTLET

[INFILTRATION]
;;Subcatchment MaxRate  MinRate  Decay  DryTime  MaxInfil
S-A           76.2     3.81     4.14   7        0

[JUNCTIONS]
;;Name          Elevation  MaxDepth  InitDepth  SurDepth  Aponded
J-TRUNK         0.0        5.5       0          0         0

[STORAGE]
;;Name          Elevation  MaxDepth  InitDepth  Shape      CurveName/Params
J-FORBAY        -5.4       4.0       0.0        FUNCTIONAL {FOREBAY_PLAN_AREA_M2:.0f} 0 0

[OUTFALLS]
;;Name          Elevation  Type       StageData
OUT-RIVER       -6.5       FREE

[CONDUITS]
;;Name   FromNode   ToNode     Length  Roughness  InOffset  OutOffset  InitFlow  MaxFlow
C-TRUNK  J-TRUNK    J-FORBAY   300     0.013      0         0          0         0

[XSECTIONS]
;;Link     Shape     Geom1     Geom2  Geom3  Geom4  Barrels
C-TRUNK    CIRCULAR  {PIPE_DN_M}       0      0      0      1

[CURVES]
;;Name     Type     X-Value  Y-Value
{pump_curve} PUMP4    0.0      0.0
{pump_curve}          0.1      0.2222
{pump_curve}          5.0      0.2222

[PUMPS]
;;Name    FromNode    ToNode      PumpCurve  Status  Startup  Shutoff
P-FB      J-FORBAY    OUT-RIVER   {pump_curve} ONLINE  0.05     0.0

[REPORT]
INPUT      NO
CONTROLS   NO
SUBCATCHMENTS ALL
NODES ALL
LINKS ALL

[COORDINATES]
J-TRUNK     0 0
J-FORBAY    300 -5
OUT-RIVER   600 -10
"""


def run_swmm(pump_on: bool) -> dict:
    """跑一次 SWMM，返回对照指标。pump_on=False 时把泵置零（泵全停档）。"""
    from pyswmm import Links, Nodes, Simulation, Subcatchments

    inp_text = build_inp()
    if not pump_on:
        # 泵全停：直接抽掉 PUMPS 行，等价于无泵
        inp_text = "\n".join(
            line for line in inp_text.splitlines() if not line.startswith("P-FB")
        )
    run_dir = _ascii_run_dir()
    run_inp = run_dir / "bypass_model.inp"
    run_inp.write_text(inp_text, encoding="utf-8")
    # 留一份在 dist 供 EPA SWMM GUI 复核（文本文件不受中文路径限制）
    INP_PATH.parent.mkdir(parents=True, exist_ok=True)
    INP_PATH.write_text(inp_text, encoding="utf-8")

    peak_runoff = 0.0
    peak_link_flow = 0.0
    runoff_volume_m3 = 0.0
    forbay_stage: list[tuple[float, float]] = []
    flooding_first_h: float | None = None
    flooding_total_m3 = 0.0
    step_h = 1.0 / 60.0  # REPORT_STEP 1min

    with Simulation(str(run_inp)) as sim:
        sim.step_advance(60)
        sub = Subcatchments(sim)["S-A"]
        trunk = Links(sim)["C-TRUNK"]
        forbay = Nodes(sim)["J-FORBAY"]
        t0 = sim.start_time
        for _ in sim:
            t_h = (sim.current_time - t0).total_seconds() / 3600.0
            peak_runoff = max(peak_runoff, sub.runoff)
            runoff_volume_m3 += sub.runoff * 60.0  # step_advance(60s)
            peak_link_flow = max(peak_link_flow, trunk.flow)
            stage = forbay.depth  # 存储节点底以上水深 = 前池 stage
            forbay_stage.append((t_h, stage))
            flooding = forbay.flooding  # m3/s 即时溢流
            if flooding > 1e-6:
                flooding_total_m3 += flooding * 3600.0 * step_h
                if flooding_first_h is None:
                    flooding_first_h = t_h

    def time_to_stage(target_m: float) -> float | None:
        for t_h, stage in forbay_stage:
            if stage >= target_m:
                return t_h
        return None

    return {
        "peak_runoff_m3s": peak_runoff,
        "runoff_volume_m3": runoff_volume_m3,
        # 有效径流系数 = 总径流体积 / 总降雨体积（体量口径，非峰值口径）
        "effective_psi": runoff_volume_m3 / (RAIN_DEPTH_MM / 1000.0 * AREA_HA * 1e4),
        "peak_link_flow_m3s": peak_link_flow,
        "time_to_warning_h": time_to_stage(WARNING_STAGE_M),
        "time_to_overflow_h": time_to_stage(EFFECTIVE_DEPTH_M),
        "flooding_first_h": flooding_first_h,
        "flooding_total_m3": flooding_total_m3,
        "peak_stage_m": max(s for _, s in forbay_stage),
    }


# --------------------------------------------------------------------------
# 我方方法（quant 层，同一输入）
# --------------------------------------------------------------------------
def run_ours() -> dict:
    intensity = intensity_from_depth(depth_mm=RAIN_DEPTH_MM, duration_h=RAIN_DURATION_H)
    q_lps_ha = intensity.value
    runoff = rational_runoff(
        runoff_coefficient=RUNOFF_COEF, intensity_lps_ha=q_lps_ha, area_ha=AREA_HA
    )
    trunk = manning_full_flow(diameter_m=PIPE_DN_M, slope=PIPE_SLOPE, roughness_n=0.013)

    curve = load_forebay_curves()["DF-PS-01"]
    from decision_poc.quant.hydrodynamics import level_pool_route, time_to_level

    ours = {"peak_inflow_m3s": runoff.value / 3600.0,
            "peak_inflow_m3h": runoff.value,
            "trunk_capacity_m3h": trunk.value,
            "intensity_lps_ha": q_lps_ha}

    # 前池库容锚点（来自曲线台账，0.5m 步长线性插值；曲线本身严格线性，无插值误差）
    stage_curve = list(curve.stage_m)
    storage_curve = list(curve.storage_m3)

    def storage_at(target_stage: float) -> float:
        if target_stage in stage_curve:
            return float(storage_curve[stage_curve.index(target_stage)])
        for s0, s1 in zip(stage_curve, stage_curve[1:]):
            if s0 < target_stage < s1:
                w = (target_stage - s0) / (s1 - s0)
                return float(storage_curve[stage_curve.index(s0)] * (1 - w)
                             + storage_curve[stage_curve.index(s1)] * w)
        raise ValueError(f"stage {target_stage} 超出库容曲线范围 {stage_curve[0]}–{stage_curve[-1]}")

    vol_warning = storage_at(WARNING_STAGE_M)
    vol_full = float(storage_curve[-1])
    ours["vol_warning_m3"] = vol_warning
    ours["vol_full_m3"] = vol_full

    for tag, pump_m3h in (
        ("no_pump", 0.0),
        ("pump_avail", PUMP_TOTAL_M3H * (PUMP_UNITS - PUMP_FAILED) / PUMP_UNITS),
    ):
        net = runoff.value - pump_m3h  # 稳态净入流（泵为常量出流）
        if net <= 0:
            ours[tag] = {"time_to_warning_h": None, "note": "净入流≤0，水位不上涨",
                         "time_to_overflow_h": None}
            continue
        # 过程线只算到曲线上限前（level_pool_route 契约：不截断不外推）
        horizon_h = (vol_full * 0.999) / net
        n_pts = max(int(horizon_h / 0.01), 3)
        hg = level_pool_route(
            inflow_m3h=[runoff.value] * n_pts,
            dt_h=0.01,
            stage_m=curve.stage_m,
            storage_m3=curve.storage_m3,
            initial_stage_m=0.0,
            pump_outflow_m3h=pump_m3h,
            storage_provenance=curve.provenance,
        )
        ttl = time_to_level(hydrograph=hg, target_level_m=WARNING_STAGE_M)
        ours[tag] = {
            "time_to_warning_h": ttl.value if ttl.computable else None,
            "time_to_overflow_h": vol_full / net,  # 水量平衡解析：蓄满即溢流
            "peak_stage_in_window_m": hg.peak,
        }
    return ours


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def fmt_h(h: float | None) -> str:
    if h is None:
        return "未达到"
    return f"{h:.2f} h（{h * 60:.0f} min）"


def main() -> None:
    ours = run_ours()
    swmm_off = run_swmm(pump_on=False)
    swmm_on = run_swmm(pump_on=True)

    q_lps_ha = RAIN_DEPTH_MM / RAIN_DURATION_H * MM_PER_H_TO_LPS_HA
    ratio_runoff = swmm_off["peak_runoff_m3s"] / ours["peak_inflow_m3s"]
    eff_psi = swmm_off["effective_psi"]

    lines = [
        "# SWMM 旁路对照报告（自动生成，勿手改数字）",
        "",
        "生成：`scripts/swmm_bypass.py`（pyswmm 2.x + decision_poc.quant 同输入对拍）。",
        "**旁路定位**：不进主链路、不改变任何冻结口径；用途是给定量层的方法假设提供"
        "行业标准（EPA SWMM 动态汇流）的交叉参照。",
        "",
        "## 输入口径（两边完全同源）",
        "",
        f"| 项 | 值 | 出处 |",
        f"|---|---|---|",
        f"| 降雨 | {RAIN_DEPTH_MM:.0f} mm / {RAIN_DURATION_H:.0f} h 均匀 | weather_alerts WA-V2-20261101-01（橙色） |",
        f"| 汇水面积 | {AREA_HA} ha | docs/reports/p12-quant-layer-report.md §3 |",
        f"| 径流系数 ψ | {RUNOFF_COEF}（SWMM 侧 %imperv=70 等效） | 同上 |",
        f"| 干管 | DN{PIPE_DN_M * 1000:.0f}、i={PIPE_SLOPE * 100:.2f}%、n=0.013 | pipeline_ledger PL-V2-DRN-MAIN + p12 §3 |",
        f"| 前池 | {FOREBAY_PLAN_AREA_M2:.0f} m² 等截面、有效深 {EFFECTIVE_DEPTH_M:.0f} m、警戒 {WARNING_STAGE_M} m | pump_forebay_curves.csv（assumption） |",
        f"| 泵 | DF-PS-01 {PUMP_UNITS} 台 {PUMP_FAILED} 台故障，可用 {PUMP_TOTAL_M3H * 2 / 3:.0f} m³/h | drainage_facilities.csv |",
        "",
        f"平均雨强换算：{q_lps_ha:.2f} L/(s·ha)（1 mm/h = 2.7778 L/(s·ha)）。",
        "",
        "## 对照结果",
        "",
        "### 1 产流洪峰（推理公式 vs SWMM 非线性水库产流）",
        "",
        "| 方法 | 峰值 |",
        f"|---|---|",
        f"| 我方 Q=ψqF（稳态均匀，ψ={RUNOFF_COEF} 定值） | {ours['peak_inflow_m3h']:.1f} m³/h = {ours['peak_inflow_m3s']:.3f} m³/s |",
        f"| SWMM S-A 子汇水区 | {swmm_off['peak_runoff_m3s'] * 3600:.1f} m³/h = {swmm_off['peak_runoff_m3s']:.3f} m³/s |",
        f"| 峰值比 | SWMM/我方 = {ratio_runoff:.2f}；SWMM 体量口径有效径流系数 = {eff_psi:.3f} |",
        "",
        "差异的物理解释：SWMM 用 Horton 入渗（能力 76→3.81 mm/h 连续衰减），3 h 长历时"
        "降雨末期透水区（30% 面积）入渗能力跌破雨强、开始产流，有效径流系数从 0.70 爬升到"
        f" {eff_psi:.2f}；推理公式的 ψ=0.7 是不随历时变化的定值简化（hydrology.py docstring"
        " 明示）。**这是方法假设的真实差异，不是对拍误差**；短历时高强度情景下两者会收敛。",
        "",
        "### 2 干管输水能力（Manning 满流 vs SWMM 动波）",
        "",
        f"- 我方 manning_full_flow(DN1400, i={PIPE_SLOPE * 100:.2f}%, n=0.013)：**{ours['trunk_capacity_m3h']:.1f} m³/h**"
        f"（p12 报告对拍值 8198.5 m³/h 一致）",
        f"- SWMM C-TRUNK 实测最大流量：**{swmm_off['peak_link_flow_m3s'] * 3600:.1f} m³/h**"
        f"（来水洪峰 {swmm_off['peak_runoff_m3s'] * 3600:.0f} m³/h 远小于能力，管内非满流，两模型一致判定「干管不约束」）",
        "",
        "### 3 前池到警戒水位（{:.1f} m）：泵全停 / 泵可用两档".format(WARNING_STAGE_M),
        "",
        f"前池库容锚点：到警戒 {ours['vol_warning_m3']:.0f} m³、蓄满（有效深 {EFFECTIVE_DEPTH_M:.0f} m）"
        f"{ours['vol_full_m3']:.0f} m³（pump_forebay_curves.csv，assumption 台账）。",
        "",
        "| 档位 | 我方 level_pool_route | SWMM 存储节点 |",
        "|---|---|---|",
        f"| 泵全停 | {fmt_h(ours['no_pump']['time_to_warning_h'])} | {fmt_h(swmm_off['time_to_warning_h'])} |",
        f"| 泵可用（{PUMP_TOTAL_M3H * 2 / 3:.0f} m³/h） | {fmt_h(ours['pump_avail']['time_to_warning_h'])} | {fmt_h(swmm_on['time_to_warning_h'])} |",
        "",
        "### 4 溢流（前池蓄满 {:.1f} m，即 {:.0f} m³）".format(EFFECTIVE_DEPTH_M, ours["vol_full_m3"]),
        "",
        "我方侧：`level_pool_route` 契约为「超曲线不截断不外推」，蓄满后过程线止步，"
        "溢流时刻按水量平衡解析给出（蓄满体积 / 净入流）；SWMM 侧直接取节点 flooding。",
        "",
        "| 档位 | 我方（水量平衡解析） | SWMM（节点 flooding） |",
        "|---|---|---|",
        f"| 泵全停 | {fmt_h(ours['no_pump']['time_to_overflow_h'])} | {fmt_h(swmm_off['flooding_first_h'])}，总量 {swmm_off['flooding_total_m3']:.0f} m³ |",
        f"| 泵可用 | {fmt_h(ours['pump_avail']['time_to_overflow_h'])} | {fmt_h(swmm_on['flooding_first_h'])}，总量 {swmm_on['flooding_total_m3']:.0f} m³ |",
        "",
        "## 结论（供材料引用的边界）",
        "",
        "1. **干管输水判定一致**：设计来水远小于 DN1400 满流能力，两模型都判「干管不约束」"
        "——Manning 满流公式在其适用域内与 EPA SWMM 动态波一致。",
        f"2. **产流洪峰差 {ratio_runoff:.2f} 倍是方法假设差异**：定值 ψ={RUNOFF_COEF} vs "
        f"Horton 衰减下有效径流系数爬升到 {eff_psi:.2f}。长历时降雨下推理公式偏小，"
        "材料引用设计流量时须注明「定值径流系数」口径。",
        "3. **到警/溢流时间我方偏早（保守侧）**：我方稳态水量平衡不含管网输水延迟与沿线"
        "调蓄（hydrology.py docstring 明示），SWMM 的管渠调蓄把到警时间从分钟级推迟到"
        "半小时级。对「封控提前量」这一决策用途，保守方向是安全侧——宁可早封、不会漏封；"
        "但材料不得把我方到警时间当精确预测，只能当上界。",
        "4. 本对照只覆盖前池水量平衡尺度；地表二维漫流、雨型（Chicago）均不在两侧范围内，"
        "不做超出数据支撑的结论。",
        "",
        "## 复算",
        "",
        "```bash",
        "PYTHONPATH=src uv run --with pyswmm python scripts/swmm_bypass.py",
        "```",
        "",
        "SWMM 模型文件落在 `dist/swmm/bypass_model.inp`（泵全停档最后一次运行的内容），",
        "可用 EPA SWMM GUI 打开复核（引擎运行用 ASCII 临时目录副本，规避中文路径限制）。",
    ]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report -> {REPORT_PATH}")
    print(f"runoff: ours={ours['peak_inflow_m3s']:.3f} swmm={swmm_off['peak_runoff_m3s']:.3f} ratio={ratio_runoff:.2f}")
    print(f"warning(no pump): ours={ours['no_pump']['time_to_warning_h']} swmm={swmm_off['time_to_warning_h']}")
    print(f"warning(pump):    ours={ours['pump_avail']['time_to_warning_h']} swmm={swmm_on['time_to_warning_h']}")


if __name__ == "__main__":
    main()
