# 泵站前池库容曲线（假设值）数据说明

- 日期：2026-11-05
- 数据文件：`samples/assets/urban/pump_forebay_curves.csv`
- 用途：给 P13 简化水动力演进（`level_pool_route`）提供"泵站停机后水位过程线"所需的**库容曲线输入**
- 状态：**假设值，未经图纸/实测核实**；已注册为数据集资产（`forebay_curve`，资产数 18→19），
  来源级别由 CSV 的 `value_basis` 驱动，不接入六跳默认口径

## 1 为什么需要它

P13 的 `level_pool_route` 需要一个"水位 → 容积"关系才能算水位过程线。
此前台账里只有泵站能力（`capacity_m3h`、`pump_units`、`failed_units`）与状态，**没有任何前池几何或库容信息**，
所以「A 区泵站停机后水位过程」只能停在"可算但没输入"。本文件补上这条输入。

## 2 文件结构

| 列 | 含义 | 本文件取值 |
| --- | --- | --- |
| `facility_id` | 设施编号，对齐 `drainage_facilities.csv` | `DF-PS-01` |
| `stage_m` | 前池水位，基准为**池底 0 m**（相对值，不是城市绝对高程） | 0.0 – 3.0，步长 0.5 |
| `plan_area_m2` | 该水位下的水平投影面积 | 100（矩形等截面假设） |
| `storage_m3` | 该水位以下容积 | 0 / 50 / … / 300 |
| `warning_stage_m` | 假设的警戒水位（占有效水深 80%） | 2.4 |
| `value_basis` | 来源级别，取值 `real` / `derived` / `assumption` | `assumption` |
| `source_ref` | 依据出处；假设值写「待补：…」 | 待补 DF-PS-01 前池竣工图/设计图 |
| `note` | 人读说明；**只在首个点写依据指针，其余留空**（换假设时只改 1 处） | 第 1 行：假设摘要 + 依据指针 |

曲线为等截面矩形前池，因此 `storage_m3 = plan_area_m2 × stage_m`（严格线性）。**这不是"简化画法"导致的巧合，
而是假设本身**：前池按矩形、垂直池壁处理；若实测有斜壁或台阶，曲线会非线性，届时只换数据、不动模型。

## 3 假设链条（这是"假设值"的全部依据，答辩时按此说明）

1. 前池平面 10 m × 10 m（100 m²）、有效水深 3.0 m → 有效容积 **300 m³**；
2. 300 m³ 相对额定抽排能力 1200 m³/h 约 **15 分钟**，属小型雨水泵站前池的常见量级；
3. 警戒水位取有效水深的 80% = **2.4 m**（不来自任何规范，是调度口径假设）；
4. 池顶（漫溢）按有效水深 3.0 m 处处理。

每一步都是假设，**不是**设计值、不是实测值，也不来自任何一份台账原文。

## 4 怎么用（走 loader：来源自动带上）

```python
from decision_poc.quant import level_pool_route, time_to_level
from decision_poc.urban_dataset import load_forebay_curves

curve = load_forebay_curves()["DF-PS-01"]   # stage_m / storage_m3 / warning_stage_m / provenance 一起回来

DT_H = 0.01                        # 1 个时段 = 0.01 h = 0.6 min（见下方换算提醒）
STOP_MIN = 10.0                    # 停机 10 分钟
STOP_STEPS = round(STOP_MIN / (DT_H * 60))   # → 17 个时段，不是 10 个
N = 60
pump = [1200.0] + [0.0] * STOP_STEPS + [1200.0] * (N - 1 - STOP_STEPS)  # t0 仍在运行，之后停机再复机

hydrograph = level_pool_route(
    inflow_m3h=[1200.0] * N,       # 来水过程线（这里按额定能力 1200 m³/h 示例）
    dt_h=DT_H,
    stage_m=curve.stage_m,
    storage_m3=curve.storage_m3,
    initial_stage_m=0.0,
    pump_outflow_m3h=pump,
    storage_provenance=curve.provenance,   # ← 只把来源原样传下去，不自己写 assumption/real
)
print(hydrograph.assumptions[-1])          # 库容来源：assumption｜待补：DF-PS-01 前池竣工图/设计图
print(hydrograph.peak, hydrograph.peak_time_h)                                                  # 2.04 m @ 0.18 h
print(time_to_level(hydrograph=hydrograph, target_level_m=curve.warning_stage_m).computable)     # False：全程未到警戒
```

**不要**自己读 CSV 拼数组再手写来源级别——数值和 `value_basis` 一旦各说一套，就是这次资产化要堵的漏。
需要曲线形状时可以读 `curve.stage_m` / `curve.storage_m3`，但来源只认 `curve.provenance`。

> **时长换算提醒（踩过一次的坑）**：`dt_h=0.01` 表示每时段 0.6 分钟，所以「10 分钟」是 **17 个时段**，
> 「10 个时段」只有 6 分钟。写停机时长时一律用 `分钟 / (dt_h × 60)` 换算，别把步数当时长——
> 本轮初稿就是把 10 个时段当成 10 分钟，峰值被低估了近一半（1.20 m vs 实际 2.04 m）。

## 5 演示算例（数字来自上面这条链路实算，非手写）

| 算例 | 输入 | 结果 |
| --- | --- | --- |
| 停机 10 分钟后复机 | 来水 1200 m³/h、dt=0.01 h、停机 17 个时段（=10.2 min） | 峰值 **2.04 m**（0.18 h），未到 2.4 m 警戒，但只剩 **0.36 m** 余量 → 短时停机可容忍，窗口只有约 10 分钟（12 分钟即到警戒） |
| 持续停机 | 来水 1200 m³/h | **12 min** 到警戒（2.4 m）、**15 min** 满溢（3.0 m）；越过池顶后模块返回 `uncomputable`，不截断 |
| 持续停机（按 A 区设计来水） | 来水 11138.4 m³/h | **1.3 min** 到警戒、**1.6 min** 满溢 —— 前池对该量级来水几乎没有缓冲 |

第 2、3 行的满溢时间由 `storage / inflow` 线性反算（等截面假设下与模型解析一致）；
模型在越过库容曲线顶部时按契约返回"超出库容曲线上限"，**不截断、不外推**。

**敏感性**：等截面假设下，到阈时间与 `plan_area_m2 × 有效水深` 成反比；面积或水深假设偏差 20%，
到阈时间同量级偏差约 20%。所以这些数字只能用于量级与排序，不能当成实测预警提前量。

## 6 已注册进 `URBAN_ASSETS`（2026-11-06 资产化）

`src/decision_poc/urban_dataset.py` 的 `URBAN_ASSETS` 是显式清单。资产化一次改全的四处口径：

| 项 | 变化 |
| --- | --- |
| 资产数 | `manifest.json` 18 → **19**（新增 `forebay_curve` 类型 1 份） |
| 资产解析 | `p8_eval.assets_parsed / assets_total` 18/18 → **19/19**（`samples/eval/latest.json` 与 p8 报告一并重生成） |
| 门禁事实 | `data_gate` 新增 `pump:DF-PS-01/forebay_effective_volume_m3` = 300 m³、来源 `assumption`，事实数 69 → **70**（`FACTS_TOTAL` 与期望文件同步） |
| 来源级别 | 由数据层 `load_forebay_curves()` 从 CSV 的 `value_basis` 直接读出，**不在这里二次声明**；"登记级别 == CSV 级别"由 `test_data_gate_fact_level_follows_csv_basis` 守住 |

**资产化真正要解决的问题不是"把文件加进清单"，而是堵住"数字换了、来源没换"**：

1. 唯一真源是 CSV 的 `value_basis` 列；
2. `load_forebay_curves()` 返回 `ForebayCurve(stage_m, storage_m3, warning_stage_m, provenance)`，
   调用方不需要、也不允许自己再写 `assumption` / `real`；
3. 把 `curve.provenance` 原样传给 `level_pool_route(storage_provenance=...)`，来源随过程线
   一路走到 `time_to_level` 的 `assumptions`（固定前缀行 `库容来源：<basis>｜<ref>`）；
4. 最后一跳由 `test_loader_provenance_reaches_water_level_chain` 守住：只给设施号，
   算出来的到达时刻仍带「库容来源：assumption｜待补：DF-PS-01 前池竣工图/设计图」。

**补齐实测资料后要做的只剩两件**：换 CSV 的数值与 `value_basis` / `source_ref`，然后跑
`python -m decision_poc.urban_dataset`、`python -m decision_poc.urban_eval --report ...`、
`python -m decision_poc.evidence_check`。模型、契约、测试都不用改——守卫会随 `value_basis`
自动放行实测曲线（见 `tests/test_hydro_forebay_data.py` 文件头说明）。

## 7 待核实清单（补齐后即可从"假设输入"变成"实测输入"）

| 项 | 需要的原始资料 |
| --- | --- |
| 前池平面尺寸与池深 | DF-PS-01 前池竣工图 / 设计图 |
| 池底与池顶绝对高程 | 泵站高程表（注意与城市绝对高程的基准换算） |
| 警戒水位依据 | 泵站运行规程或调度预案原文 |
| 是否有溢流堰/溢流管及其尺寸 | 泵站工艺图（有则可给 `spill_crest_m` / `spill_width_m`） |
| 泵特性曲线 H–Q | 泵组样本（缺则抽排能力只能按额定常值处理） |
