# P14 简化水动力过程线链路接入设计

## 1 目标

把 P13 的可选 `HydrographResult` 作为事实状态接入现有链路：

```text
HydrographResult → DeductionEngine → SkillPipeline → collab_run / ReplayScript
```

调用方没有过程线时，现有行为保持不变；过程线不可计算时只保留显式失败状态，不生成假序列。

## 2 接入边界

- `DeductionEngine.deduce` 接收可选过程线；工单没有积水深度时，且过程线单位是米，才用峰值补充
  观测深度，并记录 `depth_source=hydrograph_peak`、峰值与峰现时刻。
- `SkillPipeline`、`event_deduce`、`collab_run` 和回放指标透传同一份契约对象。
- 协同结果同时保留原有 `resource_gaps`；资源缺口仍按 v1 规则计算，不由过程线自动改写。
- 回放从同一份 Pipeline 报告读取规则 id，避免再次调用引擎造成两份可能漂移的快照。

## 3 明确不做

- 不实现圣维南非恒定流、2D 浅水、1D/2D 耦合或 SWMM 级率定；
- 不把成都风格合成片区写成真实成都率定结果；
- 不新增 MCP 工具，不改变 15 工具对外口径；
- 不凭过程线推断泵站汇水面积、故障泵组能力或积水面积，资源口径仍待真实台账输入。

## 4 验收要求

同一过程线在引擎、Skills、MCP 协同和回放中字段一致；无过程线的既有测试保持通过；
回放与可视化只消费一次 Pipeline 结果；全量测试与 106/106 证据自检通过。
