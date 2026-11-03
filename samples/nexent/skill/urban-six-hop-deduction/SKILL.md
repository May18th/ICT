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

## 平台内执行口径（MCP 工具路径，先读这段）

1. **第一步只调 `event_deduce`**。它一次返回六跳全部事实：`steps` 里的 `observed` / `affected` /
   `owner` / `conflict` / `resource`、逐条 `conflicts`（C1/C2/C3）、`resource.gap`、`action_order`
   与每步 `evidence_refs`。拿到返回后直接组输出 JSON，**不要再调 `twin_neighbors`、`conflict_check`、
   `allocation_plan`** —— 这些数据已经在返回里，重复调用只会拉长上下文并导致回答被截断。
2. **禁止 `parallel_executor`**。平台一次性并发工具调用会截断，表现为回答停在 `<|tool_call_start|>`
   文本上、拿不到最终 JSON。一次只发一个工具调用。
3. 下列工具只在**追问与复核**时按需单个调用，不是主流程必需：
   `twin_neighbors`（要受影响对象逐条证据）、`conflict_check`（要冲突条款原文）、
   `allocation_plan`（要逐区调拨明细与时延）、`evidence_locate`（要引文原文）。
4. 用户没追问依据时，不要为了"更完整"去补调工具；结论以 `event_deduce` 返回为准。

## 六跳判定口径（核对 `event_deduce` 返回，不是工具调用清单）

1. **事件理解**：看 `observed`，取片区、积水深度、响应等级；深度缺失时不要按 0 处理，直接进
   "证据不足"路径。
2. **影响分析**：看 `affected`，列出受影响对象及其 `reason`；需要逐条证据时再调 `twin_neighbors`
   （种子 = 事件 `twin_id`，`max_hops=3`，`block_expansion_through=["region"]` 防片区当中转）。
3. **责任部门**：看 `owner.owners`；出现 `owner.unassigned` 即职责空白，必须转人工，不得替部门认领。
4. **职责冲突**：看 `conflicts`，三类（overlap / gap / contention）必须**全部保留**，不允许"综合判断"
   抹平；资源争用要给缺口数值。
5. **资源测算**：看 `resource` 的 required / available / gap；缺口 > 0 时按 `resource.basis` 说明口径，
   需要逐区调拨明细再调 `allocation_plan`（跨区调拨必须写明时延）。
6. **处置顺序与二次风险**：按 `action_order` 输出，并**校验"隔离风险源"序位早于"抽排"**；
   二次风险逐条列出。

## 证据要求（硬约束）

- 每个结论必须附 `evidence_refs`（形如 `AS-...-xxxx#row:3` 或 `#line:33-35`）；
- 用户追问某条依据时，调 `evidence_locate` 取回原文片段，不得改写引文；
- 出现 `PERMISSION_DENIED`：**停下并说明"依据对当前身份不可见"**，不允许删掉依据继续给结论；
- 出现 `insufficient`：标注"证据不足、转人工"，不允许编造数值。

## 输出格式

结构如下，**所有值必须来自本次工具返回**；方括号里是占位说明，不得照抄成本文以外的具体数字：

```json
{
  "event_id": "<工单号>",
  "as_of": "<用户给的 ISO 时点>",
  "six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"],
  "summary": "<一句话结论>",
  "conflicts": [{"conflict_id": "C1", "kind": "overlap", "subject": "<对象>"}],
  "resource_gap": <整数，从 resource.gap 读>,
  "action_order": ["<处置动作，按 action_order 顺序>"],
  "residual_risks": ["<二次风险>"],
  "evidence_refs": ["<asset_id#locator>"],
  "handoff_required": <true|false>,
  "handoff_reasons": ["<转人工原因>"]
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
