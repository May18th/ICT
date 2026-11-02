---
name: urban-six-hop-deduction
description: |
  面向城市内涝/道路积水/地下管线施工等突发事件的跨部门六跳推演。当用户给出事件编号（如
  WO-20261029-007）与时间点，需要回答"哪些设施受影响、哪些部门协同、资源够不够、处置按什么
  顺序、每条结论依据什么"时使用。必须调用 MCP 工具取证，禁止凭记忆作答。
tags:
  - urban
  - emergency-response
  - cross-department
  - provenance
---

# 城市内涝跨部门六跳推演

## 适用场景

- 用户提供一个**事件编号 + 时点**（例：`WO-20261029-007 @ 2026-10-29T09:40:00+08:00`）；
- 需要跨部门协同处置建议，且每条结论要能回指来源资产与规则条款。

不适用：没有事件编号的泛泛咨询；纯知识问答；预测未来事件。

## 必须遵守的流程（六跳，顺序不可乱）

1. **事件理解**：调 `event_deduce`，取 `root`（片区、积水深度、响应等级）与第一跳状态；
   深度缺失时不要按 0 处理，直接进"证据不足"路径。
2. **影响分析**：调 `twin_neighbors`（种子 = 事件 `twin_id`，`max_hops=3`，用
   `block_expansion_through=["region"]` 防止片区当中转），列出受影响对象并给出证据定位。
3. **责任部门**：在 `event_deduce` 结果的第 3 跳里核对；出现 `unassigned` 即职责空白，
   必须转人工，不得替部门认领。
4. **职责冲突**：调 `conflict_check`，三类冲突（overlap / gap / contention）必须**全部保留**，
   不允许"综合判断"抹平。资源争用要给缺口数值。
5. **资源测算**：在 `event_deduce` 第 5 跳读需求/库存/缺口；缺口 > 0 时调 `allocation_plan`
   给出分配与需上报增援的部分（跨区调拨必须写明时延）。
6. **处置顺序与二次风险**：按 `action_order` 输出，并**校验"隔离风险源"序位早于"抽排"**；
   二次风险逐条列出。

## 证据要求（硬约束）

- 每个结论必须附 `evidence_refs`（形如 `AS-...-xxxx#row:3` 或 `#line:33-35`）；
- 用户追问某条依据时，调 `evidence_locate` 取回原文片段，不得改写引文；
- 出现 `PERMISSION_DENIED`：**停下并说明"依据对当前身份不可见"**，不允许删掉依据继续给结论；
- 出现 `insufficient`：标注"证据不足、转人工"，不允许编造数值。

## 输出格式

```json
{
  "event_id": "WO-20261029-007",
  "as_of": "2026-10-29T09:40:00+08:00",
  "six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"],
  "summary": "一句话结论",
  "conflicts": ["C1", "C2", "C3"],
  "resource_gap": 1,
  "action_order": ["停止基坑开挖...", "隔离风险源...", "封闭积水路段...", "..."],
  "residual_risks": ["..."],
  "evidence_refs": ["AS-...#row:4", "..."],
  "handoff_required": true,
  "handoff_reasons": ["..."]
}
```

## 失败处理

| 情况 | 动作 |
| --- | --- |
| 事件不存在（NOT_FOUND） | 直接告知，不猜 |
| 时点早于上报时间 / 工单已闭环 | 输出拒答原因（引擎会返回一跳 `insufficient`），不做推演 |
| 依据不可见（PERMISSION_DENIED） | 停下，说明缺哪个身份的可见性 |
| 规则/参数缺失（EVIDENCE_INSUFFICIENT） | 声明"该跳不可判定"，不补数字 |

## 参考命令（人工复核用）

```bash
PYTHONPATH=src python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00
PYTHONPATH=src python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --caller-labels ORG-EMERG
```
