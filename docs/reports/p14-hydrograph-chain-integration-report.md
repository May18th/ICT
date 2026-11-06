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
evidence_check：106/106，指纹 a60477b1f24dcfa9
```

> 以上为 P14 落地当时的验收口径；**当前口径以 `python -m decision_poc.evidence_check` 实跑为准**（数字见 README 验证状态），本报告不随后续变更同步。

## 4 后续增量：库容曲线来源标注进契约（2026-11-06）

"12 分钟到警戒"这类数字一旦进对外材料，出处不能只活在文档里，因此把来源做成契约字段
（实现见 `tests/test_hydro_provenance.py`，本报告只记录对既有链路的影响）：

| 项 | 变化 |
| --- | --- |
| 契约 | `HydrographResult` 末尾新增 `storage_provenance: CurveProvenance \| None`（默认 `None`）；旧 13 个键的相对顺序与取值不变，默认路径 dump 只多这一个键 |
| 生产端 | `level_pool_route(storage_provenance=CurveProvenance.assumption(...))` 声明后写进字段，并在 `assumptions` 追加固定前缀行 `库容来源：<basis>｜<ref>`；不可计算路径同样带来源（算不出来不等于输入没来源）。**刻意不接受"级别 + 出处"两个独立参数**：那会让"只写出处、忘了级别"成为可表达状态并静默丢来源，改为单个 `CurveProvenance` 对象后该组合在签名上不存在 |
| 下游 | `time_to_level` / `level_exceedance_duration` 继承同一前缀行；**位置不固定**（超阈时长会在末尾追加"未回落"备注），因此约定是"有且只有一行带前缀"，靠前缀识别而非位置 |
| 缓存 | `skills.py` 未改：缓存键仍是完整 `model_dump_json()`，同数值不同来源会落两个键；**故意不排除 provenance**——排除会让先跑的假设曲线替后来的实测曲线"背书" |
| 回放/可视化 payload | `replay_export` 的 `metrics["hydrograph"]` 现在多一个 `storage_provenance` 键；`test_presentation_contract` 仍与同一份 `model_dump` 比较，不改成硬编码字段表 |
| 链路状态 | 本轮只把来源接进契约，**来源仍由调用方手工传入**；CSV → loader → `storage_provenance` 的自动贯通在资产化那轮完成，在那之前不得对外宣称"出处已贯通" |
