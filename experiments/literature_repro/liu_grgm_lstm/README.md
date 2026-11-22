# 刘成帅 2023 GRGM-LSTM 方法契约

对应论文为刘成帅等《考虑产流模式空间分布的流域-城市复合系统洪水预报模型》（《水科学进展》2023）。本目录只做独立实验，不导入或修改门户、MCP、`src/`、`samples/eval/` 和主项目配置。

## 复现边界

- 论文方法链是 10 m 网格上的土壤类型、土地利用、坡度叠加，判别超渗、蓄满或混合产流模式，再把网格时段产流量交给 LSTM 汇流预报。
- 论文案例使用 18 场观测洪水、37 个雨量站的 1 h 降雨和中牟站流量标签，并按洪水场次划分训练与验证，比较预见期小于 6 h 和 12 h 的效果。
- 当前仓库没有上述原始网格、雨量站、率定参数和观测标签。`--source synthetic` 只验收输入输出形状、产流守恒和依赖门，结果带有 `source_type=synthetic`，不能作为论文精度、NSE 或观测复现结果。
- 脚本把 10 m 土壤/土地利用/坡度判别后的 `mode_grid` 作为输入，内部仅使用可审计的最小超渗、蓄满和混合算子。该算子用于接口 smoke，不等同于论文率定后的完整 GRGM 参数化。
- 预见期边界按论文表述记录为“预见期小于 6 h”与“12 h”两档，不对第二档额外解释为其他阈值。当前阶段没有时序训练和评估，因此不外推到其他预见期。
- 当前脚本不实现 LSTM。`--stage full` 或 `--stage lstm-gate` 会检查 PyTorch，缺少 `torch` 时返回非零状态并明确报错，不使用 RNN、线性模型或其他替代。

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

`observed_discharge_m3s` 可以作为标签随输入提供，形状必须是 `[events, hours]`。脚本仅检查形状，不以合成标签计算论文指标。

## 运行

在实验工作树根目录执行：

```powershell
python experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --source synthetic --stage grgm
```

该命令不依赖 PyTorch，生成 `experiments/literature_repro/liu_grgm_lstm/results/run.json` 和 `runoff_outputs.npz`。输出记录输入 SHA-256、`source_type`、网格/站点/事件形状和逐步水量守恒残差。

依赖门单独运行：

```powershell
python experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --source synthetic --stage lstm-gate
```

完整阶段会先运行 GRGM，再检查 PyTorch。当前环境未安装 PyTorch 时应明确失败：

```powershell
python experiments/literature_repro/liu_grgm_lstm/run_grgm_lstm.py --source synthetic --stage full
```

## 结果解释

`results/run.json` 中 `paper_result_claim` 固定为 `false`。只有取得论文的 18 场观测洪水、37 站降雨、10 m 空间数据、率定参数和标签后，才有条件另行评估 GRGM 产流误差、LSTM 预见期 NSE 或论文结果复现。
