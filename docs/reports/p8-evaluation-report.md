# P8 评测报告

由 `python -m decision_poc.urban_eval --report <path>` 生成，**不要手改**。

- 数据集快照：`9b60debff3c9c53c`
- 用例：22 例，通过 22，失败 0

## 1 评测项

| 评测项 | 数值 | 口径 |
| --- | --- | --- |
| 资产解析 | 15/15 | `parse_status=parsed` 的资产数 |
| 未识别列 | 0 | 表头没有对应规范列的列数，逐表统计 |
| 实体关系 | 101 条 / 8 类 | 语义层的三元组总数与谓词种类 |
| 检索命中 | 100.00% | 用例声明的必须证据定位中，实际引到的比例 |
| 多跳完整 | 63.64% | 六跳齐全的用例占全部用例比例 |
| 引用完整率 | 100.00% | 审计报告的平均引用完整率 |
| 不可支撑结论 | 0 | 未通过引用闸门的 claim 数 |
| 响应时间 p50 / p95 | 5 ms / 6 ms | 单用例端到端（五 Skill 全流程） |

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
| N01 核心事件六跳齐全 | normal | ok | 是 | 通过 | 7 ms |
| N02 同一事件两小时后（预警仍有效） | normal | ok | 是 | 通过 | 6 ms |
| N03 次日红色预警生效时 | normal | ok | 是 | 通过 | 6 ms |
| N04 基坑沉陷（无积水深度） | normal | partial | 否 | 通过 | 6 ms |
| N05 B-01 未达阈值积水 | normal | partial | 否 | 通过 | 6 ms |
| N06 泵站故障单独成案 | normal | partial | 否 | 通过 | 6 ms |
| N07 联合身份（应急+道路） | normal | ok | 是 | 通过 | 6 ms |
| C01 三类冲突编号齐全 | conflict | ok | 是 | 通过 | 3 ms |
| C02 职责空白必须出现（泵站无运维主体） | conflict | partial | 否 | 通过 | 3 ms |
| C03 未达阈值时不该编造资源争用 | conflict | partial | 否 | 通过 | 3 ms |
| I01 未知事件 | insufficient | error | 否 | 通过 | 1 ms |
| I02 as_of 早于工单上报时间 | insufficient | partial | 否 | 通过 | 4 ms |
| I03 已闭环工单不再推演 | insufficient | partial | 否 | 通过 | 4 ms |
| I04 时点早于全部数据（2026-03） | insufficient | partial | 否 | 通过 | 5 ms |
| I05 上报前 40 分钟 | insufficient | partial | 否 | 通过 | 4 ms |
| I06 B-01 上报前（差 20 分钟） | insufficient | partial | 否 | 通过 | 4 ms |
| P01 无身份调用 | permission | partial | 否 | 通过 | 6 ms |
| P02 燃气方视角（依据不可见） | permission | partial | 否 | 通过 | 5 ms |
| E01 预警与封路公告都过期（20:00） | expired | ok | 是 | 通过 | 6 ms |
| E02 民生街占道施工结束前（11-05 20:00） | expired | ok | 是 | 通过 | 6 ms |
| E03 全部施工计划结束后（11-10） | expired | ok | 是 | 通过 | 6 ms |
| E04 B-01 预警过期后 | expired | partial | 否 | 通过 | 5 ms |

## 4 失败案例

无。

## 5 改进前后对比

| 指标 | 改进前 | 改进后 |
| --- | --- | --- |
| 通过用例 | 17/22 | 22/22 |
| 失败用例 | 5 | 0 |
| 多跳完整率 | 86.36% | 63.64% |
| 引用完整率 | 100.00% | 100.00% |

失败的用例清单（改进前）：

- I02 as_of 早于工单上报时间：状态 期望 ['partial', 'error'] 实际 ok；可发布 期望 False 实际 True；应拒答（只有一跳且 insufficient），实际给出了多跳结论；拒答的结论被标为可发布；六跳齐全 期望 False 实际 True（['action', 'affected', 'conflict', 'observed', 'owner', 'resource']）
- I03 已闭环工单不再推演：应拒答（只有一跳且 insufficient），实际给出了多跳结论
- I04 时点早于全部数据（2026-03）：状态 期望 ['partial', 'error'] 实际 ok；可发布 期望 False 实际 True；应拒答（只有一跳且 insufficient），实际给出了多跳结论；拒答的结论被标为可发布
- I05 上报前 40 分钟：应拒答（只有一跳且 insufficient），实际给出了多跳结论
- I06 B-01 上报前（差 20 分钟）：应拒答（只有一跳且 insufficient），实际给出了多跳结论
