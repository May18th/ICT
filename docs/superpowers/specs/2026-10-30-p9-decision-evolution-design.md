# P9 决策进化闭环设计：跨片区抽排资源分配策略

- 日期：2026-10-30
- 角色：Codex（方案规划与评审）
- 上游依据：`docs/ICT-创新赛-赛题3-方案与实施计划.md`、`docs/innovation-and-evidence.md`、`docs/urban-scenario-ground-truth.md`、`docs/superpowers/specs/2026-10-29-urban-twin-trilogy-design.md`
- 已确认前提：用户明确要求补齐"推演反馈 → 策略调整 → 下次更优"闭环；反馈信号使用**合成数据 + 冻结 ground truth**（受控评测环境，不宣称真实市政数据回流）。

## 1 要解决的缺口

前三阶段（P3 知识演化、P6 推演、P8 评测）各自成立，但没有一条端到端的"决策进化"证据：

- P3 演化的是**知识**（三元组），不是策略；
- 通用内核的 `record_feedback → evolve → 评测门禁` 只跑 `TestModel` 演示，没接城市推演；
- P8 的 17/22 → 22/22 是**代码修复**（加时态护栏），答辩时会被追问"这是智能体自己进化吗"。

赛道 3 要的回答是固定句式：**从哪一次推演的结果、基于什么信号、调了什么策略、下一次偏差缩小多少。** P9 就是把这句话变成可运行、可复算的数字。

## 2 为什么选"资源分配"作为决策对象

现有六跳只回答"缺多少台"，不回答"这几台先给谁"。缺口的处置口径散落在 CF2 反事实里（优先保 A-03），但**没有形成一个可版本化、可评测、可回滚的决策策略**。

- 它是真实决策：零和分配，给 A 就意味着 B 少；
- 它有文档依据：`urban-scenario-ground-truth.md` 跳 5（需求 5+2、库存 4+2、跨区 40 分钟）与 CF2（"只能保一处，优先 A-03"）；
- 它有可量化反馈：分配向量、关键片区未满足量、应调拨未调拨次数、优先序；
- 它适合确定性进化：策略参数是有限维离散/连续小参数，不需要在线训练模型。

明确边界：

- **不引入大模型**。推演与分配全部确定性可复算，分配策略只调参数，不生成自然语言。
- **策略只决定"怎么分"，不改推演结论**。六跳输出（需求、缺口、冲突、序位）是策略的输入，不是被优化的对象。
- **不接真实数据**。评测场景是受控合成集，逐条标注依据条款；真实事件（WO-20261029-007）用于端到端演示，不进入进化训练集。

## 3 数据流

```
SkillPipeline.run(event)                       # 现有五 Skill
  └─ DeductionResult.decision_inputs           # 新增：按片区归集的决策输入
       DistrictsSignal{district, depth_cm, closure_required, has_hazard,
                       demand, local_available, world_required}
                    │
                    ▼
       AllocationPolicy.decide(signals)         # 新增：策略版本 vN
                    │
                    ▼
       AllocationDecision{allocation_by_district, transfers[], unmet,
                          priority_order, rationale, policy_version}
                    │
                    ▼ 与冻结 ground truth 比
       DeviationReport{deviation, attribution{维度: 值}, 分项明细}
                    │ 归因到最差维度
                    ▼
       候选策略（按维度生成）→ 评测门禁（训练集提升 + 留出集不退化）
                    │ accept / reject
                    ▼
       VersionRegistry（propose → promote / reject → rollback）
```

## 4 决策输入（推演侧新增，向后兼容）

`deduction.py` 的 `_resource_plan` 已经在算每个片区的需求、库存、深度与封路判定，P9 只把它显式暴露出来，不改计算逻辑：

```python
class DistrictSignal(StrictModel):
    district: str
    depth_cm: float | None = None          # None = 未解析出深度，不按 0 处理
    closure_required: bool | None = None   # None = 深度未知，不判定封路
    has_hazard: bool = False               # 受影响范围内含燃气/电力
    demand: int = 0
    local_available: int = 0
    world_required: int = 0                # 该次推演全部片区合计需求
```

- `DeductionResult.decision_inputs: dict[str, DistrictSignal] = {}`（默认空，不改变既有序列化语义）；
- `DeductionView.decision_inputs` 同步暴露，供 Skill/MCP 编排层直接消费；
- 资源测算失败（`_safe_resource_plan` 的 error 分支）时 `decision_inputs` 为空——**策略层因此拿不到输入，必须显式拒绝出方案，不能拿空信号分资源**。

## 5 策略模型

```python
class AllocationPolicy(StrictModel):        # frozen
    policy_version: str = "v1"
    weight_closure: float = 0.0            # 已过封路阈值
    weight_hazard: float = 0.0             # 受影响范围含燃气/电力
    weight_depth: float = 0.0              # 水深（按 depth_scale_cm 归一）
    depth_scale_cm: float = 25.0           # 归一基准，取封路阈值，不参与进化
    transfer_penalty: float = 1.0          # 跨区每台的优先序差值门槛
    allow_transfer_from_critical: bool = False
```

优先序分值：

```
score(d) = weight_closure * closure(d)
         + weight_hazard  * hazard(d)
         + weight_depth   * min(depth(d), scale) / scale
```

分配算法（确定性，无随机）：

1. 各片区先用本区库存满足本区需求：`allocated = min(demand, local_available)`；
2. 未满足片区按 `(score 降序, 片区名升序)` 处理；
3. 调出方按 `(score 升序, 片区名升序)` 扫描，跳过本片区、跳过关键片区（关键 = 已过封路阈值或有次生危险源，除非策略显式允许）、跳过优先序差 < `transfer_penalty` 的组合；
4. 调出方可动用"未用库存 + 已分配给自己但优先级更低的库存"，因此**允许低风险片区降到 0**（这正是 CF2"只能保一处"的口径）；每次调拨记账 40 分钟跨区时延（`urban-scenario-ground-truth.md` 跳 5）；
5. 全部扫完后仍未满足的，写入 `unmet` 并按优先序列出"需上报跨区增援"，不四舍五入成"已满足"。

`v1`（初始策略）= 所有权重 0 + 不调拨 = "各片区各自用本区库存"。这是没有策略时的默认做法，不是人为设弱的稻草人：`urban-scenario-ground-truth.md` 跳 5 的缺口就是按"本区 5 台 vs 本区 4 台"算出来的。

## 6 偏差信号与归因

冻结 ground truth 存于 `samples/eval/allocation_cases.json`，每个用例给出：

```python
class AllocationCase(StrictModel):
    case_id: str
    title: str
    split: Literal["train", "holdout"]
    basis: list[str]                 # 依据条款（Ground Truth 章节 + 数据集字段）
    signals: list[DistrictSignal]
    expect: AllocationExpect
```

`AllocationExpect` = `allocation_by_district` / `transfers[{from,to,qty,delay_min}]` / `priority_order`。期望值由第 2、5 节的文档口径人工推得，不由实现反推。

四个偏差维度（全部 ∈ [0,1]，越小越好）：

| 维度 | 口径 | 为什么需要它 |
| --- | --- | --- |
| `allocation_l1` | Σ\|期望−实际\| / (2×Σ需求) | 分配总量层面的偏差 |
| `critical_unmet` | 关键片区未满足量差 / 关键片区需求 | 只看总量会漏掉"把泵给了低风险片区" |
| `transfer` | 期望调拨量未被执行的占比（期望为空时，出现多余调拨记 1） | 决策动作层面的偏差，最直接 |
| `order` | 优先序的归一化 Kendall 距离 | 排序错但分配碰巧对时仍能测出 |

综合偏差 = `0.4*l1 + 0.3*critical_unmet + 0.2*transfer + 0.1*order`（权重写进代码常量，报告披露）。

`attribution` 返回四维原值，进化取**最差维度**作为本轮改进目标——这就是"偏差归因"，也是避免盲搜的依据。

## 7 进化循环

```python
def evolve(registry, train, holdout, *, max_rounds=4) -> EvolutionReport
```

每轮：

1. 用当前 active 策略评测训练集与留出集，得到 `before`；
2. 取 `before.attribution` 最差维度 `dim`；
3. 按 `dim` 生成候选参数补丁（固定顺序的候选表，见 `CANDIDATE_PATCHES`）；
4. 每个候选在同一训练集上评测，取训练偏差最小者；
5. 过评测门禁（复用 `evolution_gate.evaluate_and_apply`）：
   - `passed = train_deviation < 当前训练偏差` **且** `holdout_deviation <= 当前留出偏差 + 0.02`；
   - 不通过 → `rejected`，版本历史留痕，**保留旧版本**；
6. 通过 → `promoted`，记一轮 `EvolutionRound`（前后偏差、参数变更、依据维度、反馈指纹）；
7. 连续无提升（门禁拒绝且候选已遍历）→ 收敛，停止。

三个必须做对的点：

- **留出集不参与选择**：候选只在训练集上挑，留出集只用于门禁的非退化检查。否则"进化"变成对评测集的过拟合，答辩一问就穿。
- **反馈指纹**：每轮记录用例集的规范化 JSON 的 sha256（`feedback_digest`），报告可复算校验。反馈数据若被改动，指纹对不上——这是"溯源服务进化"的最小落地，不宣称区块链。
- **可回滚**：进化结束后执行 `rollback(v1)`，偏差必须回到初始值（硬断言），再回滚到最终版本验证恢复。

## 8 验收口径

- [ ] `DeductionResult.decision_inputs` 对 WO-20261029-007 给出 A-03/B-01 两个片区的深度、封路判定、危险源、需求、本区库存；既有 370 项测试不变。
- [ ] v1 策略在训练集上：关键片区未满足 > 0，应调拨未调拨 ≥ 1 → 初始偏差非 0。
- [ ] 进化后训练集综合偏差下降 ≥ 50%，留出集同向下降（不退化）。
- [ ] 至少一轮被门禁拒绝，且拒绝后 active 版本不变。
- [ ] `rollback(v1)` 后偏差逐位回到初始值。
- [ ] 同一进程跑两遍 `evolve()`，轮次、参数、偏差完全一致（确定性）。
- [ ] 报告给出"进化前 vs 进化后"四项指标与逐轮明细，数字由命令生成，不手写。
- [ ] 真实事件端到端：同一份推演结果下，v1 与进化后策略给出不同分配（A-03 由缺 1 台变为缺 0 台，B-01 由缺 0 变为缺 1 台），并注明 40 分钟跨区时延。

## 9 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 评测场景是合成的，评委质疑"自己出题自己答" | 可信度 | 每条用例写 `basis` 指向 Ground Truth 章节与数据集字段；真实事件单独做端到端演示；报告明确写"受控评测环境" |
| 进化 = 一次参数穷举，被说成"不是智能" | 创新性表述 | 表述为"偏差归因 + 受控搜索 + 门禁"，不宣称在线学习；强调留出集门禁与回滚是工程可信点 |
| 调出关键片区导致风险扩散 | 决策安全 | 策略默认 `allow_transfer_from_critical=False`，关键片区不可被调出；报告把该约束单列 |
| 浮点权重导致结果不确定 | 可复现 | 偏差四舍五入 6 位；候选表固定顺序；同进程重复运行一致 |
| 资源测算失败时"空信号"被当 0 分配 | 错误决策 | `decision_inputs` 为空时 `decide()` 抛 `AllocationUnavailable`，不给方案 |

## 10 模块归属

```
src/decision_poc/
├─ deduction.py            +DistrictSignal / DeductionResult.decision_inputs（追加，不改既有字段）
├─ skills.py               +DeductionView.decision_inputs（追加）
├─ allocation.py           新增：策略、分配、偏差、用例加载、CLI
├─ policy_evolution.py     新增：进化轮次、候选表、门禁、报告（Markdown）
├─ experience.py           新增（P9.1）：分层决策记忆、场景签名、人工评审 → 评审用例
└─ mcp_server.py           +allocation_plan（第 9 个 MCP 工具）
samples/eval/
├─ allocation_cases.json   新增：12 例（8 训练 / 4 留出），每例带依据
├─ decision_reviews.json   新增（P9.1）：人工评审样例（角色扮演，1 条 MODIFY）
├─ decision_memory.json    生成（P9.1）：分层记忆落盘，跨进程复用
└─ p9_evolution.json       生成：双会话进化快照
tests/
├─ test_allocation.py      策略边界、确定性、无输入拒绝、真实事件分配
├─ test_policy_evolution.py 偏差下降、门禁拒绝、回滚、确定性、版本标签、warm start
└─ test_experience.py      场景签名、分层记忆、持久化、评审用例
```

## 11 P9.1：分层决策记忆（跨会话沉淀）

问题：P9 的进化只在单进程内成立——每次从 v1 起步，跑完即弃。要证明策略是"沉淀下来的资产"，
需要第二次遇到同类场景时不用重搜。

### 11.1 场景签名（相似度，而非事件编号）

```python
class ScenarioSignature(StrictModel):
    districts: list[str]          # 涉及片区
    shortage_ratio: float         # 供不应求用例占比（资源紧张程度）
    critical_mix: list[str]       # 关键状态组合（closure/hazard/none 的每例模式）
    cases: int

def similarity(a, b) -> float:    # 0.4*片区Jaccard + 0.3*紧张度接近 + 0.3*关键组合Jaccard
```

不用事件编号/时间做键：那是"同一件事"，不是"同类场景"。

### 11.2 三层记忆与召回

```python
class MemoryScope(StrEnum): TENANT / USER / AGENT     # 越具体越优先：agent > user > tenant

class ExperienceEntry(StrictModel):
    entry_id, scope, signature, label, policy_version,
    params, outcome{deviation, allocation_l1_units, critical_unmet_units, missed_transfers},
    feedback_digest, event_ids, review_ids, note

class DecisionMemory:            # 追加式，不删除、不改写
    remember(entry) → 幂等（同 entry_id 返回既有条目）
    recall(signature, min_similarity=0.6, limit=3) → 相似度降序、scope 优先级、entry_id
    warm_start(signature) → 相似度达标条目中"实际偏差最小"者（已验证最有效）
    save(path) / load(path)      # 跨进程
```

`warm_start` 的排序规则固定（偏差 → 相似度 → entry_id），保证同一记忆库必然给出同一起点。

### 11.3 与 Nexent 分层记忆的关系（表述红线）

- 这是**离线同构契约**：Tenant/User/Agent 三层与 Nexent 记忆分层对齐，但**未在真实平台真机验证**；
- 接入平台时只需把 `remember / recall` 换成记忆 API，进化与门禁逻辑不变；
- 报告与答辩必须写"离线同构，未真机验证"，不得写成"已接入 Nexent 记忆"。

### 11.4 证据形态

第二次会话直接从记忆 warm start：起点偏差 `0.0000`（第一次是 `0.2264`），只需 1 轮收敛探针、
0 次采纳——"经验沉淀"因此是可断言的数字，而不是一句设计说明。

## 12 P9.1：人工评审（第二进化信号）

问题：纯模拟反馈会被质疑"不够真实"。评审环节作为第二信号接入，且必须可断言。

```python
class ReviewRecord(StrictModel):
    review_id, reviewer_role, reviewer_note="角色扮演，非真实部门批复",
    event_id, as_of, policy_version_reviewed,
    decision: accept | modify | reject,
    reason, signals: list[DistrictSignal],
    expected_allocation: dict[str, int], basis: list[str]
```

规则：

1. **只有 `modify` 且给出修正分派的评审**能转成评审用例进入门禁；`reject` 只留痕，不编造期望。
2. 评审用例只评"谁拿几台"：`transfer` 与 `order` 两个维度在 `case_deviation` 中置 0
   ——评审没看过的指标不能让它背。
3. 门禁条件增加一项：`review_deviation == 0`。候选违反人工结论即拒，active 版本不变。
4. 评审用例是**自包含**的（含当时决策输入快照），下一轮评测不必回跑推演，别人可直接复算。
5. 演示中的评审是**角色扮演**，`reviewer_note` 强制写明，避免被当成真实部门批复。

## 13 P9.1：语义化版本标签（可审计的进化制品）

每次采纳产出一条发布记录，进入版本历史并随报告输出：

```python
class PolicyRelease(StrictModel):
    label            # flood-response-v1.N（基线 flood-response-v1.0，每次采纳 +1 minor）
    version_id       # 内部版本 v2/v3
    parent_label     # 父标签，warm start 时继承记忆里的标签
    trigger_feedback_id   # 触发的反馈事件（p9-round-N）
    trigger_review_ids    # 生效中的人工评审
    params_delta     # 本次改了哪些参数
    expected_improvement  # 预期偏差下降值（实测）
```

标签只在门禁采纳后产生；被拒的候选只进版本历史（`rejected:vN`），不产出标签——"拒绝也留痕"。

## 14 P9.1 验收口径（追加）

- [x] `run_showcase()` 两次会话：第二次 `warm_start` 命中，起点偏差 0.0000、轮次少于第一次。
- [x] 记忆落盘后 `DecisionMemory.load()` 可召回；`reuse_memory=True` 时第一次运行即 warm start。
- [x] 人工评审使门禁新增硬约束：违反评审的候选全部被拒，active 版本不变（构造不可满足评审用例验证）。
- [x] 评审用例只评分配：`transfer`/`order` 维度恒 0；`modify` 缺修正分配直接校验失败。
- [x] 策略标签链 `v1.0 → v1.1 → v1.2`，触发反馈 ID 与评审 ID 可核对；被拒候选无标签。
- [x] 两次 `run_showcase()` 结果逐位一致（确定性）；报告写清"记忆离线同构、评审角色扮演"。

## 15 P9.2：浏览器进化对比面板

定位：**展示层，不产出数字**。面板的唯一职责是把 `run_showcase()` 已经算好的双会话证据放到一屏，
供评委自己点、自己核。任何"为了好看在这里补一个数"的做法都会让面板与报告出现两套口径，禁止。

### 15.1 与现有 Web Demo 的关系

`web_demo.py` 是"一次推演 → 证据链"的页面，P10 可视化工具层也在改它。为避免并行开发互相踩，
进化面板独立成 `evolution_demo.py`，`web_demo.serve()` 只增加两个路由：

```
GET /evolution      → evolution_demo.build_evolution_html()
GET /api/evolution  → evolution_demo.build_evolution_payload()（JSON）
```

推演面板右上角加一个「决策进化对比 →」链接，其余不动。

### 15.2 数据契约

```python
def build_evolution_payload(showcase: ShowcaseReport | None = None) -> dict
# schema_version: evolution-demo-1
# first.{initial,final}_{train,holdout}.deviation / rounds / releases / final_label
# second.warm_start{entry_id,label,similarity,source_outcome} / initial_train / rounds
# review{review_id,reviewer_role,decision,reason,expected_allocation,deviation_before/after,reviewer_note}
# memory.{entries,path} / real_event.{baseline,evolved} / boundaries[3]
```

约束：

1. 所有数字来自 `run_showcase()` 的模型 dump，展示层不做任何计算（只做格式化）；
2. 同一进程两次请求返回逐位一致（`run_showcase` 确定性）；
3. 三条表述边界（受控反馈 / 评审角色扮演 / 记忆离线同构）由服务端下发并固定渲染在页脚，
   避免讲者省略；
4. 零外部依赖：内联 CSS/JS，断网可开。

### 15.3 验收

- [x] `GET /evolution` 返回 200 且包含六块面板锚点；`GET /api/evolution` 返回 `evolution-demo-1`。
- [x] 面板数字与 CLI 报告一致（0.2264→0.0000、0.2182→0.0000、评审 0.1171→0.0000、A-03 4→5 / B-01 2→1）。
- [x] 同一输入两次 `build_evolution_payload()` 逐位一致。
- [x] 推演面板存在跳转链接；面板页有返回链接。
