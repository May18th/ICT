# 文档导航

## 当前实现

- [技术基线与风险](technical-baseline.md)：固定通用场景、测量项和已知限制。
- [真实语言模型可选接入设计](real-model-provider-design.md)：默认 `TestModel` 离线不变；显式配置后走 DeepSeek 官方 provider，含配置、失败语义、密钥安全与验收口径。

## 设计与计划

- [P0 接口冻结与骨架基线](p0-interface-freeze.md)：八层落点、id/错误码/信封/字段映射，冻结后变更需评审。
- [P2 任务单：认知加工层](tasks/p2-cognition-task-order.md)：适配器、切片、表格标准化、区域标注、抽取基线。
- [P2 回传报告](reports/p2-cognition-report.md)：解析与抽取统计、三类抽查样例、硬断言验证结果。
- [P3 回传报告](reports/p3-evolution-report.md)：提案→确认→发布→回滚闭环、三个变更场景实测、安全默认与回滚哈希一致性。
- [P4 回传报告](reports/p4-graph-provenance-report.md)：时态语义层、引用闸门、哈希链篡改定位、跨版本血缘（locator 键）。
- [P5 回传报告](reports/p5-mcp-report.md)：7 个 MCP 工具契约、stdio 协议实现、证据接口的权限收口、trace_id 与调用日志。
- [P6 回传报告](reports/p6-deduction-report.md)：六跳推演、规则即数据（来源行校验）、三类冲突、资源缺口算式、结论落账。
- [P6 Skill 与编排回传](reports/p6-skills-report.md)：五个 Skill 的契约与降级策略、审计闸门、Nexent 绑定表、与 MCP 路径的一致性校验。
- [P6 反事实回传](reports/p6-counterfactual-report.md)：CF1–CF3 的干预-重放-取差异机制、算不出来的部分如何声明、第 8 个 MCP 工具。
- [P8 评测报告](reports/p8-evaluation-report.md)：22 个用例的五类形态、七项指标口径、失败案例与改进前后对比（由 `decision_poc.urban_eval` 生成，勿手改）。
- [P8 之上的真实模型 A/B 报告](reports/p8-llm-ab-report.md)：同一批 22 个用例上的 rule / TestModel 地板 / DeepSeek 三方对照，含"模型不能自己推断优先级"的实测结论（由 `scripts/llm_ab_eval.py` 生成，勿手改）。
- [赛题 3 差距审计](competition-gap-audit.md)：官方五考点/提交物/赛程 vs 现状证据，逐条标出缺口与优先级（P0 的 Nexent 主链路已补齐，见真机报告）。
- [城市内涝主链路 Nexent 真机报告](reports/nexent-live-integration-report.md)：MCP-over-SSE 注册、Agent 发布、**8/8 一致性（conversation_id=78）**、平台工具调用日志、排障与边界。
- [Nexent 集成设计（SSE + SKILL.md）](nexent-urban-integration-design.md)：为什么必须走 SSE、装配步骤、证据清单与验收口径。
- [Nexent 侧装配与复现手册](../deploy/nexent/README.md)：容器构建、平台装配脚本、真机校验与证据抓取；含 **SKILL.md 上传平台技能仓库（§2.1）** 与 **平台界面截图/录屏取证（§4.1）**。
- [平台侧证据报告](reports/platform-evidence-report.md)：SKILL.md 上传平台技能仓库（skill_id=2/3、幂等、正文逐位一致）+ 平台界面三张截图断言取证，以及这两项如何进对外数字自检；含权限/凭据边界与"技能未绑定 Agent"的诚实口径。
- [真实用户评分采集](real-feedback-channel.md)：真实用户评分（进化前后各一次）的采集/汇总/导出，以及「只汇总不进门禁」与「带修正建议才进门禁」两条通道的口径。
- [图像语义抽取](image-semantics.md)：PNG 像素级抽取（频谱峰值/示意色块/积水范围），输出 RegionSpec 区域候选；明确不做 OCR、不用视觉模型。
- [P9 进化闭环报告](reports/p9-policy-evolution-report.md)：分配策略的逐轮偏差归因、评测门禁采纳/拒绝、留出集泛化、分层记忆 warm start、人工评审第二信号、语义化版本标签与回滚验证（由 `decision_poc.policy_evolution` 生成，勿手改）。
- [P9 决策进化闭环设计](superpowers/specs/2026-10-30-p9-decision-evolution-design.md)：为什么选资源分配、决策输入/策略/偏差口径、进化循环、分层记忆与人工反馈（第 11–14 节）、浏览器对比面板（第 15 节）与验收标准。
- [P11 消融阶梯报告](reports/p11-ablation-report.md)：五档配置 × 22 用例的同口径指标、leave-one-out（逐例剔除）稳健性与精确符号翻转检验（2²² 全枚举、零随机）；首段写清"不引入任何随机数、不伪造独立随机种子"的口径（由 `decision_poc.ablation` 生成，勿手改）。
- [P11 消融与锚定设计](superpowers/specs/2026-11-02-p11-ablation-and-anchoring-design.md)：五档能力子集、指标分母与确定性统计口径（leave-one-out + 精确符号翻转），以及 `H(o),H(Z),H(a*),meta` 锚定包与链下原文复核边界。
- [P12 多智能体协同 + 场景 v2 设计](superpowers/specs/2026-11-02-p12-multi-agent-collaboration-and-scenario-v2-design.md)：1 总控 + 5 专业 Agent 的分层架构（多 Agent 管协同、单内核管断言）、三个现实问题→三项能力的映射、地下通道倒灌入六跳、泵站负荷缺口公式与对外数字重算计划。
- [P12 定量层落地报告](reports/p12-quant-layer-report.md)：水文/水力/不确定性内核的公式-出处对照、与稳态校核报告的手算对拍、本阶段未做项与测试数口径变更说明。
- [P12 多智能体协同层落地报告](reports/p12-collab-layer-report.md)：1 总控 + 5 专业部门的动作卡契约、联合发布与处置顺序裁决、内核指纹稳定性实测，以及三条"多 Agent 不可自由发挥"边界的强制点。
- [P12 场景 v2 口径标定报告](reports/p12-scenario-v2-calibration-report.md)：下穿隧道倒灌判定与堰流进水量、泵站负荷分档，以及"v2 泵站口径会让缺口 1→0 并摧毁 C3/P9"的实测证据与两条处置方案。
- [P13 简化水动力演进设计](superpowers/specs/2026-11-05-p13-simplified-hydrodynamics-design.md)：马斯京根汇流 + 修正 Puls 调蓄的公式、稳定条件与"不是圣维南"的边界，以及过程线级分析要满足的契约。
- [P13 简化水动力演进落地报告](reports/p13-simplified-hydrodynamics-report.md)：两种演进方法与两个过程线查询的手算对拍、"泵站停机后水位过程"缺口的可算链路、未做项与测试数口径变更。
- [泵站前池库容曲线（假设值）数据说明](hydro-forebay-assumptions.md)：`samples/assets/urban/pump_forebay_curves.csv` 的列定义、假设链条、演示算例与敏感性；已注册为 `forebay_curve` 资产，来源级别由该文件的 `value_basis` 列经 loader 驱动到 `level_pool_route`。
- [相关工作定位表](competition/related-work-positioning.md)：与已发表的 Agentic AI + 数字孪生 + 审计框架逐维度对照（真实执行 / 审计层 / 评测口径 / 可核验性）、三条答辩口径、引用格式与"不能怎么做"边界。
- [对外数字自检报告](reports/evidence-check-report.md)：106 项对外宣称数字的期望/实际逐项对照（含漂移检测，由 `decision_poc.evidence_check` 生成，勿手改）。
- [P10 可视化工具层设计](p10-visualization-tools-design.md)：决策快照 → UE 渲染作业 → 引擎 ack 的异步闭环、3 个 MCP 工具（`scene_render`/`scene_compare`/`asset_lookup`）、工具分组 allowlist 与演示时序；说明为什么不宣称做成实时闭环、Blender 为什么只在离线跑。
- [实体重投影与绑定表](scene-binding.md)：决策层 `TW-*` 如何对齐到场景几何与 Actor tag、`primary_hop` 为什么取"最后一跳"、19 个高亮里 3 个无几何为什么显式列出而不补；生成物见 [绑定表报告](reports/scene-binding-report.md)（由 `decision_poc.scene_binding` 生成，勿手改）。
- [Nexent 工具注册](nexent-tool-registration.md)：14 个工具的注册清单（`samples/nexent/tools.json`，由代码导出）、三阶段分组挂载与验收标准；**真机注册已完成**，结果见 [真机报告](reports/nexent-live-integration-report.md)。
- [四个创新点：机制、证据、答辩口径](innovation-and-evidence.md)：语义层自动演化、因果推演与反事实、血缘影响分析、决策策略受控进化，以及不作为创新宣传的清单。
- [核心事件 Ground Truth](urban-scenario-ground-truth.md)：六跳标准答案、三类职责冲突、5 个安全拦截用例、3 个反事实问题。
- [通用内核设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)：目标、边界、架构和验收范围。
- [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)：按任务拆分的实现记录。
- [Nexent 集成边界](nexent-integration-boundary.md)：适配层职责与接入验收。
- [Nexent 北向传输层设计](nexent-northbound-transport-design.md)：已核对的 `/nb/v1` 契约、SSE 事件、幂等/限流与传输层落地方案。
- [演示运行手册](demo-runbook.md)：现场按步执行（自检 → 清记忆 → 跑 smoke → 判读 → 失败对照）。
- [数据资产标识规范](data-asset-spec.md)：异构文件如何变成可检索、可引用、可追溯的资产记录。
- [开源与提交清单](competition/release-checklist.md)：LICENSE、一条命令复现脚本（`scripts/reproduce.py`，17 步）、待提交改动分组与推送前的三个确认项。
- [本体半自动构建方案](ontology-bootstrap.md)：种子本体、规则抽取、置信度分档、跨文档多跳证据与合并计划。

## 阅读顺序

1. 先看根目录 [README.md](../README.md)，了解项目定位和快速运行方式。
2. 再看 [通用内核设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)，确认当前不绑定行业的范围。
3. 需要验证结果时查看 [技术基线与风险](technical-baseline.md)。
4. 需要追溯实现过程时查看 [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)。

## 文档维护规则

- 新增公共行为时同步更新 README、对应设计文档和测试。
- 行业主题确定后，新增独立领域文档，不把主题逻辑直接塞入通用内核说明。
- 技术基线只记录可复现的技术事实，不提前写入未经确认的比赛指标。
