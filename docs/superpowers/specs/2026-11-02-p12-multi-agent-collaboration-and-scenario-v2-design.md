# P12 多智能体协同 + 内涝场景 v2 设计规格

- 日期：2026-11-02
- 角色：Codex（方案规划与评审）
- 下游：CodeArts（按任务单实现）
- 上游输入（用户本轮明确决策）：
  1. **需要多个智能体**（不是"单 Agent + 角色化文案"）；
  2. **地下通道倒灌、排水泵站负荷升高两项都按新定义重算**，接受既有对外数字漂移。
- 上游依据：`docs/ICT-创新赛-赛题3-方案与实施计划.md`、`docs/superpowers/specs/2026-10-29-urban-twin-trilogy-design.md`、
  `docs/superpowers/specs/2026-11-02-p11-ablation-and-anchoring-design.md`、`docs/urban-scenario-ground-truth.md`、
  `docs/innovation-and-evidence.md`、`blender-ue-scene/docs/semantic-binding.md`

## 0 一句话

**多 Agent 管"协同"，单内核管"断言"**：5 个部门 Agent + 1 个总控 Agent 在协同板上产出动作草案与联合发布建议；
任何进入对外结论的断言仍只能由 `event_deduce` / `conflict_check` / `allocation_plan` / `provenance_verify` 生成。
多 Agent 因此不破坏"六跳路径正确率可计算、引用完整率 100%、同输入逐位一致"这三条已冻结口径。

## 0.1 三个现实问题 → 三项能力 → 可核实证据

作品要解决的不是三个并列功能，而是同一条链的三段：**看懂 → 推演 → 证伪**。多智能体是中间那段"推演"的组织形式。

| 现实问题 | 对应能力 | 技术落点 | 现状可核实证据 |
| --- | --- | --- | --- |
| 城市设施数据分散、彼此关系不清楚 | 基础设施语义知识图谱 | `TwinObject` + `SemanticTriple` + `as_of` 时态 + `snapshot_hash` 锚定 | 16 个异构资产统一登记（`asset_id+sha256+locator`）、102 条语义三元组 / 8 类谓词 |
| 各部门独立分析、缺乏协同 | 多智能体协同推演 + 任务分配 | 1 总控 + 5 专业 Agent + 协同板 + 内核冲突/分配 | C1 权限交叉 / C2 职责空白 / C3 资源争用全部保留；P9 分配 A-03 5 台 / B-01 1 台 |
| AI 给出结论后无法说明依据 | 数据来源与决策过程溯源 | claim 引用闸门 + append-only 哈希链 + `verify` / `replay` | 引用完整率 100%、篡改可定位断点、replay 给出最小证据集 |

三条能力的失败形态必须可测（沿用既有定义）：语义层与孪生状态不同步 → 结论引用已失效事实；
协同层让冲突被"综合判断"抹平 → 跨部门方案退化为单部门清单；溯源层只到"某个文档"级别 → 无法证伪。

## 1 目标与非目标

### 1.1 目标

| 编号 | 目标 | 可验收 |
| --- | --- | --- |
| G1 | 6 个 Agent 真跑（总控 1 + 部门 5），有协同轨迹与冲突汇总 | `CollabTrace` 含每角色调用与交接 |
| G2 | 部门 Agent 只产出 `ActionCard`，不产出 claim | 构造期校验：`ActionCard` 无 `evidence_refs` 之外的断言字段 |
| G3 | 多 Agent 最终结论 = 单内核结论 | 逐位一致 8/8，不一致即失败 |
| G4 | 地下通道倒灌进六跳推演（不只是场景） | ground-truth v2 跳 2/5/6 命中 |
| G5 | 泵站"负荷升高"成为可计算口径 | 缺口由公式重算，不再用固定 80% 折算 |
| G6 | 对外数字一次性重算并重新冻结 | `evidence_check` 全绿 + 五处同步 |

### 1.2 非目标（本阶段不做）

- 不做"多 Agent 各自进化"：进化仍只在策略层（P9），理由是局部反馈无法独立归因，这条保持。
- 不做 Agent 间自由对话协商：协同走结构化黑板 + 规则裁决，不做开放式 chat 互调（不可复现）。
- 不引入真实图数据库、不接真实 GIS、不改 `DecisionRequest` / `DecisionOutput` 语义。
- 不追求多 Agent 更快：多 Agent 的收益记为"协同覆盖度"，不是时延。

## 2 总体架构

```
┌──────────── 协同编排层（新增 src/decision_poc/collab/） ────────────┐
│  EMERG-COORD 总控 Agent                                             │
│    ├─ ORG-DRAIN   水务/排水处置                                     │
│    ├─ ORG-ROAD    道路封控                                          │
│    ├─ ORG-TRAFFIC 交通管制与绕行                                     │
│    ├─ ORG-GAS     燃气/基坑风险                                      │
│    └─ ORG-POWER   供电与资源保障                                     │
│  Blackboard：事件上下文 / ActionCard[] / ConflictSet / 裁决记录      │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ 只读调用已有 MCP 工具（按角色分组白名单）
┌───────────────────────────▼──────────── 确定性内核（不改语义） ─────┐
│ semantic_query · twin_neighbors · evidence_locate · claim_trace      │
│ provenance_verify · event_deduce · counterfactual_run                │
│ conflict_check · allocation_plan · scene_render · scene_compare      │
│ asset_lookup                          （现有 12 个，新增 collab_run）│
│ 六跳推演 + 三类冲突 + 哈希链溯源 + P9 分配/进化                       │
└──────────────────────────────────────────────────────────────────────┘
```

平台装配两条路径，先做 B：

| 路径 | 做法 | 前置 |
| --- | --- | --- |
| **B（默认，先做）** | Nexent 只挂 1 个总控 Agent，调 `collab_run` 一次拿全部角色动作 + 协同轨迹 | 无需平台新能力 |
| A（探索） | Nexent 上建 5 个部门 Agent + 总控，真机 Agent 互调 | **需验证 Nexent v2.7 是否支持 Agent-as-Tool；仓库现无证据，标注"无法确认"** |

## 3 Agent 花名册与契约

### 3.1 花名册

| Agent | 部门 | 职责 | 可见工具组 | 典型 ActionCard |
| --- | --- | --- | --- | --- |
| `EMERG-COORD` | ORG-EMERG | 定优先级、触发推演、汇总冲突、出最终协同方案 | 全部 | `coordination_plan` |
| `ORG-DRAIN` | ORG-DRAIN | 雨水口清掏、泵站补位、移动泵与通道抽排需求 | `role_drain` | `drain_pump_request` |
| `ORG-ROAD` | ORG-ROAD | 封控路段、围挡、封路时序 | `role_road` | `road_closure` |
| `ORG-TRAFFIC` | ORG-TRAFFIC | 绕行组织、管制点、校峰避让 | `role_traffic` | `traffic_control` |
| `ORG-GAS` | ORG-GAS | 基坑保护、停气降压、浓度检测 | `role_gas` | `gas_protection` |
| `ORG-POWER` | ORG-POWER | 泵站备电、浸水断电 | `role_power` | `power_guarantee` |

### 3.2 ActionCard（新增 StrictModel）

```python
class ActionCard(StrictModel):
    card_id: str
    agent_id: str                     # 出卡角色
    stage: Literal["affected", "owner", "conflict", "resource", "action", "residual_risk"]
    action: str                       # 动作标识，如 close_underpass / dispatch_pump
    targets: list[str]                # twin_id 列表，必须在语义层可解析
    quantity: int | None              # 台数等
    depends_on: list[str] = []        # 前置 card_id（顺序约束的来源）
    co_sign_required: list[str] = []  # 必须联合发布的部门（如 ORG-ROAD + ORG-TRAFFIC）
    evidence_refs: list[str]          # 非空，asset_id#locator
    claim_ids: list[str]              # 必须指向内核已产出的 claim，不允许自造
    status: Literal["proposed", "accepted", "rejected", "no_action"]
    no_action_reason: str | None      # status=no_action 时必填，用于"角色覆盖度"统计
```

硬约束（构造期校验，违反即抛异常）：

1. `claim_ids` 非空且每个 id 都能在内核 `DeductionResult` / `ProvenanceLedger` 中查到；
2. `evidence_refs` 非空且可在资产清单解析；
3. `targets` 每个 `twin_id` 都存在于语义层快照（`snapshot_hash` 锚定）；
4. `status=proposed` 的卡不得直接进入对外答案，必须经裁决。

### 3.3 协同板与裁决

```python
class CollabResult(StrictModel):
    event_id: str
    cards: list[ActionCard]
    conflicts: list[DepartmentConflict]   # 直接复用内核产物，不由 Agent 生成
    co_sign_pairs: list[tuple[str, str]]  # 需联合发布的部门对
    handoffs: list[RoleHandoff]           # 角色间结构化交接（非自由对话）
    merge_order: list[str]                # 裁决后的处置顺序
    kernel_fingerprint: str               # 内核结论指纹，用于一致性门禁
    snapshot_hash: str
    trace_path: str                       # artifacts/collab/<event>/trace.jsonl
```

裁决规则（顺序固定，不投票）：

1. 内核 `conflict_check` 给出的 C1/C2/C3 不可被 Agent 抹平，原样进 `conflicts`；
2. `co_sign_required` 非空的动作必须由全部列名部门各出一张卡才可 `accepted`，否则 `rejected` + 原因；
3. 处置顺序按 `depends_on` 拓扑排序，再套预案硬约束（见 §5）；
4. 拓扑冲突（环）或硬约束冲突 → 整体 `conflict` 状态，转人工确认点，不得自动取一个顺序。

**任务分配落在总控，不落在部门之间**：分配由总控依据内核 `conflict_check` / `allocation_plan` 的输出生成，
专业 Agent 只能 `accept` / `reject + reason`，不得互相派活。Agent 之间互派任务等于把编排逻辑交给 LLM，
既不可复现，也会让"资源缺口 2 台"这类可核数字变成协商产物。

### 3.4 为什么是 1 总控 + 5 专业，而不是 1 个或 N 个平级

| 判断 | 依据 |
| --- | --- |
| 必须有 1 个总控 | 跨部门冲突（C1 权限交叉）需要唯一裁决点；封路/封通道必须联合发布，没有牵头方就发不出去 |
| 必须是 5 个专业 | 现有职责台账只登记 5 个专业部门（ORG-DRAIN / ROAD / TRAFFIC / GAS / POWER）+ 1 个统筹（ORG-EMERG）。新增专业角色必须有独立台账 + 职责原文可定位，否则是空角色 |
| 不能是 N 个平级 | 平级 Agent 遇冲突只能协商或投票，两者都不可复现，且会把 C1/C2/C3 抹平——这正是我们要击败的"多智能体编排"类作品的主要缺陷 |

证明 1+N 不是摆设的三条，直接进验收：

1. **去重性**：每个专业 Agent 必须有本部门独有输入与独有输出；两个 Agent 产出完全相同的动作即合并角色。
2. **消融**：去掉任一专业 Agent，结论必须缺一块（缺哪一跳、缺哪条动作），且该缺失能被评测捕获。
3. **总控不可替代**：去掉总控后 `co_sign` 联合发布与处置顺序无法生成，整体应转 `conflict` 转人工，不得自动出方案。

## 4 三条边界（本设计成立的前提）

| 边界 | 内容 | 失守后果 |
| --- | --- | --- |
| **断言权** | Agent 不生成 claim；所有断言来自内核工具 | 引用完整率 100% 与"可证伪"叙事同时失效 |
| **裁决权** | 冲突与顺序由规则 + 预案硬约束决定，不由 Agent 协商或投票 | C1/C2/C3 会被"综合判断"抹平，正好是竞品被批评的点 |
| **复现口径分轨** | 内核指标要求逐位一致；多 Agent 层只要求"结论一致"，对话轨迹允许不同并整轨留痕 | 把 LLM 轨迹当可复现证据 → 现场第二次跑就对不上 |

复现口径具体分法：

| 对象 | 要求 |
| --- | --- |
| `event_deduce` / `conflict_check` / `allocation_plan` 输出 | 逐位一致（现有 `evidence_check` 口径不变） |
| `CollabResult.cards` 的动作集合与 `merge_order` | 与单 Agent 内核驱动结果一致（`kernel_fingerprint` 相等） |
| `CollabTrace` 的 LLM 文本、调用顺序 | 不要求一致，只要求落盘可查；离线测试用确定性桩替代 LLM |

## 5 场景 v2 之一：地下通道倒灌入推演

### 5.1 新增对象与数据

新增资产 `samples/assets/urban/underpasses.csv`，字段：

| 字段 | 含义 | 示例（天府广场下穿隧道，来自已建场景） |
| --- | --- | --- |
| `underpass_id` | 对象 ID | `UP-TIANFU-E` |
| `name` | 名称 | 天府广场下穿隧道东端 |
| `entry_sill_height` | 入口挡水槛高（m，相对路面） | `0.15`（**假设，待确认**） |
| `lowest_point_z` | 最低点标高（m） | `-1.81`（取自 S9 场景实测） |
| `ramp_length` | 敞开坡道长（m） | `96`（S9 场景） |
| `open_water_area` | 敞口汇水面积（m²） | `96×34 ≈ 3264`（由坡道与洞口宽推得，**量级口径**） |
| `has_pump_room` | 是否有泵房 | `false` |
| `close_threshold` | 通道封控水深阈值（m） | `0.25`（**与道路封控阈值同源，待确认**） |
| `owner_org` | 运维主体 | 留空 → 触发 C2 职责空白（沿用现有泵站写法） |

语义层新增 `ObjectKind.UNDERPASS`，`kind` 进入 `TwinObject`；对既有场景里的 `TN-TIANFU`（天府广场下穿隧道锚点）
补 `twin_id` 映射，使 3D 高亮与推演对象是同一个 id（当前两边 id 不统一，属遗留缺口）。

### 5.2 新增规则（`DeductionRule`，规则仍是数据）

| rule_id | stage | when | then |
| --- | --- | --- | --- |
| `RULE-RISK-BACKFLOW-UNDERPASS` | `residual_risk` | 外部积水深 `h_ext ≥ entry_sill_height` | `backflow_risk = high` |
| `RULE-RISK-BACKFLOW-UNDERPASS-MED` | `residual_risk` | `0.6×entry_sill ≤ h_ext < entry_sill` | `backflow_risk = medium` |
| `RULE-ACT-CLOSE-UNDERPASS` | `action` | 通道内水深 ≥ `close_threshold` 或 `backflow_risk = high` | `close_underpass`，`co_sign = [ORG-ROAD, ORG-TRAFFIC]` |
| `RULE-RES-PUMP-UNDERPASS` | `resource` | `has_pump_room=false` 且通道积水 | 移动泵台数 = `ceil(通道汇水量 / 单泵 400 m³/h)` |

**跳数不变**（仍六跳）。倒灌是"受影响对象 + 二次风险 + 处置动作"三跳里的新实例，不新增跳。
这是刻意的：跳数一变，"路径正确率"口径要重定义，代价远大于收益。

### 5.3 处置顺序硬约束（ground truth 跳 6 验收口径更新）

现行硬约束：隔离风险源必须早于抽排（输出"先抽排"视为引用废止预案）。
v2 追加两条：

1. **封闭地下通道不晚于道路封控**（生命风险优先）；
2. 通道封闭与道路封控若同时需要，`co_sign` 部门必须联合发布，单部门发布记为 `rejected`。

### 5.4 冲突归属

通道权属不明（地铁公司 / 城管 / 道路养护）**归入现有 C2 职责空白**，不新增 C4。
理由：验收口径"三类冲突必须全部命中"保持不变，避免评测分母膨胀；只是新增一个实例。

## 6 场景 v2 之二：泵站负荷升高口径

### 6.1 现行口径（v1，将被替换）

`capacity_available = rated × 0.8`（故障期固定折算）。DF-PS-01：1200 × 0.8 = 960 m³/h
→ 单台移动泵 400 m³/h → 需 3 台。这个 0.8 是拍的，无法解释"负荷升高"。

### 6.2 v2 口径：负荷体现为"来水 vs 能力"的缺口，不给人造衰减系数

```
failed_loss      = Σ 故障泵组额定能力            # DF-PS-01：1# 泵组 400 m³/h 待修
capacity_avail   = rated − failed_loss           # DF-PS-01：1200 − 400 = 800 m³/h
inflow           = 片区汇水面积 × 降雨强度        # 推理公式，量级口径，与 hydraulics-report.md 一致
pump_gap         = max(0, inflow + 通道需求 + 基坑需求 − capacity_avail)
mobile_pumps     = ceil(pump_gap / 400)           # 单台移动泵 400 m³/h
load_state       = normal | high_load | overload  # inflow/capacity_avail ≤0.85 / 0.85–1.0 / >1.0
```

优点：`load_state` 是可解释标签（答辩可讲），缺口是可计算公式（可重算），不再有拍出来的 0.8。
代价：DF-PS-01 可用能力从 960 降到 800，**A-03 需求必然上升，缺口从 1 台变成新值**，P9 分配数字随之漂移。

### 6.3 台账字段增补

`drainage_facilities.csv` 增列：`inflow_m3h`（当前来水）、`load_ratio`（计算得）、`load_state`、
`failed_unit_m3h`（故障泵组能力明细）。台账"额定值 + 故障明细"取代"额定值 + 隐含 0.8"。

## 6.4 物理与数学定量层（新增：解决"数字凭什么"）

现状：决策内核只从台账取值 + 算术。`blender-ue-scene/out/hydraulics-report.md` 做过稳态校核
（Manning 满流、推理公式 `Q=ψqF`、泵站能力对比），但它是**一次性报告，没进内核，也没被任何 claim 引用**。
本阶段把它做成内核里的确定性定量提供者。

### 能算且该进内核（确定性强、有规范出处）

| 计算 | 公式/方法 | 用途 | 数据前提 |
| --- | --- | --- | --- |
| 产流（设计流量） | 推理公式 `Q = ψ·q·F` | 把"80 mm/2h"变成片区来水 m³/h，支撑 §6.2 的 `inflow` | 暴雨强度 q（**成都系数需核实**）、汇水面积 F、径流系数 ψ |
| 管流能力与充满度 | Manning `Q = (1/n)·A·R^(2/3)·i^(1/2)` | 支撑"DN600 管径不足"这条结论（现仅为台账断言） | 管径、坡度、n（已有：DN600 i=0.30%） |
| 积水上涨与封控提前量 | `dV/dt = Q_in − Q_out`，`d = V/A` | **决策价值最高**：给出"再过 X 分钟达 25 cm 阈值"及建议启动封控时刻 | 片区内涝面积、当前水深、抽排能力 |
| 下穿隧道倒灌 | 堰流 `Q = C·b·h^(3/2)` / 孔口出流 `Q = C·A·√(2gh)` | 服务 §5 的 `backflow_risk`，并给出距封闭窗口剩余时间 | 洞口宽、入口挡水槛、外部水位 |
| 封控点选择 | 图论：点覆盖 / 最小割 / 绕行代价加权最短路 | 替代"逐条按阈值判封控"，给出代价最小的封控组合 | 路网连接表（已有 `road_graph.py`） |
| 不确定性传播 | 区间算术 / 固定种子 Monte Carlo | 降雨 60–100 mm 情景下的缺口区间（如 1–3 台），决策从点估计变稳健区间 | 各输入的量级范围 |

### 统计学、运筹学与可靠性（同样进内核，不靠 Agent 口算）

| 学科 | 具体机制 | 落在哪里 | 数据前提 |
| --- | --- | --- | --- |
| 统计学 · 推断 | bootstrap 置信区间、配对置换检验（P11 已在用）、分层抽样 | 评测口径、A/B 与消融对照 | 已有 |
| 统计学 · 不确定性 | 区间算术 + 固定种子 Monte Carlo → 缺口区间而非点值 | §6.2 缺口 | 已有量级范围 |
| 概率论 · 极值 | 重现期 / 超阈概率：把"80 mm/2h"翻译成"约 N 年一遇" | 跳 1 预警等级 | **需成都历史降雨序列**，缺则 `uncomputable` |
| 运筹学 · 优化 | min-cost flow / 整数规划求"最优分配"，作为 P9 规则策略的上界对照 | 跳 5 / P9 | 已有库存与跨区时延 |
| 可靠性工程 | 故障树 + 无备电启动失效概率（泵组 MTBF 或假设分布） | 跳 5 / 二次风险 | 需 MTBF；缺则只做定性 |
| 图论 | 最短路（已有 `road_graph.py`）、最小割 / 点覆盖选封控组合 | 跳 6 | 已有路网 |
| 决策论 | 多准则（安全 / 时延 / 成本）权重显式化 + 风险偏好 | 总控裁决 | 已有 |

### 只能做量级判断（必须标 `uncomputable`）

管底绝对标高、局部损失、跌水/倒虹、泵特性曲线 H-Q、Chicago 雨型、地形高程网格 —— 台账均无。
输出一律走 `uncomputable` 声明，不用推测补全。

### 明确不做

| 不做 | 原因 |
| --- | --- |
| 圣维南非恒定流、2D 浅水、1D/2D 耦合内涝推演 | 需 EPA SWMM 级模型 + 率定数据，硬做就是假精确 |
| 让 LLM 现场推导流体公式 | 违反 §4「断言权在内核」 |
| 把定量计算塞进推演主循环做迭代求解 | 时延不可控；准静态 + 缓存足够 |

### 架构落法

```
src/decision_poc/quant/
├─ hydrology.py     产流 Q=ψqF，暴雨强度分档
├─ hydraulics.py    Manning、积水速率与提前量、堰流倒灌
├─ graphopt.py      封控点选择（图论）
└─ uncertainty.py   区间算术 / 固定种子 Monte Carlo
```

```python
class QuantResult(StrictModel):
    value: float
    unit: str
    formula_id: str              # Q-RUNOFF-RATIONAL / Q-MANNING / Q-WEIR ...
    formula_source: str          # 规范条款或教科书出处，asset_id#locator 形式
    assumptions: list[str]       # 非空
    inputs: dict[str, float]
    error_bound: str             # 量级区间，或"未率定"
    computable: bool             # False 时 value 置 0 并给原因
```

以 `rule_output` 类 claim 进溯源链，`source_ref` 指公式出处 —— 物理算出来的数与台账里的数走同一套可证伪机制。

**与多智能体的关系**：定量层是工具，不是 Agent 能力。部门 Agent 决定"算什么情景"，内核决定"算出多少、依据哪条公式"。
新增 MCP 工具组 `quant`（`runoff_estimate` / `ponding_forecast` / `backflow_estimate` / `closure_optimize` /
`sensitivity_run`），Agent 只能调用，不能自己推公式。角色去重性因此可测：排水 Agent 引用产流与提升能力、
交通 Agent 引用封控提前量、燃气 Agent 引用浸泡时长，各自引用的物理量不同。

**重算合并要求**：§6.2 的 `inflow` 由本层计算，所以定量层必须与 §7 的数字重算**合并成一次**，
不得分两批漂移。

## 7 对外数字重算（严格执行 AGENTS.md 单一 owner）

本轮**接受数字漂移**，但必须一次改全、留变更记录：

| 需重算/重生成 | 位置 |
| --- | --- |
| 六跳与缺口 | `docs/urban-scenario-ground-truth.md`（升 v2） |
| 冻结期望 | `samples/eval/evidence_expectations.json`（含 `checks.*`、指纹） |
| 评测用例 | `samples/eval/*.json`、`urban_eval` 22 用例期望 |
| P9 进化 | `allocation_cases.json`（8 训练 + 4 留出）、`p9-policy-evolution-report.md` |
| 五处同步 | `README.md`、`docs/competition-gap-audit.md`、`docs/competition/preliminary-submission-draft.md`、`docs/demo-runbook.md`、`scripts/reproduce.py` 的 `expect` |
| 自检报告 | `docs/reports/evidence-check-report.md`（重生成，不手改） |

新增 `docs/reports/p12-scenario-v2-changelog.md`：逐条列 v1 → v2 的数值变化与原因（新增通道、泵站口径）。
**这张 changelog 是答辩材料**——被问"数字怎么变了"时直接给原因，而不是让它看起来像改数字过检。

## 8 评测与消融

### 8.1 新增指标

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 角色覆盖度 | 每跳至少有 1 张 `ActionCard`，或显式 `no_action + reason` | 100% |
| 联合发布命中率 | C1 实例中封路/封通道指令必须含全部 `co_sign` 部门 | 100% |
| 结论一致性 | 多 Agent 最终 `kernel_fingerprint` == 单 Agent 驱动 | 8/8 |
| 协同轨迹完整性 | 每个 Agent 至少一次调用落盘到 `trace.jsonl` | 100% |
| 端到端时延 | 多 Agent 全流程 p50/p95 | 如实报，不设美化目标 |

### 8.2 消融落点

P11 五档（Rules → DT → 单 Agent → **多 Agent** → 全量）中的第 4 档由本阶段补齐。
增益只记：协同动作条数、角色覆盖度、冲突保留数；**不记速度**（多 Agent 只会更慢）。
"多 Agent 到底贡献了什么"的答案是：**把跨部门协同从"一条综合结论"变成"6 份可分别送办的部门动作"**。

## 9 MCP 与平台装配

- 工具 12 → 13：新增 `collab_run`（组 `collaboration`），输入 `event_id + as_of`，输出 `CollabResult`。
- 现有 `--groups` 白名单机制复用，新增 `role_drain` / `role_road` / `role_traffic` / `role_gas` / `role_power` 五组，
  部门 Agent 只能看见本组工具（越权在工具层就拦住，不靠提示词自律）。
- 平台侧（路径 B）：总控 Agent 工具集改为「`collab_run` + 推理类工具」，提示词改为先 `collab_run` 再摘要。
- 真机验证从"六跳 8/8"扩展为"六跳 8/8 + `collab_run` 结论一致 8/8"。

## 10 三维与语义绑定同步

| 项 | 现状 | v2 要求 |
| --- | --- | --- |
| 实体 id 统一 | 3D 用 `TN-TIANFU`，推演用对象 id，两边未对齐 | 建立 `twin_id` 映射表，高亮按推演 id 命中 |
| 泵站状态枚举 | `fault` / `normal` | 增 `high_load` / `overload`，UE `apply_scenario` 同步 |
| 通道倒灌表现 | 只表现为"隧道积水" | 增加倒灌状态标签与封控动作高亮（`close_underpass`） |
| 六跳高亮 | 24 实体 / 6 跳 | 补 `UNDERPASS` 类与 `co_sign` 部门节点 |

## 11 子任务拆分（CodeArts 可直接执行）

### T1 场景 v2 数据与规则
- 范围：新增 `samples/assets/urban/underpasses.csv`；`drainage_facilities.csv` 增列；新增 4 条规则 JSON；
  `docs/urban-scenario-ground-truth.md` 升 v2。
- 输入：§5、§6。
- 测试：新增空台账时不崩（返回 `insufficient`）；`h_ext` 恰好等于阈值的边界；规则 `source_ref` 必须能定位原文行。
- 禁止改动：现有 32 条规则的 `rule_id` 与 `source_ref`。
- 回传：v1/v2 数值对照表 + ground truth v2 全文。

### T2 推演层扩展
- 范围：`ObjectKind.UNDERPASS`、通道规则接入六跳、§5.3 两条顺序硬约束、`ActionCard` 所需的 claim 关联。
- 测试：跳数仍为 6；倒灌命中时 `close_underpass` 出现在 `action` 跳；"先抽排"仍被拒；通道权重空白产出 C2。
- 禁止改动：六跳 stage 枚举；`DeductionStep` 构造期非空校验。
- 回传：新推演树样例 JSON + 顺序约束用例。

### T3 对外数字重算与冻结
- 范围：§7 全表，按 AGENTS.md 单一 owner 流程一次改全；新增 changelog。
- 测试：`evidence_check` 全绿（新口径）；`pytest -q` 全绿；`git status` 干净。
- 禁止改动：不为过检放宽期望值。
- 回传：新指纹 + 五处同步清单 + changelog。

### T4 多 Agent 契约与编排内核
- 范围：新增 `src/decision_poc/collab/`（`contracts.py` / `roles.py` / `blackboard.py` / `orchestrator.py` / `runtime.py`）+ 测试。
- 测试：构造期校验四条硬约束；拓扑冲突转人工；离线用确定性桩跑通全流程（不依赖真实 LLM）；
  结论一致性 8/8；单 Agent 驱动与多 Agent 驱动 `kernel_fingerprint` 相等。
- 禁止改动：内核 12 工具签名；`conflict_check` 判定逻辑。
- 回传：`CollabResult` 样例 + `trace.jsonl` 样例。

### T5 MCP 分组与平台装配
- 范围：`collab_run` 工具 + 6 个工具组白名单；`deploy/nexent/` 装配脚本与提示词更新。
- 测试：越权工具调用返回 `PERMISSION_DENIED`；`tools/list` 分组过滤生效（13 工具全量）。
- 禁止改动：MCP 信封类型。
- 回传：分组表 + 越权用例 + 真机 `collab_run` 调用记录。

### T7 物理/数学定量层
- 范围：新增 `src/decision_poc/quant/`（`hydrology.py` / `hydraulics.py` / `graphopt.py`）
  + MCP 工具组 `quant` 的 4 个工具（`runoff_estimate` / `ponding_forecast` / `backflow_estimate` /
  `closure_optimize`；`sensitivity_run` 由 T8 交付）+ 测试。
- 输入：§6.4。
- 测试：每个公式至少一个手算对拍用例（如 DN600 i=0.30% 满流能力 ≈ 1211 m³/h，须与 `hydraulics-report.md` 一致）；
  `assumptions` 为空即构造失败；`computable=False` 时不得产出数字；固定种子 Monte Carlo 两次结果逐位一致。
- 禁止改动：`deduction.py` 六跳结构与规则判定；除标准库外不新增依赖（图论与矩阵运算手写）。
- 回传：公式-出处对照表 + 手算对拍表 + 对 §6.2 缺口的重算差异。

### T8 统计、优化与可靠性落点
- 范围：新增 `quant/uncertainty.py`（bootstrap / 配对置换 / 区间 / 固定种子 Monte Carlo）、
  `quant/optimize.py`（min-cost flow 最优分配上界）、`quant/reliability.py`（故障树与无备电失效概率，缺 MTBF 时只输出定性）；
  `quant/sensitivity_run` 工具。
- 输入：§6.4 统计学/运筹学表。
- 测试：固定种子两次结果逐位一致；最优分配上界必须 ≥ P9 规则解（合理性断言，不满足即设计错误）；
  缺 MTBF 时 `computable=False` 且不产出概率数字；bootstrap 区间不得窄于 0。
- 禁止改动：P9 规则策略本身（本任务只加对照，不替换策略）；不新增第三方依赖。
- 回传：规则解 vs 最优解差距表 + 缺口区间表 + 可靠性是否可算的结论。

### T6 三维绑定与答辩口径
- 范围：`semantic_entities.json` 与 UE 状态枚举同步；`preliminary-submission-draft.md` 数字节；
  `demo-runbook.md` 逐字期望；`innovation-and-evidence.md` 更新"多智能体暂不做"条目。
- 测试：高亮 id 与推演 id 对齐断言；文档内不得残留旧数字（含深目录）。
- 禁止改动：3D 场景几何与已验收出图。
- 回传：对齐表 + 文档 diff 摘要。

## 12 验收清单

- [ ] 6 个 Agent 真跑，`trace.jsonl` 有每角色调用。
- [ ] `ActionCard` 四条构造期校验全部生效（各有一个失败用例）。
- [ ] 多 Agent 结论与单内核结论逐位一致 8/8。
- [ ] C1/C2/C3 全部保留，未被"综合判断"抹平。
- [ ] 倒灌进入六跳；跳数仍为 6；"封闭通道不晚于封路"成立。
- [ ] 泵站负荷用 §6.2 公式计算，`load_state` 标签可复算。
- [ ] `evidence_check` 全绿、`pytest -q` 全绿、`git status` 干净。
- [ ] 五处对外数字同步完成，changelog 逐条可核。
- [ ] 3D 高亮 id 与推演 id 对齐。
- [ ] 平台真机 `collab_run` 结论与离线一致。

## 13 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 多 Agent 放大不可复现性 | 现场第二次跑对不上 | 断言权在內核 + 结论一致性门禁 + 轨迹整轨留痕；对外只说"结论一致"，不说"轨迹一致" |
| Nexent 不支持 Agent 互调 | 路径 A 无法交付 | 先做路径 B（总控 + `collab_run`），A 作为探索，不写进验收线 |
| 重算后数字全漂移被质疑"改数字" | 可信度 | changelog 写清原因（新增通道、替换 0.8 口径）；v1 报告归档不删 |
| 多 Agent 时延上升 | 演示节奏 | 只报协同覆盖度收益，不承诺更快；渲染仍异步、不进主循环 |
| 通道参数（挡水槛、封控阈值）无原始依据 | 被追问数据来源 | 全部标"假设/待确认"，并在 `uncomputable` 声明；不冒充规范值 |
| 6 个 Agent 提示词漂移 | 出卡不稳定 | 角色提示词模板化 + 输出 schema 强校验 + 失败即失败，不降标准 |

## 14 待用户确认

1. **泵站口径**：§6.2 用"来水量 vs 可用能力"算缺口（DF-PS-01 可用能力 800 而非 960），是否接受？
2. **通道封控阈值**：§5.1 的 `close_threshold=0.25 m`、`entry_sill_height=0.15 m` 是假设值，是否沿用道路 25cm 阈值？
3. **Nexent 多 Agent**：是否要我先花一轮验证平台是否支持 Agent-as-Tool（决定走路径 A 还是只做 B）？
