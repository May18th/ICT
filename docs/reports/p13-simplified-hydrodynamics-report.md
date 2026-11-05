# P13 简化水动力演进落地报告（马斯京根汇流 + 修正 Puls 调蓄）

- 日期：2026-11-05
- 对应设计：[P13 简化水动力演进设计](../superpowers/specs/2026-11-05-p13-simplified-hydrodynamics-design.md)
- 本阶段范围：**只做内核定量模块 + 测试 + 文档**，不接六跳、不注册 MCP 工具、不改任何既有推演数字
- 结论：P13 的两种简化演进方法与两个过程线查询已落地，27 项新测试全绿；对外数字只动"测试数 + 期望指纹"，
  106 项自检项数不变

## 1 实现清单

| 模块 | 内容 |
| --- | --- |
| `src/decision_poc/hydro_contracts.py`（由 `quant/contracts.py` 兼容重导出） | 新增 `HydrographResult`（过程线 + 峰值/峰现时刻 + 公式/出处/假设/误差界 + 算不出来时显式声明）、`uncomputable_hydrograph()` |
| `src/decision_poc/quant/hydrodynamics.py` | 马斯京根系数与稳定性判定、马斯京根汇流演进、修正 Puls 调蓄演进（含抽排与溢流堰）、到阈时刻、超阈时长 |
| `src/decision_poc/quant/__init__.py` | 导出新契约与 6 个新函数 |

新增契约延续三条硬规则：`assumptions` 不得为空；`formula_source` 不得为空；
`computable=False` 时**不得给过程线**且必须说明原因——不截断、不外推、不拿近似序列冒充结果。

## 2 公式与出处

| 计算 | 公式 | 出处字段 |
| --- | --- | --- |
| 马斯京根系数 | `C₀=(Δt/2−KX)/D`、`C₁=(Δt/2+KX)/D`、`C₂=(K(1−X)−Δt/2)/D`、`D=K(1−X)+Δt/2` | 工程水文学 / USACE HEC-HMS 通用方法 |
| 汇流演进 | `O₂ = C₀·I₂ + C₁·I₁ + C₂·O₁` | 同上；K、X 未按本工程率定 |
| 稳定性判定 | `2KX ≤ Δt ≤ 2K(1−X)`，不满足即 `uncomputable` | 负系数会造出不物理的负出流 |
| 调蓄演进 | `S(h₂)/Δt + O(h₂)/2 = S(h₁)/Δt − O(h₁)/2 + (I₁+I₂)/2` | 修正 Puls 法（storage-indication） |
| 溢流 | `Q = C·b·max(0, h−h_堰顶)^(3/2)`，C = 1.7 | 宽顶堰，沿用 `hydraulics.py` 的经验值（未率定） |
| 过程线查询 | 相邻时段线性插值求上穿/下穿时刻 | 离散误差 ≤ 一个计算时段 Δt |

**边界（写进每个结果的 `assumptions`/`error_bound`）**：

- 不是圣维南非恒定流完整方程，没有壅水/回水计算，不适用于强回水、超临界流与漫滩；
- 不含 2D 浅水与 1D/2D 耦合，不做地表漫流推演；
- K、X 与库容曲线都不是本工程实测/率定值——**答辩里不许把本层输出说成 SWMM 级推演**。

## 3 手算对拍（全部在 `tests/test_quant_hydrodynamics.py`）

| 项 | 输入 | 手算值 | 模块输出 | 偏差 |
| --- | --- | --- | --- | --- |
| 马斯京根系数 | K=2 h、X=0.2、Δt=2 h | C₀=C₂=0.230769、C₁=0.538462 | 同左 | 0 |
| 两步演进 | 入流 0 → 1000 → 1000 m³/h | O₂=230.769、O₃=822.485 | 同左 | 0 |
| 峰值削减 | 三角入流峰值 1500 m³/h | 出流峰值 1027.663（削减 31.49%） | 同左 | 0 |
| 峰现滞后 | 同上（K=2 h、Δt=1 h） | 峰值 3 h → 4 h，滞后 1 h | 同左 | 0 |
| 线性调蓄 | 1000 m²、净 4000 m³/h、Δt=0.1 h | 每步 +0.4 m（0/0.4/0.8/1.2） | 同左 | <1e-7 |
| 停机调蓄 | 同上、抽排 0 | 每步 +0.5 m（0/0.5/1.0/1.5） | 同左 | <1e-7 |
| 溢流稳态 | 来水 20000、抽排 1000、堰顶 1 m、堰宽 5 m | 稳态 `O(h)=I` → h=1.72780 m | 1.7278±0.01 | ≤0.01 |
| 到阈时刻 | 线性调蓄、目标 1.0 m | 0.25 h（0.8@0.2 h、1.2@0.3 h 插值） | 0.25 h | 0 |
| 超阈时长 | 构造过程线 0.2/0.6/1.1/0.9/0.4 | 0.18 h 上穿、0.25 h 下穿 → 0.07 h | 0.07 h | 0 |

叠加上限/下界、稳定性、序列长度、负入流、目标未达到等边界用例，共 27 项。

## 4 与"泵站停机后水位过程"缺口的关系

`blender-ue-scene/docs/vd02-空间论证.md` §3 把"A 区泵站停机后水位过程"标为"水动力计算（本场景明确不做）"。
本层落地后，它变成一条**可算但有前提**的链路：

```

前池库容曲线（水位-容积表）+ 来水过程线 + 抽排能力（含停机序列）
  → level_pool_route → 水位过程线
  → time_to_level       → 到警戒水位的时刻（封控/调度的提前量）
  → level_exceedance_duration → 超警持续时长
```

前提是**前池容积表台账里没有**：要么现场量取，要么按明确标注的假设值算并写进 `assumptions`。
本层不替台账补数，也不能把假设值算出来的时间说成实测结论。

## 5 本阶段明确没做

| 项 | 原因 |
| --- | --- |
| 接六跳推演 | 会改变缺口与处置顺序，必须与对外数字重算同一批做 |
| MCP `quant` 工具组新增工具 | 会改 `mcp.tools_count` 对外数字，需单独一轮 |
| 圣维南非恒定流 / 2D 浅水 | 需 SWMM 级模型与率定数据，边界不变（见设计 §2） |
| 泵特性曲线 H–Q、库容曲线自动生成 | 台账缺数据；本层只接受调用方给定曲线 |

## 6 对外数字变化

| 口径 | 变更前 | 变更后 |
| --- | --- | --- |
| 测试总数 | 776 | **803**（新增 27 项） |
| 对外数字自检 | 106/106（指纹 `3e01b8b1d25523d5`） | **106/106（指纹 `a60477b1f24dcfa9`）**，项数不变 |

按 `AGENTS.md`「单一 owner + 一次性同步」执行：

- `samples/eval/evidence_expectations.json`：`test_baseline.collected`、`checks.test_suite.docs_declared`；
- 三份文档测试数：`README.md`、`docs/competition-gap-audit.md`、`docs/competition/preliminary-submission-draft.md`；
- 其余对外文档与脚本里"803 测试 / 新指纹"同步更新（含 `scripts/reproduce.py` 说明与
  `scripts/build_answer_ppt.py`、`scripts/build_preliminary_docx.py` 文案）；
- `docs/reports/evidence-check-report.md` 与 `samples/eval/evidence_check.json` 由命令重生成（勿手改）。

## 7 验证命令与结果

```powershell
uv run --locked pytest -q                                  # 803 passed
uv run --locked python -m decision_poc.evidence_check       # 106/106（指纹 a60477b1f24dcfa9）
uv lock --check                                            # 通过
uv run --locked python -m compileall -q src tests           # 通过
uv pip check --python .venv\Scripts\python.exe              # 通过
```

## 8 后续接入说明

P13 报告记录的是内核落地时的范围；随后 P14 已将可选 `HydrographResult` 接入六跳、Skills、
`collab_run` 与三维回放。P14 只补充过程线事实状态，仍不重算“需 8 台、可用 6 台、缺口 2 台”等
资源缺口 v1 口径，完整二维浅水求解与真实场景率定仍不在范围内。
