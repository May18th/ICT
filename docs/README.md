# 文档导航

## 当前实现

- [技术基线与风险](technical-baseline.md)：固定通用场景、测量项和已知限制。

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
- [三个创新点：机制、证据、答辩口径](innovation-and-evidence.md)：语义层自动演化、因果推演与反事实、血缘影响分析，以及不作为创新宣传的清单。
- [核心事件 Ground Truth](urban-scenario-ground-truth.md)：六跳标准答案、三类职责冲突、5 个安全拦截用例、3 个反事实问题。
- [通用内核设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)：目标、边界、架构和验收范围。
- [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)：按任务拆分的实现记录。
- [Nexent 集成边界](nexent-integration-boundary.md)：适配层职责与接入验收。
- [Nexent 北向传输层设计](nexent-northbound-transport-design.md)：已核对的 `/nb/v1` 契约、SSE 事件、幂等/限流与传输层落地方案。
- [演示运行手册](demo-runbook.md)：现场按步执行（自检 → 清记忆 → 跑 smoke → 判读 → 失败对照）。
- [数据资产标识规范](data-asset-spec.md)：异构文件如何变成可检索、可引用、可追溯的资产记录。
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
