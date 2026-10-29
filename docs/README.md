# 文档导航

## 当前实现

- [技术基线与风险](technical-baseline.md)：固定通用场景、测量项和已知限制。

## 设计与计划

- [通用内核设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)：目标、边界、架构和验收范围。
- [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)：按任务拆分的实现记录。
- [Nexent 集成边界](nexent-integration-boundary.md)：适配层职责与接入验收。
- [Nexent 北向传输层设计](nexent-northbound-transport-design.md)：已核对的 `/nb/v1` 契约、SSE 事件、幂等/限流与传输层落地方案。

## 阅读顺序

1. 先看根目录 [README.md](../README.md)，了解项目定位和快速运行方式。
2. 再看 [通用内核设计](superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)，确认当前不绑定行业的范围。
3. 需要验证结果时查看 [技术基线与风险](technical-baseline.md)。
4. 需要追溯实现过程时查看 [实施计划](superpowers/plans/2026-10-26-generic-evolution-kernel.md)。

## 文档维护规则

- 新增公共行为时同步更新 README、对应设计文档和测试。
- 行业主题确定后，新增独立领域文档，不把主题逻辑直接塞入通用内核说明。
- 技术基线只记录可复现的技术事实，不提前写入未经确认的比赛指标。
