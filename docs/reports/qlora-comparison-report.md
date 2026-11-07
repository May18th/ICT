# QLoRA 微调对照（base vs tuned，allocation holdout 4 例）

- 模型：Qwen/Qwen2.5-1.5B-Instruct（4bit NF4）；tuned = base + `train_adapter`（qlora_smoke 1-step 产物）
- 口径：allocation_cases 为受控合成场景（eval-dataset-spec §2），非真实工单数据。

| 案例 | 维度 | base | tuned |
| --- | --- | --- | --- |
| H1 | json_valid | ❌ | ✅ |
| H1 | allocation_by_district | ❌ | ❌ |
| H1 | transfers | ❌ | ❌ |
| H1 | priority_order | ❌ | ❌ |
| H1 | critical_unmet | ❌ | ❌ |
| H2 | json_valid | ❌ | ✅ |
| H2 | allocation_by_district | ❌ | ❌ |
| H2 | transfers | ❌ | ❌ |
| H2 | priority_order | ❌ | ❌ |
| H2 | critical_unmet | ❌ | ❌ |
| H3 | json_valid | ❌ | ✅ |
| H3 | allocation_by_district | ❌ | ❌ |
| H3 | transfers | ❌ | ✅ |
| H3 | priority_order | ❌ | ❌ |
| H3 | critical_unmet | ❌ | ❌ |
| H4 | json_valid | ❌ | ❌ |
| H4 | allocation_by_district | ❌ | ❌ |
| H4 | transfers | ❌ | ❌ |
| H4 | priority_order | ❌ | ❌ |
| H4 | critical_unmet | ❌ | ❌ |

| 汇总（4 例命中率） | base | tuned |
| --- | --- | --- |
| json_valid | 0% | 75% |
| allocation_by_district | 0% | 0% |
| transfers | 0% | 25% |
| priority_order | 0% | 0% |
| critical_unmet | 0% | 0% |

> 诚实声明：smoke_adapter 仅 1 步反向传播，tuned 预期与 base 基本持平；
> 本表的目的是冻结评测口径，正式训练后同一脚本可直接复跑对比。

## 正式训练后的结论（2026-11-07，3 epochs × 8 例 = 24 步，loss 2.54→1.60，峰值显存 2303MB）

1. **格式能力可教**：json_valid 0% → 75%（3/4 输出合法 JSON）。
2. **schema 对齐与数值推理教不会**（24 步训练量下）：tuned 的 JSON 字段名偏差（`allocation` 而非 `allocation_by_district`）、跨区算术仍全错（0%）。
3. **结论**：调度结论的正确计算交给带工具的 DeepSeek 主链路；本地小模型微调只用于验证训练管线与对照方法，不作为生产推理路径。

训练脚本 `scripts/qlora_train.py`（batch=1、截断 512、梯度检查点——smoke 的 8508MB 溢出教训已吸收，正式训练峰值仅 2303MB）。
