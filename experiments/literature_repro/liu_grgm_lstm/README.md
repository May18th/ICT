# 刘成帅 2023 GRGM-LSTM 方法契约

对应论文为刘成帅等《考虑产流模式空间分布的流域-城市复合系统洪水预报模型》（《水科学进展》2023）。本目录只做独立实验，不导入或修改门户、MCP、`src/`、`samples/eval/` 和主项目配置。

## 复现边界

- 论文方法链是 10 m 网格上的土壤类型、土地利用、坡度叠加，判别超渗、蓄满或混合产流模式，再把网格时段产流量交给 LSTM 汇流预报。
- 论文案例使用 18 场观测洪水、37 个雨量站的 1 h 降雨和中牟站流量标签，并按洪水场次划分训练与验证，比较预见期小于 6 h 和 12 h 的效果。
- 当前仓库没有上述原始网格、雨量站、率定参数和观测标签。`--source synthetic` 只验收输入输出形状、产流守恒和依赖门，结果带有 `source_type=synthetic`，不能作为论文精度、NSE 或观测复现结果。
- 脚本把 10 m 土壤/土地利用/坡度判别后的 `mode_grid` 作为输入，内部仅使用可审计的最小超渗、蓄满和混合算子。该算子用于接口 smoke，不等同于论文率定后的完整 GRGM 参数化。
- 预见期边界按论文表述记录为“预见期小于 6 h”与“12 h”两档，不对第二档额外解释为其他阈值。当前阶段没有时序训练和评估，因此不外推到其他预见期。
- LSTM 汇流阶段已实现：GRGM 的网格产流序列作为输入特征，输出逐时流量。缺少 `torch` 时 `--stage full` 与 `--stage lstm-gate` 返回退出码 2 并明确报错，不使用 RNN、线性模型或其他替代。
- **关键限定**：目标量要么来自输入自带的 `observed_discharge_m3s`（合成 fixture 里是面雨量的线性换算，`target_source=synthetic`），要么来自 GRGM 自算流量（`task=closed_loop_self_consistency`）。**两者都不是实测**，NSE 接近 1 只说明接口与训练流程可用，不能当作洪水预报能力。

## 最小输入契约

纯 NumPy GRGM 入口接受 JSON（`--source json --input <path>`），字段如下。

| 字段 | 形状/要求 |
| --- | --- |
| `source_type` | 必须显式填写，原始数据只能填写 `paper` 或其他可追溯来源；合成数据填写 `synthetic` |
| `grid_resolution_m` | 10 |
| `rainfall_mm` | `[events, hours, 37]`，37 站、1 h 间隔、非负有限值 |
| `station_xy` | `[37, 2]`，与降雨站顺序一致 |
| `mode_grid` | `[rows, cols]`，0=超渗、1=蓄满、2=混合 |
| `parameters` | `f0_mm_h`、`fc_mm_h`、`decay_h_inv`、`storage_capacity_mm`，均为非负有限值 |

`observed_discharge_m3s` 可以随输入提供（形状 `[events, hours]`）。**提供时 LSTM 以它为监督目标**，结果里 `task` 记为 `observed_discharge`；未提供时退回 GRGM 自算流量，`task` 记为 `closed_loop_self_consistency`。两种情况下 `paper_result_claim` 都是 `false`。

## 运行

在实验工作树根目录执行：

```powershell
python experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --source synthetic --stage grgm
```

该命令不依赖 PyTorch，生成 `experiments/literature_repro/liu_grgm_lstm/results/run.json` 和 `runoff_outputs.npz`。输出记录输入 SHA-256、`source_type`、网格/站点/事件形状和逐步水量守恒残差。

依赖门单独运行：

```powershell
.venv-exp/Scripts/python.exe experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --stage lstm-gate
```

完整阶段（GRGM → LSTM 训练与留出评估）需要 `.venv-exp` 里的 torch：

```powershell
.venv-exp/Scripts/python.exe experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --stage full
```

分场次留出是硬约束：最后 4 场作为测试集、前 14 场训练，按事件切分而不是按时间点切分，避免同一场洪水泄漏到两侧。缺 torch 的环境跑 `--stage full` 返回退出码 2，并把 `lstm_dependency` 标成 `blocked` 写进 `run.json`。

## 当前结果（合成输入）

```text
留出事件 14–17（前 14 场训练）  torch 2.15.0+cpu  单线程  seed 42
task=observed_discharge  target_source=synthetic
NSE train 0.9995    NSE test 0.9981    RMSE test 0.0009 m³/s
均值基线 NSE test -0.0201（说明指标本身没算错）
隐藏层 16 / 400 epoch / Adam lr 0.01
```

两点必须同时读：

1. 目标量不是实测。fixture 自带的 `observed_discharge_m3s` 是"面雨量 × 0.0015"的合成序列（注释里已写明不能拿来宣称 LSTM 精度），所以 `target_source` 标 `synthetic`。
2. NSE 接近 1 是**预期结果而不是成果**。输入是 GRGM 网格产流、目标由同一场降雨换算而来，信息几乎全在输入里，这个映射接近恒等。

它能证明的三件事：产流字段能喂进时序模型、按事件留出没有泄漏、训练可复现（同机同码两次运行指标逐位相同）。它不能证明任何预报能力。

若换成 `--source json` 且输入不带 `observed_discharge_m3s`，脚本退回 GRGM 自算流量当目标，`task` 会标成 `closed_loop_self_consistency`，`target_source=grgm`。

## 结果解释

`results/run.json` 中 `paper_result_claim` 固定为 `false`。只有取得论文的 18 场观测洪水、37 站降雨、10 m 空间数据、率定参数和标签后，才有条件另行评估 GRGM 产流误差、LSTM 预见期 NSE 或论文结果复现。
