# P11 设计：消融阶梯评测 + 决策锚定包（2026-11-02）

- 目的：把"改进前后对比"升级为**逐组件归因**，并补上"一次决策周期打包成可核验锚定元组"的能力。
- 依据：`docs/competition/technical-breadth-and-presentation-boundary.md`（呈现口径）、
  `docs/reports/p8-evaluation-report.md`（现有评测基线）、`samples/eval/urban_cases.json`（22 例）。
- 外部参考（只借方法论，不借数字）：Syed T A, et al. Agentic AI-enhanced digital twins for smart city civil
  infrastructure[J]. PLoS One, 2026, 21(7): e0353610. 其 §6.1 的五档消融、§6.2 的四项指标、§4.6 的锚定元组。

## 0 定位与红线

1. 我们的内核是**确定性**的：同一输入两次运行逐位一致（已由 `evidence_check` 与 pytest 冻结）。
   因此**不伪造"30 个独立随机种子"**。本文的"运行级统计"定义为：在固定 22 例用例集上做
   **固定种子的 bootstrap 重采样 30 次**，给出指标抽样波动量级与配对置换检验 p 值；
   它度量的是"这套用例上差异是否一致"，不是对总体的推断。口径写进报告，不允许含糊成"30 次独立实验"。
2. **不宣称区块链**：锚定包是 append-only 哈希链上的一次提交（`H(o), H(Z), H(a*), meta`），
   原始数据链下保存；文案沿用"哈希链审计日志"，不写"上链/不可篡改存储"（见 P4 设计 §349–351 红线）。
3. 合成数据边界不变：`urban_cases.json` 是受控评测环境，动作层是模拟工具，没有真实市政副作用。

## 1 五档消融阶梯（对应论文 Rules→DT→单 Agent→多 Agent→全量）

| 档 | 名称 | 能力子集 | 实现 |
| --- | --- | --- | --- |
| L1 | `rules_only` | 原始工单 + 预案阈值直出动作，无语义层/推演/审计 | `ablation.rules_only_actions()` 读 `scene.json` 事件属性与工单描述正则 |
| L2 | `semantic_only` | 感知：事件理解 + 影响范围 | `SkillPipeline(stages=("understand","impact"))` |
| L3 | `single_skill` | 单个推演技能包办六跳，无影响范围/资源/审计 | `stages=("understand","deduce")` |
| L4 | `multi_skill` | 四技能顺序编排，审计门禁关闭 | `stages=("understand","impact","resource","deduce")` |
| L5 | `full_audited` | 五技能 + 审计门禁 + 引用完整率 | 现有 `SkillPipeline()` 默认全量 |

`SkillPipeline.__init__` 新增可选 `stages` 参数（默认全量，向后兼容）；`run()` 行为不变。
**允许某个技能缺席**，`_settle()` 已按"无审计即不可发布"处理，不需要第二套发布逻辑。

## 2 指标（每个配置同口径算，分母写清）

| 指标 | 定义 | 分母 |
| --- | --- | --- |
| `deliverable_rate` | 给出可执行处置动作（`action_order` 非空） | 全部 22 例 |
| `six_hop_rate` | 六跳齐全（`observed…action`） | 进入推演的用例（L1 记 0） |
| `conflict_recall` | 期望冲突类型被识别出的比例 | 声明了期望冲突类型的 6 例 |
| `safety_rate` | 证据不足/越权用例被正确拒答或拒绝的比例 | 该 8 例 |
| `expiry_filter_rate` | 过期用例里不再暴露已失效预警的比例 | `expired` 类 4 例 |
| `impact_resource_rate` | 结论同时带影响范围与资源核算的比例 | 全部 22 例 |
| `citation_completeness` | 审计引用完整率均值 | 全部 22 例（无审计记 0） |
| `auditable_rate` | 决策带可核验依据（审计存在且 `rejected==0`）的比例 | 全部 22 例 |
| `publishable_rate` | 审计通过、无降级、无需补证据即可发布的比例（高影响动作仍需人工审批，这是设计） | 全部 22 例 |

`conflict_recall` 与 `safety_rate` 分母小（6/8），报告里同时给分子分母，不给百分比以外的推断。

## 3 运行级统计

- 30 次 bootstrap（有放回抽 22 例，种子固定 `20261102`）：每配置每指标给 mean±SD、95% 分位 CI。
- 相邻档位配对置换检验：22 例的逐例 0/1 结果做符号翻转置换（10,000 次，固定种子），给 p 值。
- 报告必须写明：这是**用例集内的重采样**，不是独立重复实验；p 值只说明"该用例集上差异的一致性"。

## 4 决策锚定包

论文 §4.6：`L_t = (H(o_t), H(Z_t), H(a*_t), meta_t)`，原文链下、链上只存承诺。我们的映射：

| 论文 | 本仓库 |
| --- | --- |
| `o_t` 结构化观测 | `PipelineReport` 的 `event_id/as_of/snapshot_hash/facts` 规范化 JSON |
| `Z_t` 证据包 | `report.audit.claims`（claim_id/text/kind/evidence_refs） |
| `a*_t` 选定动作 | `report.deduction.action_order` + `publishable` + `handoff_reasons` |
| `meta_t` 元数据 | `knowledge_version`、`status`、`decision_id`（**不含墙钟时间**，保持确定性） |

实现：`anchoring.AnchorLog`
- `commit(report)` → `AnchorRecord(anchor_hash, upstream_anchor_hash, 三个哈希, metadata)`，链式追加；
- `verify()` → 重算链，报首个断点（与 `ProvenanceLedger.verify()` 同风格，不抛异常）；
- `verify_offchain()` → 用链下保存的原始包重算三个哈希，与锚定记录对不上即失败（证明链上承诺可被复核）；
- `save(dir)` / `load(dir)`：`anchors.jsonl` + `offchain/<anchor_id>.json`。
- 不写墙钟时间、不写随机数；同一次决策重复提交得到逐位相同的哈希（测试冻结）。

## 5 实测结果（2026-11-02）

| 指标 | L1 规则 | L2 语义层 | L3 单推演 | L4 多技能 | L5 全量+审计 |
| --- | --- | --- | --- | --- | --- |
| 六跳完整率 | 0% | 0% | 63.64% | 63.64% | 63.64% |
| 安全处理率 | 0%（0/8） | 12.50%（1/8） | 100%（8/8） | 100%（8/8） | 100%（8/8） |
| 过期过滤率 | 0% | 100%（4/4） | 100% | 100% | 100% |
| 范围与资源覆盖率 | 0% | 0% | 0% | 63.64% | 63.64% |
| 引用完整率 | 0% | 0% | 0% | 0% | 63.64% |
| 可审计率 | 0% | 0% | 0% | 0% | 63.64% |

三条实测归因（只在本用例集上成立）：

1. **推演引擎是安全与六跳的主因**：L2→L3 一步把安全处理率从 12.5% 拉到 100%；
2. **编排层贡献的是覆盖度**：L3→L4 只增加影响范围与资源核算（0%→63.64%），六跳与安全不变；
3. **审计层只贡献可审计性**：L4→L5 只动引用完整率与可审计率，六跳、安全、范围三项逐位不变。

第 3 条与外部研究"审计/链层只贡献可审计性、对性能无显著影响"的结论一致，但本仓库的
数字来自可复现的确定性评测，不是合成蒙特卡洛。

## 6 验收

1. `python -m decision_poc.ablation --report docs/reports/p11-ablation-report.md --json samples/eval/ablation.json`
   输出五档对比表 + 运行级统计；两次运行逐位一致。
2. `python -m decision_poc.anchoring --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --out artifacts/ledger`
   提交并打印 `verify=True`；篡改链下动作包后 `verify_offchain` 报 `OFFCHAIN_MISMATCH`。
3. 新增数字进 `samples/eval/evidence_expectations.json` 的 `p11_ablation` / `p12_anchoring`，由
   `evidence_check` 冻结；测试数、`97/97`、期望指纹同步更新到 README 与对外文档。
4. 现有 600 项测试不回归，`evidence_check` 全绿。
