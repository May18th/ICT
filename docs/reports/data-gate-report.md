# 数据门禁报告

- 数据集：`samples/assets/urban`
- 来源标注：`samples/gate/data_provenance.json`
- 事实总数：**60**

## 一、来源分级

| 级别 | 条数 | 含义 |
| --- | --- | --- |
| real | 4 | 可定位到预案/台账原文 |
| derived | 3 | 由真实值按明确公式算出 |
| assumption | 2 | 无公开口径，按量级假设，待核实 |
| synthetic | 51 | 演示数据，不得当作实测 |

## 二、检查结果

| 检查项 | 级别 | 对象 | 说明 |
| --- | --- | --- | --- |
| DG-SRC-006 | warn | `50 条` | 未逐条标注来源，按数据集自述记为 synthetic：road:R-101/elevation_m、road:R-101/length_m、road:R-102/elevation_m、road:R-102/length_m、road:R-103/elevation_m 等 50 条；对外表述不得把这一批说成实测值 |

## 三、声明边界

- 本次结论建立在 **60** 条事实上，其中 real 4 条、derived 3 条、assumption 2 条、synthetic 51 条。
- assumption 与 synthetic 两类**不得对外表述为实测值**；阈值与假设的口径需甲方书面确认后才能在对外材料中引用。
- 来源层面有 **1** 项待确认（见上表）。确认前，相关数值只能在内部联调材料里出现，不得写进对外材料当作实测。

结论：**通过**（0 项 error / 1 项 warn）
