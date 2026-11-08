# Twin3D 完整业务闭环

## 已验收（2026-11-08）

- [x] 统一 `event_id / jump_id / entity_id / work_order_id / action_card_id / claim_id`。
      关联层 `src/decision_poc/twin_links.py` 一处产出，页面不再各自猜。
- [x] 主页面通过 URL 参数定位沙盘跳次，沙盘可返回主页面对应事件。
      `/twin3d?jump=N&entity=ID`，`N` 取 1..6 六跳、7 表"处置后"；`entity` 不静默纠正，
      未命中时接口回 `requested_entity.found=false` 由页面提示。
- [x] 将场景对象与工单、ActionCard、证据引用建立映射。
      依据链：`cards[].evidence_refs` 里的 `work_order#row:N` → 台账第 N 行 → 工单 →
      `entity.links.work_orders` → 对象。每一步都能回溯，不做空间邻近猜测。
- [x] `claim_id` 落到沙盘对象：对象详情新增「判断依据」区块，列出挂在它上面的断言
      （中文名 + 是否成立 + 证据条数）。claim 由动作卡的 `claim_ids` 并集得到，
      不是另算一遍；断言名在后端 `twin_links.claim_label` 统一翻译，页面不再各写一份。
- [x] 同步 ActionCard 状态：待执行、执行中、已完成、待人工确认。
      由当前跳次派生（`twin_links.card_state`），规则四条、可断言；
      台账里没有任何"已开工/已完工"字段，页面上标注为**推演状态**，不是现场回传。
- [x] 当前跳次高亮所有关联对象，并支持状态恢复。
      地面环 + 竖直光柱，颜色随对象当前跳的状态变化；切跳只换颜色不改几何。
- [x] 展示处置前后结果变化。
      七行对照，每行带 `basis` 来源；积水深度"处置后"固定 null（无实测、无回演），
      缺口前后必然相同（库存不因处置动作变化），这两条写在 `note` 里而不是藏起来。
- [x] 增加一条端到端测试：事件 → 判断 → 动作 → 状态 → 结果 → 证据。
      `tests/test_twin3d_e2e.py`，另含状态单调性、页面级绑定顺序回归。

## 尚未做

- [ ] 工单状态回写：台账 `status` 目前只读，处置动作不会改它。若要做"办结"闭环，
      得先有写入通道与审计（谁改的、依据哪张卡），否则就是假闭环。
      暂缓到审计契约确定后再做。

## 结果行回指断言（已完成，但不新建断言）

`results.rows[*].claim_ids` 指向**已有**断言，7 行里绑定 5 行：

| 结果行 | 依据 |
|---|---|
| 积水深度 | `CL-S1-observed`（观测断言里就写着 30.0 cm） |
| 已被处置动作覆盖的未闭环工单 | 各动作卡的 `CL-S6-action-*` |
| 处置动作卡（已完成） | 那几张已完成卡各自挂的断言 |
| 已挂上动作的沙盘对象 | `CL-S2-affected` |
| 移动泵缺口 | `CL-S5-resource` |
| 未闭环工单 / 残余风险 | 空 —— 前者来自台账计数，后者是推演结论清单，都没有对应断言 |

**刻意不新建 `CL-S7-result-*`**：`claims_from()` 里 `claim_id = CL-{step_id}`，新增断言等于
新增推演步骤，会连带改六跳结构、`event_stream` 事件条数与审计账本，属于推演层改动。
结果行是统计视图，不是推演步骤，所以只在展示层做"回指"，挂不上的行留空并在 `basis` 写明来源。

## 验证口径

```
python -m pytest -q                     # 956 项，1 skipped（可选依赖 openai）
python -m decision_poc.evidence_check    # 108/108，指纹 00354b21254a9f1a
```
