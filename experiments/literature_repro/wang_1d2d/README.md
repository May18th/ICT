# 王小杰 2024：1D-2D 双向耦合与三种水流交换模式

对应文献：王小杰, 夏军强, 李启杰, 侯精明. 考虑不同水流交换模式的城市洪涝一维二维双向耦合模型[J]. 水科学进展, 2024, 35(2): 244-255. DOI `10.14042/j.cnki.32.1309.2024.02.006`。

## 复现范围

- 复现的是**双向耦合的接口与三种交换模式**：一维管段按容积路由，二维地表按有限体积漫流，两者通过检查井 / 雨水口按孔口流量公式双向交换，并在每个子步内同步求解。
- 三种模式与原文同名：`manhole`（检查井法）、`inlet`（雨水口法）、`inlet_manhole`（雨水口-检查井法）；另有 `none` 作为无排水系统基线。
- 没有实现 SWMM 完整管网与二维浅水方程，也没有 Glasgow 街区的几何、糙率与观测。论文给出的削减比例（9.3% / 23.2% / 24.5%，以及重度积水 43.6% / 79.9% / 80.9%）**不复现**。

## 输入契约

`--source synthetic` 用内置 toy 地形 + 合成降雨 + 单条主干管；`--source json` 读外部 JSON：

```text
source_type        必填，toy 或 synthetic
dem_m              二维数组，m
manning_surface    地表糙率
manning_pipe       管道糙率
dt_s               输出时间步，秒
rain_mm            一维数组，每步降雨量（mm）
pipe               {cells[], area_m2, length_m, invert_m[], initial_level_m,
                    outfall_index, outfall_stage_m}
manholes           [{unit_id,row,col,pipe_index,orifice_m2,crest_m}]
inlets             同 manholes
grid_resolution_m  可选，默认 10
```

## 运行

```text
.venv-exp/Scripts/python.exe experiments/literature_repro/wang_1d2d/run_1d2d_coupling.py
.venv-exp/Scripts/python.exe ... --modes none,manhole,inlet,inlet_manhole
```

## 验收门（脚本自带，失败返回非零）

1. **交换步守恒**：任一交换调用前后，地表 + 管网总水量变化必须 < 1e-9 m³，否则抛错。
2. **全模式守恒**：`rain = surface + pipe + outfall`，相对残差必须 < 1e-9，否则退出码 1。
3. **满管溢流**：管内水位不得超过管径，超出部分从检查井冒回地表（`pipe_full_spill_m3` 记账），保证不会靠钳位凭空造水。
4. **可追溯**：`run.json` 记录模式、命令、输入 sha256、环境、逐模式守恒与对比、证据边界。

## 当前结果（toy 场景，四模式同场对比）

```text
模式            峰值水深   水深削减    淹没面积(>0.3m)  面积削减
none            0.770 m       —          600 m²          —
manhole         0.591 m    23.26%        300 m²        50%
inlet           0.650 m    15.55%        600 m²         0%
inlet_manhole   0.549 m    28.66%        300 m²        50%
```

方向性结论（可作为方法自检）：**排水系统接入后峰值水深单调下降，交换单元越多下降越多**。但三种模式之间的具体排序取决于本 toy 的布点（8 个检查井 × 0.5 m² vs 3 个雨水口 × 0.2 m²），**不能**当成论文的交换模式优劣结论。

## 已知局限

- 淹没面积按整格计数，最小步长一格（100 m²），面积百分比是粗粒度；判断方向请看峰值水深。
- 一维侧是容积路由 + Manning 输运，不是 SWMM 的圣维南求解；管道没有坡度分段、没有泵站与闸门。
- 交换用固定孔口系数 0.62，未做系数敏感性分析；每个子步做两次交换迭代，仍未完全消除显式滞后。
