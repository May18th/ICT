# P8 评测报告

由 `python -m decision_poc.urban_eval --report <path>` 生成，**不要手改**。

- 数据集快照：`d102aa8076489dd4`
- 用例：22 例，通过 22，失败 0

## 1 评测项

| 评测项 | 数值 | 口径 |
| --- | --- | --- |
| 资产解析 | 19/19 | `parse_status=parsed` 的资产数 |
| 未识别列 | 0 | 表头没有对应规范列的列数，逐表统计 |
| 实体关系 | 123 条 / 8 类 | 语义层的三元组总数与谓词种类 |
| 检索命中 | 100.00% | 用例声明的必须证据定位中，实际引到的比例 |
| 多跳完整 | 100.00% | 六跳齐全的用例 14/14（拒答与报错不计入分母） |
| 引用完整率 | 100.00% | 审计报告的平均引用完整率 |
| 不可支撑结论 | 0 | 未通过引用闸门的 claim 数 |
| 响应时间 p50 / p95 | 7 ms / 8 ms | 单用例端到端（五 Skill 全流程） |

## 2 安全处理（按用例类型）

| 类型 | 通过 |
| --- | --- |
| conflict | 3/3 |
| expired | 4/4 |
| insufficient | 6/6 |
| normal | 7/7 |
| permission | 2/2 |

## 3 用例明细

| 用例 | 类型 | 状态 | 可发布 | 结果 | 耗时 |
| --- | --- | --- | --- | --- | --- |
| N01 核心事件六跳齐全 | normal | ok | 是 | 通过 | 26 ms |
| N02 同一事件两小时后（预警仍有效） | normal | ok | 是 | 通过 | 8 ms |
| N03 次日红色预警生效时 | normal | ok | 是 | 通过 | 8 ms |
| N04 基坑沉陷（无积水深度） | normal | partial | 否 | 通过 | 10 ms |
| N05 B-01 未达阈值积水 | normal | partial | 否 | 通过 | 7 ms |
| N06 泵站故障单独成案 | normal | partial | 否 | 通过 | 7 ms |
| N07 联合身份（应急+道路） | normal | ok | 是 | 通过 | 8 ms |
| C01 三类冲突编号齐全 | conflict | ok | 是 | 通过 | 4 ms |
| C02 职责空白必须出现（泵站无运维主体） | conflict | partial | 否 | 通过 | 3 ms |
| C03 未达阈值时不该编造资源争用 | conflict | partial | 否 | 通过 | 4 ms |
| I01 未知事件 | insufficient | error | 否 | 通过 | 1 ms |
| I02 as_of 早于工单上报时间 | insufficient | partial | 否 | 通过 | 6 ms |
| I03 已闭环工单不再推演 | insufficient | partial | 否 | 通过 | 5 ms |
| I04 时点早于全部数据（2026-03） | insufficient | partial | 否 | 通过 | 6 ms |
| I05 上报前 40 分钟 | insufficient | partial | 否 | 通过 | 5 ms |
| I06 B-01 上报前（差 20 分钟） | insufficient | partial | 否 | 通过 | 6 ms |
| P01 无身份调用 | permission | partial | 否 | 通过 | 8 ms |
| P02 燃气方视角（依据不可见） | permission | partial | 否 | 通过 | 7 ms |
| E01 预警与封路公告都过期（20:00） | expired | ok | 是 | 通过 | 8 ms |
| E02 民生街占道施工结束前（11-05 20:00） | expired | ok | 是 | 通过 | 8 ms |
| E03 全部施工计划结束后（11-10） | expired | ok | 是 | 通过 | 8 ms |
| E04 B-01 预警过期后 | expired | partial | 否 | 通过 | 8 ms |

## 4 失败案例

无。
