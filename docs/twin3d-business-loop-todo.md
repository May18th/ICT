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

## 工单状态写回（已完成，写进独立账本）

台账 `work_orders.csv` 是冻结数据资产（推演快照哈希、108 项自检都锚着它），
**一个字节都不改**。执行状态写进 `artifacts/execution/`：

- `events.jsonl` 唯一事实源，只追加；`state.json` 是投影缓存，删掉可重放重建；
- 入口：`GET /api/execution`（计划 vs 实际）、`POST /api/execution/events`、`POST /api/execution/export`；
- 页面：主页面「执行账本」卡片，逐卡 开始执行 / 提交完成 / 申请会签 / 会签确认，
  工单进度 `done/total`，全部完成才出现「申请关闭」（需人工二次确认）。

契约落点与边界：

| 项 | 落点 | 边界 |
|---|---|---|
| 状态机 | `execution_ledger.TRANSITIONS` | 跳步一律 409 `illegal_transition`，不自动补中间态 |
| 幂等 | `sha256(card_id + to_state)` | 粒度不含 `from_state`，所以 `done` 后的重开请求会以 `duplicate` 返回（效果同为拒绝，错误码不同） |
| 乐观锁 | `expected_version` | 不符返回 409 `version_conflict` 带当前版本 |
| 会签 | `co_sign_required` | 转 `done` 前必须逐部门 `sign`，缺一个 409 `co_sign_incomplete` |
| 身份 | 服务端 `DEFAULT_ACTOR` | 前端传 `actor_org/actor_role` 一律忽略；无真实登录，页面标注"演示执行账本" |
| 关单 | `close_order` 事件 | 需人工确认且该工单全部卡 `done`；台账不改，只在账本记关闭 |
| 导出 | `POST /api/execution/export` | 脱敏：`actor_role` 与 `note` 抹除，部门代码保留 |

**执行账本只收 `stage=action` 的卡**：观测/影响/责任/冲突/资源五跳是推演的分析产物，
不是谁去现场执行的动作，纳入账本会逼人给「观测」点开工。

## 尚未做

- [ ] `claim_id` 之外的审计维度：目前没有操作人真实身份（无登录系统），只有部门代码
      与固定角色。接真实身份要先有账号体系，不能靠前端传。
- [ ] 关单与台账的最终同步：账本记了"已关闭"，但 `work_orders.csv` 仍是 `open`。
      要真正结案需要一个受控的写回通道（带审批与回滚），不在当前范围。

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
python -m pytest -q                     # 977 项，1 skipped（可选依赖 openai）
python -m decision_poc.evidence_check    # 108/108，指纹 b0a3ecbead0bb678
```
