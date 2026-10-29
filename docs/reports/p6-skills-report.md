# P6 回传（第二部分）：五个 Skill 与编排

对应任务单 P6 第 1 项「实现 5 个 Skill 的输入输出和工具编排」。

产物：`src/decision_poc/skills.py`、`tests/test_skills.py`（25 项）、`samples/nexent/skills.json`（绑定表）。全量 **319 passed**。

---

## 1. 为什么 Skill 放在本仓库，而不是只写 Nexent 配置

两种做法都能"跑通"，区别在于出问题时能不能定位：

| 做法 | 后果 |
| --- | --- |
| 只在 Nexent 侧写 prompt + 绑工具 | Skill 行为只能靠联调验证；换个模型、改一句 prompt，判定逻辑跟着变，且没有回归测试 |
| 本仓库落实现，Nexent 只管 prompt 与工具映射 | 五个 Skill 的输入输出与降级规则可被 pytest 断言；Nexent 侧配置改坏只影响措辞，不影响结论 |

选后者。划分边界：

- **本仓库**：Skill 的请求契约、调用顺序、降级与终止条件、审计闸门。
- **Nexent 侧**：prompt 文案、模型选择、会话记忆、部署参数。
- **谁都不写**：业务规则（在 `samples/rules/urban_deduction_rules.json`，带原文行号）。

`skill_manifest()` 从运行时的同一批对象导出绑定表，所以"文档说的"和"代码跑的"不可能不一致——不一致时测试会先失败。

## 2. 五个 Skill

| # | Skill | 阶段 | 输入 | 输出要点 | 失败/降级 |
| --- | --- | --- | --- | --- | --- |
| 1 | `event_understand` | understand | `event_id` / `as_of` / `caller_labels` | 片区、道路、深度、响应等级、预警等级、规则覆盖、数据缺口、同片区过期依据数 | 事件不存在 → `NOT_FOUND`（整链终止）；量级缺失 → `partial` |
| 2 | `impact_analysis` | impact | 同上 + `max_hops` / `min_confidence` | 受影响对象（分类型）、道路、片区、涉及部门、三元组数 | 范围被权限过滤 → `partial` + 原因码 |
| 3 | `resource_check` | resource | 同 1 | 需求/库存/缺口、折算依据逐条 | 参数规则不可用 → `partial`，缺口留空**不填 0** |
| 4 | `cross_department_deduction` | deduce | 同 1 | 六跳逐步状态、三类冲突、处置顺序、二次风险、转人工理由 | 依据越权 → `PERMISSION_DENIED`（不删证据后照给结论）；有跳不足 → `partial` |
| 5 | `evidence_audit` | audit | 同 1 + `persist` | 逐条 claim 是否过闸、引用完整率、账本记录号与哈希、链校验 | 任一 claim 不达标 → `EVIDENCE_INSUFFICIENT`，**不落账** |

统一信封：五者都返回 MCP 那套 `ToolResult`（`status` / `data` / `error` / `stats` / `context`）。`data` 固定含 `skill`、`stage`、`payload`、`degraded`，降级时多一个 `notice`。

一条与信封有关的事实：`ToolResult` 规定 `partial` 不能带 `error`。这不是缺陷，是刻意的——**"降级"和"失败"是两件事**。降级信息放 `degraded` + `notice`，调用方据此决定是否继续；只有"输入不可用 / 依据越权 / 审计不过"才是 `error`。

## 3. 编排：谁能继续，谁必须停

`SkillPipeline.run()` 固定顺序，逐跳判断：

| 情形 | 处理 | 理由 |
| --- | --- | --- |
| 事件不存在 / 不是事件对象 | 立即终止，`status=error` | 后面四步全部无从谈起，继续跑只会产出编造内容 |
| 影响范围被过滤或为空 | 继续，记降级 | 影响范围不完整不妨碍给出"受限的"结论，但必须标注 |
| 资源测算参数缺失 | 继续，缺口未知 | 缺口填 0 是最危险的错误：看起来资源充足 |
| 推演依据越权 / 有跳不足 | 终止，`status=partial` | 越权时不能"删掉证据再给结论"；不足时结论已在推演里标了 `insufficient` |
| 审计拒绝任一 claim | 终止，`publishable=False` | 引用断了就不能对外发布 |

`PipelineReport.publishable` 是 Demo 层唯一允许直接信任的开关：`status=="ok"` 且审计零拒绝且没有终止原因。其余情况一律先给人看。

## 4. 实测（`WO-20261029-007` @ 2026-10-29T09:40+08:00，`ORG-EMERG`）

```
[1 事件理解]  片区 A-03  道路 R-101  深度 30.0 cm  响应 orange  预警 orange
              ! 缺少积水面积，封路规则的“面积≥200m²”分支永不参与判定
[2 影响分析]  受影响 18 个对象  drainage=6 gas=1 incident=2 power=1 pump_station=3 road=4 work_site=1
              道路: R-101, R-103, R-105, R-201
[3 资源检查]  RES-PUMP-M: 需 7 / 可用 6 / 缺口 1 台
              - DF-PS-01 故障，按额定 1200 × 0.8 = 960 m³/h 折算，÷ 400 m³/h 向上取整 = 3 台移动泵
              - 1 处开挖基坑 × 2 台 = 2 台
              - 其他片区 1 个 × 2 处低洼点 × 1 台 = 2 台
[4 跨部门推演] C1 overlap 封路权限 ORG-ROAD,ORG-TRAFFIC / C2 gap DF-PS-01 / C3 contention RES-PUMP-M
              9 步处置顺序，隔离风险源序位在启动泵站抽排之前；5 条二次风险（含暴露期×预警重叠）
[5 证据审计]  决策 DEC-WO-20261029-007-20261029T0940  claim 14/14 通过  引用完整率 1.00
              账本记录 PV-0001-13ca217b  链校验 通过
需人工接手: 职责空白待明确 DF-PS-01 / 使用了无原文数值的演示假设，需现场核实
```

`publishable=True` 与 `handoff_required=True` 同时成立是正常的：**结论本身可发布（引用完整），但处置需要人拍板（职责空白、演示假设）**。把两者混成一个布尔值，会让"引用完整"替"业务决策"背书。

## 5. 两个真问题

**① 规则"无法判定"被读成了"不适用"**

把工单描述里的「积水深约 30cm」删掉后，封路规则不匹配，于是**封路动作从处置顺序里静默消失**，而推演仍报 `ok`。这不是降级，是假装问题不存在。

修法：给 `DeductionRule` 的阈值条件加 `_missing_inputs()`，输入缺失（`depth_cm is None`）时该规则进 `undecided` 集合，产出一条 `status=insufficient` 的步骤：

```
S6-action-undecided  undecided: RULE-ACT-CLOSE-ROAD|area_m2,depth_cm
                     detail: 以下处置规则因输入缺失无法判定（不是不适用）：RULE-ACT-CLOSE-ROAD 缺 area_m2,depth_cm
```

现在同一场景输出：understand `partial(no_depth)` → deduce `partial(insufficient_stages)` → `publishable=False`，且处置顺序里**没有**封路动作（不虚构），但明确写了为什么没有。

测试断言三件事：`RULE-ACT-CLOSE-ROAD` 出现在 `undecided`、缺的字段被点名、动作列表里没有"封闭积水路段"。

**② 深度解析不能有第二套口径**

第一版 `UnderstandSkill` 自己去读 `obj.attributes["depth_cm"]`，结果读出 `None`——深度在工单描述文本里，引擎用正则解析。同样的输入，Skill 说没深度、推演说 30cm，整条链会自相矛盾。

改成直接取引擎的 `EventContext`（`runtime.deduce(...).root`），解析口径只有一处。

## 6. 权限判定收成一份实现

`evidence_locate`（P5）与四个 Skill 都要判断"这条依据对调用方是否可见"。原来 MCP 里有一份，Skill 再写一份就必然漂移——权限判断是唯一不能有第二份实现的地方。

新增 `governance.blocked_by_visibility(refs, caller_labels, visibility)`，MCP 的 `_visibility_blocked` 与 Skill 的 `runtime.blocked_assets` 都调它。P5 的越权测试（`ORG-ROAD` 取排水规程正文被拒）继续通过，说明重构没改语义。

当前实测：`ORG-ROAD` 调整链 → 影响范围照常返回（可见部分），`resource_check` 与 `cross_department_deduction` 均 `PERMISSION_DENIED`，`audit` 不执行，`publishable=False`，终止原因写明"推演引用了对当前调用方不可见的依据"。

## 7. 与 MCP 入口的一致性

Skill 在进程内直调模块，MCP 走 JSON-RPC。两条路径必须给同一个结论，否则"两个入口两个产品"。

`test_skill_deduction_agrees_with_the_mcp_tool` 比对：快照哈希、冲突编号序列、资源缺口数值、处置顺序逐项相等。这条测试的价值在于——**以后任何一边改了推演参数，它立刻失败**。

## 8. 边界（写在前面，避免答辩被问倒）

1. **没有 prompt，也没有模型**：五个 Skill 是确定性代码。Nexent 的 prompt 属于它那一侧，这里不假装拥有。
2. **`caller_labels` 由调用方自报**：本地 stdio 下信任边界是本地进程，可接受；上 HTTP 必须换成服务端签发的身份，否则权限过滤可被自报绕过（P5 报告已列此风险）。
3. **审计只覆盖推演产出的 claim**：语义层查询结果本身不落账。原因是查询是只读检索，不构成"结论"；一旦给检索也发 claim，账本会退化成访问日志。
4. **`publishable` 不等于"建议正确"**：它只保证"每个结论都能指回文件与行号"，业务合理性仍需人判断。
5. **进程内不再走 MCP**：15 个资产规模下起子进程只是多加一个故障点；`mcp_tools` 字段声明的是外部编排时的绑定，二者用测试对齐而非用 IPC 对齐。

## 9. 复现

```powershell
$env:PYTHONPATH = "src"

# 五个 Skill 顺序执行（文本）
uv run --locked python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --caller-labels ORG-EMERG

# 越权身份：影响范围可见、推演被拒、不可发布
uv run --locked python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --caller-labels ORG-ROAD

# 落账到目录（可跨进程复核链）
uv run --locked python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --ledger artifacts/ledger

# 重新生成 Nexent 绑定表
uv run --locked python -m decision_poc.skills --manifest > samples/nexent/skills.json
```
