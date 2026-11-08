# Twin3D 完整业务闭环待办

- [ ] 统一 `event_id / jump_id / entity_id / work_order_id / action_card_id / claim_id`。
- [ ] 主页面通过 URL 参数定位沙盘跳次，沙盘可返回主页面对应事件。
- [ ] 将场景对象与工单、ActionCard、证据引用建立映射。
- [ ] 同步 ActionCard 状态：待执行、执行中、已完成、待人工确认。
- [ ] 当前跳次高亮所有关联对象，并支持状态恢复。
- [ ] 展示处置前后结果变化，结果回指 claim 和证据。
- [ ] 增加一条端到端测试：事件 → 判断 → 动作 → 状态 → 结果 → 证据。
