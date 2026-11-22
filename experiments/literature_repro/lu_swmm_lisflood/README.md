# 卢兴超 2024：管网溢流 → 地表漫流单向耦合

对应文献：卢兴超, 徐宗学, 李永坤, 胡小红, 唐清竹, 宋鹏越. 基于SWMM与LISFLOOD-FP耦合模型的城市街区内涝模拟研究[J]. 水资源保护, 2024, 40(3): 98-105. DOI `10.3880/j.issn.1004-6933.2024.03.012`。

## 复现范围

- 复现的是**耦合接口**，不是两个模型本体：一维管网侧的节点溢流过程（m³/s）作为二维地表模型的入流边界，二维模型负责摊开水量并给出淹没深度与淹没面积。
- 没有实现 SWMM 的管网求解器，也没有实现 LISFLOOD-FP 的完整浅水格式；二维侧是一个显式有限体积格式（Manning 面通量 + 供水方限流 + 自由出流边界）。
- 论文里的 NSE（0.76 / 0.68 / 0.71）、淹没面积（0.024→0.345 km²）、10/20/30/50 a 重现期情景都依赖作者的 DEM、管网与实测降雨，本项目没有，因此**不做结果复现**。

## 输入契约

`--source synthetic` 用脚本内置的 toy 地形与溢流过程；`--source json` 读外部 JSON，字段：

```text
source_type       必填，toy 或 synthetic
dem_m             二维数组，单位 m
manning_n         地表糙率，0.005–0.2
dt_s              输出时间步，秒
overflow_m3s      (时间步, 节点数) 数组，单位 m³/s，非负
nodes             [{node_id,row,col}, …]，节点所在网格单元
boundary_stage_m  网格四边自由出流的外水位，默认 0
grid_resolution_m 可选，默认 10
```

校验会直接报错的情形：`source_type` 缺失、DEM 含非有限值、溢流为负、节点落在网格外、形状不匹配。

## 运行

```text
.venv-exp/Scripts/python.exe experiments/literature_repro/lu_swmm_lisflood/run_swmm_lisflood.py
.venv-exp/Scripts/python.exe ... --source json --input my_case.json --output-dir results/my_case
```

只在 `.venv-exp` 里跑，依赖仅 `numpy`；不装 SWMM、不装 LISFLOOD-FP。

## 验收门（脚本自带，失败返回非零）

1. **体积守恒**：`inflow = boundary_outflow + storage`，相对残差必须 < 1e-6，否则退出码 1。
2. **非负水深**：任一步出现负水深立即抛错，说明限流器失效。
3. **可追溯**：`run.json` 记录 `source_type`、`paper_result_claim: false`、命令、输入 sha256、环境版本、方法与证据边界。

## 当前结果（toy 场景，仅作方法自检）

```text
溢流总量 4772.79 m³   边流出流 2453.21 m³   末期蓄水 2319.58 m³
体积残差 -4.4e-11 m³（相对 -9.2e-15）
峰值水深 2.53 m   末期湿面积 0.90 ha
淹没面积 >0.3 m 2500 m²；>1.0 m 900 m²
```

这组数字只能说明"接口连通、质量守住、水会往低处摊"，**不能**与论文任何数值对比。

## 已知局限

- 二维格式是一阶显式，固定子步（每输出步 60 次），没有做时间步自适应，也没有做格式收敛性检验。
- 没有入渗、没有管网与地表的动态反馈（单向耦合的定义如此），也没有建筑物阻挡与地下空间。
- 糙率、地形坡度、节点密度都是自拟的，不对应任何真实片区。
