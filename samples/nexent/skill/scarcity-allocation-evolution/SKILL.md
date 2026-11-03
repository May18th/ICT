---
name: scarcity-allocation-evolution
description: |
  当六跳推演显示跨片区抽排资源存在缺口，需要回答"这几台先给谁、为什么"时使用；也用于
  比较 v1 与进化后策略在同一事件上的分配差异、以及回滚验证。所有分配必须来自策略工具，
  不允许按经验口算。
tags:
  - allocation
  - decision-evolution
  - rollback
---

# 稀缺资源分配与策略进化

## 适用场景

- 推演结论里出现 `resource_gap > 0`，需要给出**分配 + 调拨 + 需上报增援**；
- 评委/用户追问"进化前后有什么不同"、"改坏了怎么回滚"；
- 需要把一次分配决策与对应场景快照对齐（供演示/追溯）。

## 边界（平台内，重要）

- 用户只问**六跳结论**时，**不要走本文流程**：`event_deduce` 返回里已有 `resource.gap` 与
  `resource.basis`，直接按六跳 skill 输出即可，不要为了"更完整"去调 `allocation_plan`。
- 只有用户明确追问"这几台先给谁""进化前后差异""怎么回滚"时，才执行下面的流程。
- 一次只发一个工具调用，禁止 `parallel_executor`（会被平台截断）。

## 流程

1. **取决策输入**：`event_deduce` 结果的 `decision_inputs` 是每个片区的
   深度/封路判定/危险源/需求/本区库存，不要自己从结论文字里推算。
2. **默认策略（v1）**：调 `allocation_plan`（不传 `policy`），得到"各片区先用本区库存"的方案。
3. **进化后策略**：调 `allocation_plan` 并传入进化后的 `policy`：
   `{"policy_version":"v3","weight_closure":1.0,"weight_hazard":0.25,"transfer_penalty":0.1}`。
4. **对比**：若有渲染快照工具，先 `scene_render` 生成两个快照，再 `scene_compare` 取差异；
   否则直接对比两次 `allocation_plan` 的 `allocation_by_district` / `unmet_by_district` / `transfers`。
5. **回滚说明**：策略版本链与回滚验证由 `python -m decision_poc.policy_evolution` 产出，
   引用报告中的标签（`flood-response-v1.0 → v1.1 → v1.2`）与回滚后偏差复原数值。

## 硬约束

- **关键片区不可被调出**：`closure_required=True` 或 `has_hazard=True` 的片区默认禁止被抽调；
  如果需要放宽，必须显式传 `allow_transfer_from_critical` 并说明风险。
- **调拨必须写时延**：跨区调拨按台账记录 40 分钟，不得省略。
- **缺口不得抹平**：仍缺的部分写进 `escalation`（上报跨区/市级增援），不允许显示为"已满足"。
- **口径**：反馈来自"受控评测环境下的反馈信号"（冻结 ground truth + 人工评审），
  不得表述为"真实市政数据回流"；人工评审为角色扮演，不得表述为真实部门批复。

## 输出格式

```json
{
  "policy_version": "v3",
  "allocation_by_district": {"A-03": 5, "B-01": 1},
  "unmet_by_district": {"A-03": 0, "B-01": 1},
  "transfers": [{"from_district": "B-01", "to_district": "A-03", "qty": 1, "delay_min": 40}],
  "escalation": ["B-01 仍缺 1 台：需上报跨区/市级增援"],
  "why": "A-03 已过封路阈值且有燃气基坑；B-01 未达阈值、无危险源"
}
```

## 参考命令（人工复核用）

```bash
PYTHONPATH=src python -m decision_poc.allocation --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00
PYTHONPATH=src python -m decision_poc.policy_evolution --report docs/reports/p9-policy-evolution-report.md
```
