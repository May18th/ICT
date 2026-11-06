# 泵站前池库容曲线（假设值）数据说明

- 日期：2026-11-05
- 数据文件：`samples/assets/urban/pump_forebay_curves.csv`
- 用途：给 P13 简化水动力演进（`level_pool_route`）提供"泵站停机后水位过程线"所需的**库容曲线输入**
- 状态：**假设值，未经图纸/实测核实**；本轮只补输入数据，不接入六跳默认口径

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
| `note` | 人读说明 | 假设链条摘要 |

曲线为等截面矩形前池，因此 `storage_m3 = plan_area_m2 × stage_m`（严格线性）。**这不是"简化画法"导致的巧合，
而是假设本身**：前池按矩形、垂直池壁处理；若实测有斜壁或台阶，曲线会非线性，届时只换数据、不动模型。

## 3 假设链条（这是"假设值"的全部依据，答辩时按此说明）

1. 前池平面 10 m × 10 m（100 m²）、有效水深 3.0 m → 有效容积 **300 m³**；
2. 300 m³ 相对额定抽排能力 1200 m³/h 约 **15 分钟**，属小型雨水泵站前池的常见量级；
3. 警戒水位取有效水深的 80% = **2.4 m**（不来自任何规范，是调度口径假设）；
4. 池顶（漫溢）按有效水深 3.0 m 处处理。

每一步都是假设，**不是**设计值、不是实测值，也不来自任何一份台账原文。

## 4 怎么用（当前：手动输入模型）

```python
import csv
from decision_poc.quant import level_pool_route, time_to_level

rows = list(csv.DictReader(open("samples/assets/urban/pump_forebay_curves.csv", encoding="utf-8")))
stages = [float(r["stage_m"]) for r in rows]
storages = [float(r["storage_m3"]) for r in rows]

DT_H = 0.01                        # 1 个时段 = 0.01 h = 0.6 min（见下方换算提醒）
STOP_MIN = 10.0                    # 停机 10 分钟
STOP_STEPS = round(STOP_MIN / (DT_H * 60))   # → 17 个时段，不是 10 个
N = 60

pump = [1200.0] + [0.0] * STOP_STEPS + [1200.0] * (N - 1 - STOP_STEPS)  # t0 仍在运行，之后停机再复机

hydrograph = level_pool_route(
    inflow_m3h=[1200.0] * N,       # 来水过程线（这里按额定能力 1200 m³/h 示例）
    dt_h=DT_H,
    stage_m=stages,
    storage_m3=storages,
    initial_stage_m=0.0,
    pump_outflow_m3h=pump,
)
print(hydrograph.peak, hydrograph.peak_time_h)                        # 2.04 m @ 0.18 h
print(time_to_level(hydrograph=hydrograph, target_level_m=2.4).computable)  # False：全程未到警戒
```

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

## 6 为什么本轮没有注册进 `URBAN_ASSETS`

`src/decision_poc/urban_dataset.py` 的 `URBAN_ASSETS` 是显式清单，注册后会连锁改变三处冻结口径：

- `samples/assets/urban/manifest.json` 资产数 18 → 19；
- `evidence_check` 的 `p8_eval.assets_parsed / assets_total`（18/18）随之漂移；
- `docs/gate` 事实数与 `data-gate-report.md` 需要重生成。

按 AGENTS.md「对外数字：单一 owner + 一次性同步」，这属于另一次需要一次性改全的变更。
本轮按约定**只补输入数据、不碰对外数字**，因此文件放在台账目录里但暂不注册；
`test_committed_manifest_is_current` 只校验已注册资产，因此该文件不会被当成资产。

**下一轮把它变成正式资产时需要一次做完的事**：

1. `urban_dataset.py` 增 `UrbanAssetSpec`（含 doc_type / 版本 / 治理字段）；
2. 重新生成 `samples/assets/urban/manifest.json`；
3. 在 `data_gate.py` 增加 `forebay` 类事实并更新 `tests/test_data_gate.py` 的 `FACTS_TOTAL`；
4. 同步 `evidence_check` 的资产数与指纹，按单一 owner 一次改全；
5. 补一条"假设值不得当实测引用"的负例测试。

## 7 待核实清单（补齐后即可从"假设输入"变成"实测输入"）

| 项 | 需要的原始资料 |
| --- | --- |
| 前池平面尺寸与池深 | DF-PS-01 前池竣工图 / 设计图 |
| 池底与池顶绝对高程 | 泵站高程表（注意与城市绝对高程的基准换算） |
| 警戒水位依据 | 泵站运行规程或调度预案原文 |
| 是否有溢流堰/溢流管及其尺寸 | 泵站工艺图（有则可给 `spill_crest_m` / `spill_width_m`） |
| 泵特性曲线 H–Q | 泵组样本（缺则抽排能力只能按额定常值处理） |
