# 文献方法复现：四条路线的结论与边界

- 日期：2026-11-22
- 分支：`exp/literature-tech-repro`（`d7f177d` → `ff36f8e` → `2a8890d`），实验代码在 `experiments/literature_repro/`
- 对应卡片：[P1-1 水动力耦合](../references/paper-p1-1-hydrodynamic-coupling.md)、[P1-2 代理模型](../references/paper-p1-2-surrogate-prediction.md)
- 主分支运行时、MCP、门户、19 项业务台账、六跳链、分配规则、对外数字均未改动

## 结论先行

四条路线的方法链**全部可运行，且各自带守恒/门禁自检**；四条的结果复现**全部未完成**，原因统一：论文作者提供的原始数据拿不到（管网 `.inp`、DEM、PCSWMM 情景、18 场观测洪水、街区几何）。

这属于**能力局限**，不是待办清单——没有实测数据时，任何"复现论文精度"的说法都站不住。

## 四条路线到什么程度

| 路线 | 复现到什么程度 | 关键数字 | 缺什么 |
| --- | --- | --- | --- |
| Xu 2023 LightGBM | 49 情景 × 10 点 × 11 变量的接口、按情景切分、10 折 | 合成基线 NSE 0.866 / MAPE 6.55% | PCSWMM 情景、10 点水深标签、作者的数据切分 |
| 刘成帅 2023 GRGM-LSTM | GRGM 产流 + LSTM 汇流全链；按事件留出最后 4 场测试 | 产流守恒残差 1.8e-12 m³；汇流 NSE test 0.998、RMSE 0.0009 m³/s | 18 场实测洪水与标签、10 m 土壤/土地利用/坡度 |
| 卢兴超 2024 SWMM–LISFLOOD-FP | 溢流→地表单向耦合适配器、显式有限体积、自由出流 | 体积相对残差 −9e-15；峰值水深 2.53 m | SWMM 管网、DEM、糙率、实测降雨 |
| 王小杰 2024 1D–2D 双向 | 管段路由 + 地表漫流 + 孔口双向交换，四种交换模式同场对比 | 各模式体积相对残差约 1e-15；峰值水深单调下降 23.3% → 28.7% | Glasgow 街区几何、观测、三种交换模式的边界条件 |

## 三个必须守住的表述边界

1. **汇流 NSE 0.998 不能对外说成"复现了 GRGM-LSTM 精度"**。目标量是 fixture 里"面雨量 × 0.0015"的合成序列，输入已含全部信息，这个映射接近恒等；结果文件里 `target_source=synthetic`、`paper_result_claim=false`。
2. **单向耦合残差到 −9e-15 只说明质量守恒**，不说明淹没范围算得准。没有实测淹没范围，就没有精度。
3. **1D-2D 的削减比例是本 toy 布点的性质**（8 个检查井 × 0.5 m²、3 个雨水口 × 0.2 m²、面积按整格计数），与论文的 9.3% / 23.2% / 24.5% 不可比，只能用来看方向。

## 对项目口径的影响

- 实验结论只留在实验分支与 `docs/reports/`，**不进 `samples/`、不进 19 项台账、不改对外数字**；两张卡片仍只作方法出处，不作事实来源。
- 被问"你们做过水动力验证吗"，口径是：**方法链路做过可运行验证，未做率定与精度验证，缺实测数据**。不写"已复现""已接入"。

## 工程副作用（踩过的坑，后续别再犯）

1. **面通量用 `min(η)` 作驱动**：干邻居永远算不出水头，水全堆在注入格里——第一版跑出 15.75 m 的假水深、湿面积只有 6 格。正确写法是堰流形式 `max(η) − max(z)`。
2. **限流按单面给**：一段管子同时向上、下游放水，各按全额限流，水位被钳到 0 时凭空造水，残差达 49 倍。必须按管段**总出流**统一缩放，并补"满管溢流从检查井冒回地表"。
3. **`lightgbm` 的 sklearn 接口必须有 `scikit-learn`**，只装前者会在构造 `LGBMRegressor` 时抛 `LightGBMError`。

## 复现入口

```text
工作树：agent_poc/.worktrees/literature-tech-repro
环境：  uv venv .venv-exp --python 3.12
        uv pip install --python .venv-exp/Scripts/python.exe numpy lightgbm scikit-learn
        uv pip install --python .venv-exp/Scripts/python.exe torch --index-url https://download.pytorch.org/whl/cpu
命令：  .venv-exp/Scripts/python.exe experiments/literature_repro/<route>/run_*.py
```

每份 `results/run.json` 都带 `source_type`、`paper_result_claim`、生成命令、输入 sha256、环境版本与证据边界；缺依赖时返回退出码 2，不降级成别的模型。
