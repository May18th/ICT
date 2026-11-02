# P12 定量层落地报告（水文 / 水力 / 不确定性）

- 日期：2026-11-02
- 对应设计：[P12 多智能体协同 + 场景 v2 设计](../superpowers/specs/2026-11-02-p12-multi-agent-collaboration-and-scenario-v2-design.md) §6.4、T7、T8
- 本阶段范围：**只做内核定量模块，不接六跳、不注册 MCP 工具、不改任何既有推演数字**
- 结论：T7 的水文/水力与 T8 的不确定性内核已落地，39 项新测试全绿；六跳与缺口数字保持原口径，留到 T1/T2/T3 一次性重算

## 1 实现清单

| 模块 | 内容 |
| --- | --- |
| `src/decision_poc/quant/contracts.py` | `QuantResult`（公式/出处/假设/误差界 + 算不出来时显式声明）、`SpreadResult`（低/标称/高）、`uncomputable()` |
| `src/decision_poc/quant/hydrology.py` | 面雨量→雨强换算、推理公式 `Q=ψ·q·F`、一步串联、情景分档 `60/80/100 mm/2h` |
| `src/decision_poc/quant/hydraulics.py` | 曼宁满流能力与流速、积水上涨与封控提前量、堰流倒灌、孔口出流倒灌 |
| `src/decision_poc/quant/uncertainty.py` | 角点枚举区间（确定性）、固定种子 Monte Carlo |

三条硬规则由契约强制：`assumptions` 不得为空；`formula_source` 不得为空；
`computable=False` 时 `value` 必须为 0 且必须给出原因——**不许用近似值冒充结论**。

## 2 公式与出处

| 计算 | 公式 | 出处字段 |
| --- | --- | --- |
| 雨强换算 | 1 mm/h = 2.7778 L/(s·ha) | 量纲换算 |
| 产流 | `Q = ψ·q·F` | 《室外排水设计标准》GB 50014 雨水设计流量（推理公式） |
| 满流能力 | `Q = (1/n)·A·R^(2/3)·i^(1/2)`，圆管 `R = D/4` | GB 50014 曼宁公式 |
| 积水提前量 | `dV/dt = Q_in − Q_out`，`d = V/A` | 水量平衡 |
| 堰流倒灌 | `Q = C·b·h^(3/2)`，C = 1.7 | 宽顶堰流量公式（未按本工程率定） |
| 孔口倒灌 | `Q = Cd·A·√(2gh)`，Cd = 0.60 | 孔口出流公式（未按本工程率定） |
| 区间/抽样 | 角点枚举 / 固定种子均匀抽样 | 明确声明"区间不是置信区间" |

## 3 手算对拍（与 `blender-ue-scene/out/hydraulics-report.md` 一致）

| 项 | 本模块 | 校核报告 | 偏差 |
| --- | --- | --- | --- |
| DN600 满流能力（i=0.30%） | 1210.9 m³/h | 1,211 m³/h | 0.01% |
| DN600 满流流速 | 1.190 m/s | 1.19 m/s | 0.03% |
| DN1200 满流能力（i=0.15%） | 5436.6 m³/h | 5,436 m³/h | 0.01% |
| DN1400 满流能力（i=0.15%） | 8198.5 m³/h | 8,200 m³/h | 0.02% |
| A 区设计流量（ψ=0.7、q=200、22.1 ha） | 3.094 m³/s | 3.09 m³/s | 0.13% |

偏差全部来自报告的四舍五入，公式与口径一致。对拍用例已写进
`tests/test_quant_hydraulics.py` 与 `tests/test_quant_hydrology.py`，公式被改坏会直接红。

## 4 本阶段明确没做

| 项 | 原因 |
| --- | --- |
| 接六跳推演（T1/T2） | 会改变缺口与处置顺序，必须与对外数字重算（T3）同一批做，不能分两次漂移 |
| 图论封控优化 `graphopt.py` | 需要与既有 `road_graph.py` 的连通语义对齐，单独一轮更安全 |
| 最优分配上界 / 可靠性 `optimize.py`、`reliability.py`（T8 剩余） | 依赖 T1 的新缺口口径，先有数才能算上界 |
| MCP `quant` 工具组（T7 工具注册） | 会让 `mcp.tools_count` 从 12 变 13，属于对外数字变更，并入 T5 一次改全 |
| 非恒定流 / 2D 浅水 | 需 EPA SWMM 级模型与率定数据，设计规格已列为"不做" |

## 5 对外数字变化

本阶段新增 39 项测试（600 → 639），自检指纹随之变更；随后协同层再增至 671（见
[协同层报告](p12-collab-layer-report.md)），**当前口径以 `pytest -q` 与 `evidence_check` 实跑为准**。
已按 `AGENTS.md`「单一 owner + 一次性同步」执行：

- `samples/eval/evidence_expectations.json`：`test_baseline.collected`、`checks.test_suite.docs_declared`；
- 三份文档测试数：`README.md`、`docs/competition-gap-audit.md`、`docs/competition/preliminary-submission-draft.md`；
- 全部"106/106 + 指纹"位置：README、`docs/demo-runbook.md`、
  `docs/competition/technical-breadth-and-presentation-boundary.md`、
  `docs/competition/presentation-closure-report.md`、`scripts/build_preliminary_docx.py`、`AGENTS.md`；
- `docs/reports/evidence-check-report.md` 由命令重生成（勿手改）；
- 初赛内容稿 `.docx` 已用 bundled python 重新生成（`scripts/build_preliminary_docx.py`）。

六跳、缺口、冲突、P9 分配等**其余 105 项自检口径未变**。

## 6 验证命令与结果

```powershell
uv run --locked pytest -q                                   # 671 passed（含协同层 32 项）
uv run --locked python -m decision_poc.evidence_check        # 106/106（指纹随测试数变化，当前口径以实跑为准）
uv lock --check                                             # 通过
uv run --locked python -m compileall -q src tests            # 通过
uv pip check --python .venv\Scripts\python.exe              # 通过
```
