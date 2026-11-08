# Twin3D 完整业务闭环

## 已验收（2026-11-08）

- [x] 统一 `event_id / jump_id / entity_id / work_order_id / action_card_id`。
      关联层 `src/decision_poc/twin_links.py` 一处产出，页面不再各自猜。
      `claim_id` 目前只到卡片级（`cards[].claim_ids`），未落到沙盘对象上——见下方待办。
- [x] 主页面通过 URL 参数定位沙盘跳次，沙盘可返回主页面对应事件。
      `/twin3d?jump=N&entity=ID`，`N` 取 1..6 六跳、7 表"处置后"；`entity` 不静默纠正，
      未命中时接口回 `requested_entity.found=false` 由页面提示。
- [x] 将场景对象与工单、ActionCard、证据引用建立映射。
      依据链：`cards[].evidence_refs` 里的 `work_order#row:N` → 台账第 N 行 → 工单 →
      `entity.links.work_orders` → 对象。每一步都能回溯，不做空间邻近猜测。
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

- [ ] `claim_id` 落到沙盘对象：现在点对象能看到动作卡的证据，但看不到"这条判断支撑哪条断言"。
      补法是在 `twin_links` 里把 `cards[].claim_ids` 合并到 `entity.claim_ids`，
      页面加一行"判断依据（claim）"。**不需要改推演内核**。
- [ ] 工单状态回写：台账 `status` 目前只读，处置动作不会改它。若要做"办结"闭环，
      得先有写入通道与审计（谁改的、依据哪张卡），否则就是假闭环。
- [ ] 结果回指 claim：`results.rows` 现在只挂台账来源，未挂 claim_id。
      需要 claim 侧补一条"结果类断言"，属于推演层扩展，不在展示层能解决。

## 验证口径

```
python -m pytest -q                     # 952 项，1 skipped（可选依赖 openai）
python -m decision_poc.evidence_check    # 108/108，指纹 a8acecc032b02a4b
```
