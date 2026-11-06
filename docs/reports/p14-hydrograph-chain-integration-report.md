# P14 简化水动力过程线链路接入报告

- 日期：2026-11-05
- 对应设计：[P14 接入设计](../superpowers/specs/2026-11-05-p14-hydrograph-chain-integration.md)

## 1 已接入位置

| 位置 | 结果 |
| --- | --- |
| `DeductionEngine` | 接收可选过程线；无工单深度时补充米制峰值，并留下来源字段 |
| `SkillPipeline` | 请求、缓存键、StageView 和 DeductionView 保留同一过程线 |
| `event_deduce` / `collab_run` | 通过 MCP 参数校验并透传；协同输出保留 `resource_gaps` |
| `ReplayScript` / 可视化 | 使用同一 Pipeline 报告，指标中记录过程线，不再重复调用引擎 |

## 2 口径与限制

过程线只补充事实状态，不替代完整二维水动力模型，也不改变 v1 资源缺口。P13 的库容曲线、K/X、
泵站能力等输入仍需由调用方提供并在 `assumptions` 中标注；没有实测或率定数据时不能宣称为真实
成都片区水动力结论。

## 3 验收

新增断言复用既有测试函数，不增加对外测试总数；全量验收以本次命令实跑结果为准：

```text
pytest：803 passed
evidence_check：106/106，指纹 6484b6add7571a85
```
