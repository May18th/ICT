# 文档导航

这个目录按**阅读顺序**组织，不按文件名字母序。想快速判断"我该读哪几篇"，看下面的六步；
想查某一篇具体讲什么，往下翻到对应分层。

## 阅读顺序

| 步 | 读什么 | 解决什么问题 |
|---|---|---|
| 0 | 仓库根 [`README.md`](../README.md) | 这是什么、怎么三分钟跑起来 |
| 1 | [技术基线与风险](technical-baseline.md)、[工程版说明](README-dev.md) | 做到哪一步、哪些是已知限制 |
| 2 | [数据资产标识规范](data-asset-spec.md)、[核心事件 Ground Truth](urban-scenario-ground-truth.md) | 数据从哪来、标准答案长什么样 |
| 3 | [演示运行手册](demo-runbook.md)、[Nexent 侧装配手册](../deploy/nexent/README.md) | 现场怎么跑、平台怎么装 |
| 4 | [对外数字自检报告](reports/evidence-check-report.md)、[P8 评测报告](reports/p8-evaluation-report.md) | 凭什么说它有效 |
| 5 | [`competition/`](competition/) 下的四维映射与价值证据 | 交付材料怎么对应评分维度 |
| 6 | `reports/` 里 P2–P13 各阶段报告、`superpowers/specs/` | 追溯某个设计为什么是这样 |

前四步走完就能答辩，5 和 6 是备查。

## 1. 项目架构与技术基线

- [工程版说明](README-dev.md)：安装、部署、调试与目录职责。
- [技术基线与风险](technical-baseline.md)：固定通用场景、测量项和已知限制。
- [P0 接口定稿与骨架基线](p0-interface-freeze.md)：八层落点、id/错误码/信封/字段映射，固定后变更需评审。
- [四个创新点：机制、证据、对外说明口径](innovation-and-evidence.md)：语义层自动演化、因果推演与反事实、血缘影响分析、决策策略受控进化，以及不作为创新宣传的清单。
- [通用演进设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)：目标、边界、架构和验收范围。
- [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)：按任务拆分的实现记录。
- [城市可移植性审计](city-portability-audit.md)：哪些城市常量是写死的、换成别的城市需要改哪里。
- [赛道 3 组件边界与推进路径 v0.1](赛道3-组件边界与推进路径-v0.1.md)：四个外部组件各扮演什么角色、能借到哪一步、哪一步不能碰，以及先做什么后做什么、哪里会翻车。

## 2. 数据与场景契约

- [数据资产标识规范](data-asset-spec.md)：异构文件如何变成可检索、可引用、可追溯的资产记录。
- [核心事件 Ground Truth](urban-scenario-ground-truth.md)：六步标准答案、三类职责冲突、5 个安全拦截用例、3 个反事实问题。
- [图像语义抽取](image-semantics.md)：PNG 像素级抽取（频谱峰值/示意色块/积水范围），输出 RegionSpec 区域候选；明确不做 OCR、不用视觉模型。
- [实体重投影与绑定表](scene-binding.md)：决策层 `TW-*` 如何对齐到场景几何与 Actor tag、`primary_hop` 为什么取"最后一跳"、19 个高亮里 3 个无几何为什么显式列出而不补。
- [泵站前池库容曲线（假设值）数据说明](hydro-forebay-assumptions.md)：`samples/assets/urban/pump_forebay_curves.csv` 的列定义、假设链条、演示算例与敏感性。
- [本体半自动构建方案](ontology-bootstrap.md)：种子本体、规则抽取、置信度分档、跨文档多跳证据与合并计划。
- [P2 任务单：认知加工层](tasks/p2-cognition-task-order.md)：适配器、切片、表格标准化、区域标注、抽取基线。
- [P2 回传报告](reports/p2-cognition-report.md)：解析与抽取统计、三类抽查样例、硬断言验证结果。
- [P4 回传报告](reports/p4-graph-provenance-report.md)：时态语义层、引用闸门、哈希链篡改定位、跨版本血缘（locator 键）。
- [定量口径固定：方案 A](quant-baseline-option-a.md)：需求折算 v1 与负荷 `load_state` 的裁决结论——为什么选这套口径、被否决的方案差在哪里。后续水文/水力改动以此为基准。

## 3. 运行与部署

- [演示运行手册](demo-runbook.md)：现场按步执行（自检 → 清记忆 → 跑 smoke → 判读 → 失败对照）。
- [Nexent 侧装配与复现手册](../deploy/nexent/README.md)：容器构建、平台装配脚本、平台实测校验与证据抓取；含 SKILL.md 上传平台技能仓库（§2.1）与平台界面截图/录屏取证（§4.1）。
- [Nexent 集成设计（SSE + SKILL.md）](nexent-urban-integration-design.md)：为什么必须走 SSE、装配步骤、证据清单与验收口径。
- [Nexent 集成边界](nexent-integration-boundary.md)：适配层职责与接入验收。
- [Nexent 北向传输层设计](nexent-northbound-transport-design.md)：已核对的 `/nb/v1` 契约、SSE 事件、幂等/限流与传输层落地方案。
- [Nexent 工具注册](nexent-tool-registration.md)：15 个工具的注册清单（`samples/nexent/tools.json`，由代码导出）、三阶段分组接入与验收标准。
- [城市内涝主链路 Nexent 集成实测报告](reports/nexent-live-integration-report.md)：MCP-over-SSE 注册、Agent 发布、8/8 一致性（conversation_id=120，Agent v19 / DeepSeek-Chat）、平台工具调用日志、排障与边界。
- [平台侧证据报告](reports/platform-evidence-report.md)：SKILL.md 上传平台技能仓库（skill_id=2/3、幂等、正文逐位一致）+ 平台界面三张截图断言取证。
- [真实语言模型可选接入设计](real-model-provider-design.md)：默认 `TestModel` 离线不变；显式配置后走 DeepSeek 官方 provider，含配置、失败语义、密钥安全与验收口径。
- [真实用户评分采集](real-feedback-channel.md)：真实用户评分（进化前后各一次）的采集/汇总/导出，以及「只汇总不进校验」与「带修正建议才进校验」两条通道的口径。
- [P5 回传报告](reports/p5-mcp-report.md)：7 个 MCP 工具契约、stdio 协议实现、证据接口的权限收口、trace_id 与调用日志。
- [P10 可视化工具层设计](p10-visualization-tools-design.md)：决策状态记录 → 渲染作业 → 引擎 ack 的异步流程、3 个 MCP 工具、工具分组 allowlist 与演示时序。
- [渲染能力接入 Nexent 操作手册](nexent-mcp-render-integration.md)：让平台 Agent 推演完自己拿到可播放成片，且拿到的是那份结论对应的版本——同一次调用链里 `snapshot_id` 与成片 sha256 都可核对、重跑幂等。

## 4. 评测与证据

- [对外数字自检报告](reports/evidence-check-report.md)：108 项对外宣称数字的期望/实际逐项对照（含漂移检测，由 `decision_poc.evidence_check` 生成，**勿手改**）。
- [P8 评测报告](reports/p8-evaluation-report.md)：22 个用例的五类形态、七项指标口径、失败案例与改进前后对比（由 `decision_poc.urban_eval` 生成，**勿手改**）。
- [P8 之上的真实模型 A/B 报告](reports/p8-llm-ab-report.md)：同一批 22 个用例上的 rule / TestModel 地板 / DeepSeek 三方对照，含"模型不能自己推断优先级"的实测结论（由 `scripts/llm_ab_eval.py` 生成，**勿手改**）。
- [P11 消融阶梯报告](reports/p11-ablation-report.md)：五档配置 × 22 用例的同口径指标、leave-one-out 稳健性与精确符号翻转检验（2²² 全枚举、零随机）（由 `decision_poc.ablation` 生成，**勿手改**）。
- [P9 策略演进报告](reports/p9-policy-evolution-report.md)：分配策略的逐轮偏差归因、评测校验采纳/拒绝、留出集泛化、分层记忆 warm start、人工评审第二信号与回滚验证（由 `decision_poc.policy_evolution` 生成，**勿手改**）。
- [绑定表报告](reports/scene-binding-report.md)：决策层实体到场景几何的绑定结果（由 `decision_poc.scene_binding` 生成，**勿手改**）。
- [P12 定量层落地报告](reports/p12-quant-layer-report.md)：水文/水力/不确定性引擎的公式-出处对照、与稳态校核报告的手算对拍、本阶段未做项与测试数口径变更说明。
- [P12 多智能体协同层落地报告](reports/p12-collab-layer-report.md)：1 总控 + 5 专业部门的动作卡契约、联合发布与处置顺序裁决、引擎输出稳定性实测。
- [P12 场景 v2 口径标定报告](reports/p12-scenario-v2-calibration-report.md)：下穿隧道倒灌判定与堰流进水量、泵站负荷分档，以及"v2 泵站口径会让缺口 1→0 并摧毁 C3/P9"的实测证据与两条处置方案。
- [P13 简化水动力演进落地报告](reports/p13-simplified-hydrodynamics-report.md)：两种演进方法与两个过程线查询的手算对拍、"泵站停机后水位过程"缺口的可算链路、未做项与测试数口径变更。
- [P3 回传报告](reports/p3-evolution-report.md)：提案→确认→发布→回滚流程、三个变更场景实测、安全默认与回滚哈希一致性。
- [P6 回传报告](reports/p6-deduction-report.md)：六步推演、规则即数据（来源行校验）、三类冲突、资源缺口算式、结论落账。
- [P6 Skill 与编排回传](reports/p6-skills-report.md)：五个 Skill 的契约与降级策略、审计闸门、Nexent 绑定表、与 MCP 路径的一致性校验。
- [P6 反事实回传](reports/p6-counterfactual-report.md)：CF1–CF3 的干预-重放-取差异机制、算不出来的部分如何声明、第 8 个 MCP 工具。

## 5. 比赛交付材料

先看 [赛题官方简报要点](competition/official-brief-2026-11-06.md)，再看下面三篇能把"题目要求"逐条对上"我们做了什么"：

- [四维映射](competition/four-dimensions-mapping.md)：评分维度 → 实现与证据的逐项对应。
- [价值与证据](competition/value-evidence.md)：每项价值的可核验出处。
- [相关工作定位表](competition/related-work-positioning.md)：与已发表的 Agentic AI + 数字孪生 + 审计框架逐维度对照、三条对外说明口径与"不能怎么做"边界。

补充材料：

- [Skill 与 MCP 设计](competition/skills-mcp-design.md)：平台侧技能与工具的分工。
- [评测数据集规范](competition/eval-dataset-spec.md)：22 个用例的构成与标注口径。
- [评审说明](competition/reviewer-instructions.md)：给评审的操作指引。
- [离线交付说明](competition/OFFLINE.md) 与 [离线部署评估](competition/offline-deploy-assessment.md)：无网环境下的交付与限制。
- **成文材料**：`competition/初赛开发设计文档-v2（模板原样）.docx`（按官方模板 1 成文）、`competition/模板1-原件备份.docx`。
- **证据附件**：`competition/evidence/`（页面与截图）、`competition/figures/`（架构图源文件）、`competition/video-clips/`（录屏片段）。

> 内部工作文档（分工、答辩主线、差距审计、初赛 PPT、信息收集表等）**不随公开仓库分发**。
> 它们在本地仍有副本，但不要在这里加链接——读者拿不到文件。

## 6. 设计档案与历史报告

按阶段编号的原始设计与回传报告，用于追溯某个决策当时是怎么定的。日常不需要读。

- [P9 决策策略演进设计](superpowers/specs/2026-10-30-p9-decision-evolution-design.md)：为什么选资源分配、决策输入/策略/偏差口径、进化循环、分层记忆与人工反馈（第 11–14 节）、浏览器对比面板（第 15 节）。
- [P11 消融与锚定设计](superpowers/specs/2026-11-02-p11-ablation-and-anchoring-design.md)：五档能力子集、指标分母与确定性统计口径，以及 `H(o),H(Z),H(a*),meta` 锚定包与链下原文复核边界。
- [P12 多智能体协同 + 场景 v2 设计](superpowers/specs/2026-11-02-p12-multi-agent-collaboration-and-scenario-v2-design.md)：1 总控 + 5 专业 Agent 的分层架构、三个现实问题→三项能力的映射、地下通道倒灌入六步、泵站负荷缺口公式。
- [P13 简化水动力演进设计](superpowers/specs/2026-11-05-p13-simplified-hydrodynamics-design.md)：马斯京根汇流 + 修正 Puls 调蓄的公式、稳定条件与"不是圣维南"的边界。

**已停用模块的历史记录**——保留是为了追溯"为什么删"，不要按它继续开发：

- [Twin3D 完整业务闭环](twin3d-business-loop-todo.md)：**已废弃 2026-11-14**，`/twin3d` 页面下线（3D 沙盘与 `/map2d` 表达同一批数据，删页减入口）。关联层 `twin_links.py` 仍在使用，被 `/map2d` 与 `/workbench` 的对象卡片消费。
- [UE 真实资产进度](ue-real-assets-progress.md)：Unreal 工程已停用且不入库，本文是当时的完成情况记录，不作为待办。

**技术债与暂缓项**：

- [水动力链路重构候选清单](hydro-integration-refactor-candidates.md)：只记死代码、重复逻辑和暂缓项。规则是「已删除项必须有行为测试，暂缓项不在本轮擅自删除」，不是想做就做的愿望清单。

## 7 输入材料

赛题原文与早期方案——属"给定"而非"产出"。改它们之前先确认你不是在改题目。

- [赛题 3 任务单](ICT-赛题3-城市内涝事件推演-WorkBuddy任务单.md)：赛题下达时的原始任务书。里面写的是当时的验收口径，与当前数字不同属正常，不要拿它当依据。
- [赛题 3 方案与实施计划](ICT-创新赛-赛题3-方案与实施计划.md)：立项时的阶段划分与交付设想；实际落地以 `reports/` 下的阶段报告为准。

## 文档维护规则

- 新增公共行为时同步更新 README、对应设计文档和测试。
- 自动生成的报告（标注"勿手改"的）只由脚本重写，不手工编辑。
- 对外数字只认 `python -m decision_poc.evidence_check`；改数字要同步 `samples/eval/evidence_expectations.json`、相关 `.md` 与 docx/pptx 交付物。
- 技术基线只记录可复现的技术事实，不提前写入未经确认的比赛指标。
