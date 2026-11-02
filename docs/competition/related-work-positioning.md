# 相关工作定位：本仓库 vs 外部 Agentic AI + 数字孪生 + 审计框架

- 用途：答辩/投稿被问"你们和已发表的那套框架有什么区别"时的标准回答；同时是引用口径。
- 外部对象：Syed T A, Akarma A, Alatify A, Naqash M T, Alqurashi A. *Agentic AI-enhanced digital twins
  for Smart City civil infrastructure: A secure, autonomous and auditable management framework*.
  PLoS One, 2026, 21(7): e0353610. DOI: 10.1371/journal.pone.0353610（CC BY 4.0）；
  其代码与数据另见 Zenodo `10.5281/zenodo.18849091`（v2.1，CC BY 4.0）。
- 原则：**只借方法，不借数字，不宣称同一套实现**。方法（五档消融、同口径指标、锚定元组、
  威胁到有效性自省）已经落进本仓库 P11；效果量一律不引用。

## 1 一张表说清差异

| 维度 | Syed et al. 2026（外部框架） | 本仓库（agent_poc） |
| --- | --- | --- |
| 目标系统 | 桥梁/道路/给排水等民建基础设施的监测→干预 | 城市内涝联合事件 → 跨部门推演 → 资源分配策略受控进化 |
| 架构 | 数字孪生 + 多智能体 PCA（LangChain/LangGraph）+ 许可链审计 + 市政 API | 时态语义层 + 六跳推演 + append-only 哈希链溯源 + MCP/Skill 编排 + 受控进化 |
| 真实执行 | **全部模拟**：无真实传感器、无真实 LLM 调用、无真实链、无真实市政 API（其 §6.7 自述） | **真机跑通**：本机 Nexent v2.7.0，Agent 调 MCP 工具产出与离线推演 8/8 一致（`conversation_id=21`）；离线内核确定性可复现 |
| 审计层 | 许可链（文中实为 SHA-256 hash-commit 日志服务，无链、无共识，其 §5.7 自述） | append-only 哈希链账本 + `H(o),H(Z),H(a*),meta` 决策锚定包 + 链下原文复核 |
| 评测 | 18,000 次合成蒙特卡洛：5 配置 × 3 复杂度 × 30 独立随机种子 | 22 例受控用例 × 5 档消融；固定种子 bootstrap + 配对置换检验（**不伪造独立种子**） |
| 主要归因 | 多智能体编排是延迟/成功率主因；链层只贡献可审计性 | 推演引擎是安全/六跳主因；编排层贡献影响范围与资源覆盖；审计层只贡献可审计性 |
| 可核验性 | 数字由仿真参数生成；代码/数据 CC BY 发布 | 每条结论有命令可当场复现；106 项对外数字冻结，任一漂移即退出码 1 |

两条结论方向一致的地方值得讲：**审计/链层对运行指标没有显著贡献，价值在可追溯**。
这条外部证据正好替本仓库"不上链、保持哈希链审计日志"的设计取舍背书。

## 2 答辩口径（三句话）

1. "我们不是一个数字孪生大屏，是**一条从事件到可追责决策、并能从反馈中受控进化的链**：
   同一个事件 ID 穿过资产→语义→推演→反事实→溯源→进化→编排共 8 层，每层有一个可当场跑出来核对的数字。"
2. "外部同类研究（Syed et al., PLoS One 2026）证明两件事：多智能体编排带来主要收益、
   审计层只买可审计性。我们**采用它的消融方法**，在自己的确定性内核上重做了归因，
   结论是同向的，但数字来自可复现评测而不是合成蒙特卡洛。"
3. "差异在可运行性：他们是纯仿真（无真实模型调用、无真实链、无真实 API），
   我们是真机 Nexent 跑通 + 离线确定性复现，且**不宣称区块链**——我们只有哈希链审计日志。"

## 3 引用（提交物里必须署名）

BibTeX：

```bibtex
@article{syed2026agentic,
  title  = {Agentic AI-enhanced digital twins for Smart City civil infrastructure:
            A secure, autonomous and auditable management framework},
  author = {Syed, Toqeer Ali and Akarma, Ali and Alatify, Ali and Naqash, Muhammad Tayyab and Alqurashi, Abdulaziz},
  journal = {PLoS One}, volume = {21}, number = {7}, pages = {e0353610},
  year = {2026}, doi = {10.1371/journal.pone.0353610}
}
```

GB/T 7714：SYED T A, AKARMA A, ALATIFY A, 等. Agentic AI-enhanced digital twins for smart city
civil infrastructure: a secure, autonomous and auditable management framework[J]. PLoS One, 2026,
21(7): e0353610.

复用其代码/数据时另引 Zenodo：`Syed T A, et al. Agentic AI-Enhanced Digital Twins for Smart City
Civil Infrastructure (v2.1). Zenodo, 2026. DOI: 10.5281/zenodo.18849091`（CC BY 4.0，需署名；
CC BY 对软件无专利授权与担保条款，深度集成前核对仓库 LICENSE）。

## 4 借鉴边界（不能怎么做）

| 不能做 | 原因 |
| --- | --- |
| 引用其效果量（91.9% 延迟降低、$1.37B vs $2.96B 生命周期成本等） | 来自合成仿真参数，不可迁移；引用会变成"拿别人的仿真当自己的实测" |
| 照搬其 Table 1 能力对照表或图 | 版权允许（CC BY）但学术上必须标 "adapted from" 并自己重画 |
| 照抄摘要数字 | 其摘要把"可审计率 71.8%"与"工作负载下降 91.7%"写反了（表 2 与正文是 91.7% / 71.8%），引用请以 Table 2 与 §6.4 为准 |
| 把"许可链"写进自己的实现 | 本仓库只有单机 append-only 哈希链，无共识无多节点；P4 设计已立红线 |
| 用"30 个独立随机种子"描述本仓库评测 | 本仓库内核确定性；P11 是固定用例集上的 bootstrap 重采样，口径不同 |

## 5 方法落地对照（均已实现，可复跑）

| 外部方法 | 本仓库落点 | 证据 |
| --- | --- | --- |
| 五档消融阶梯（§6.1） | `decision_poc.ablation` 的 L1–L5 | `docs/reports/p11-ablation-report.md` |
| 同口径四项指标（§6.2） | 9 项指标 + 分母写清（可执行/六跳/冲突/安全/过期/范围资源/引用/可审计/可发布） | 同上 §3 |
| 运行级统计 + 威胁到有效性自省（§6.1、§6.7） | 固定种子 bootstrap + 配对置换；报告首段写明"不伪造独立种子" | 同上 §4、§5 |
| 锚定元组 `H(o),H(Z),H(a*),meta`、原文链下（§4.6） | `decision_poc.anchoring.AnchorLog`（提交/链校验/链下复核/篡改检出） | `samples/eval/anchor_demo.json`、`tests/test_anchoring.py` |
| 高危动作人工审批门（§4.3） | 审批门禁 + 权限收口 + 转人工原因 | `docs/reports/p6-skills-report.md` |
| 审计拒绝即不可发布（治理约束） | `publishable` 单一开关：审计拒绝/降级一律不可发布 | `docs/reports/p6-skills-report.md` |
