# 城市基础设施数字孪生语义层 / 跨部门事件推演 / AI 数据可信溯源 设计规格

- 日期：2026-10-29
- 角色：Codex（方案规划与评审）
- 下游：CodeArts（按任务单实现）
- 上游依据：`docs/ICT-创新赛-赛题3-方案与实施计划.md`、`docs/data-asset-spec.md`、`docs/ontology-bootstrap.md`、`docs/superpowers/plans/2026-10-26-knowledge-governance-rag.md`

## 1. 目标与边界

### 1.1 三个能力的一句话定义

| 能力 | 定义 | 失败形态（必须可测） |
| --- | --- | --- |
| 数字孪生语义层 | 把设施对象的几何/状态事实，与"谁负责、受谁影响、依据哪条规则"的语义关系统一到一个带时态和版本的查询面上 | 语义层与孪生状态不同步 → 结论引用已失效事实 |
| 跨部门事件推演 | 从单个事件现象出发，沿语义层多跳推出受影响对象、责任部门、职责冲突、资源缺口和二次风险，输出可复核的推演树 | 模型自由发挥编造部门/路径 → 推演不可复核 |
| AI 数据可信溯源 | 结论中的每一条断言都绑定可定位、可校验、可回放的证据链，链条被篡改或过期即拦截 | 引用只到"某个文档"级别 → 无法证伪 |

### 1.2 硬边界（本阶段不做）

- 不引入 Neo4j / Nebula 等真实图数据库；语义层用内存实现，仅以 `GraphBackend` Protocol 留出替换点。
- 不接大模型生成推演结论。推演只由确定性规则 + 图遍历产生，LLM 仅可用于自然语言润色且不参与断言生成。
- 不做真实 GIS 渲染；几何以 `geometry_ref` 字符串指向外部要素 ID。
- 不改动现有 `DecisionRequest` / `DecisionOutput` 字段语义，新能力以新增模块 + 可选注入方式挂到 `graph.py`。
- 不实现真实身份认证；权限标签沿用知识治理计划里的 `visibility_labels` 模拟口径。

## 2. 总体架构与数据流

```
异构资产 ──data_asset.py──> DataAsset(asset_id + sha256 + locator)
                                    │
                        ontology.py / ontology_bootstrap.py
                                    │ 候选三元组（已有人工确认流程）
                                    ▼
        ┌──────────── semantic.py ────────────┐
        │  TwinSemanticStore                  │
        │  TwinObject + SemanticTriple        │
        │  as_of 时态快照 / 版本 / 邻接索引    │
        └───────────────┬─────────────────────┘
                        │ 语义查询（邻接扩展、职责、规则）
                        ▼
        ┌──────────── deduction.py ───────────┐
        │  DeductionRule(声明式) + 多跳遍历    │
        │  → DeductionTrace（推演树）          │
        │  → DepartmentConflict / ResourceGap │
        └───────────────┬─────────────────────┘
                        │ 每个节点都必须带 evidence_refs
                        ▼
        ┌─────────── provenance.py ───────────┐
        │  Claim → ProvenanceRecord           │
        │  append-only 哈希链 + verify/replay │
        │  引用完整性 / 过期 / 权限校验        │
        └───────────────┬─────────────────────┘
                        ▼
        ToolResult 信封 → MCP Tool → Nexent Skill 编排 → 现有审批门禁与评测
```

关键约束：**下游只能消费上游已落库的对象**。推演节点不得引用语义层查不到的实体；溯源记录不得引用资产清单里不存在的 `asset_id`。这条约束用"构造即校验"实现，而不是事后巡检。

## 3. 语义层设计（`src/decision_poc/semantic.py`）

### 3.1 双模型分离

数字孪生的两个面必须分开建模，否则时态和版本会互相污染：

- **TwinObject（孪生实体面）**：客观存在、有几何、有生命周期状态、会变化。
- **SemanticTriple（语义关系面）**：描述职责、影响、隶属、依赖、依据；带版本和置信度，可被人工修订。

两者通过 `twin_id` 对齐；孪生对象再通过 `asset_ids` 对齐资产清单。三方对齐关系是溯源能落地的前提。

### 3.2 数据模型

```python
class ObjectKind(StrEnum):
    ROAD = "road"; DRAINAGE = "drainage"; POWER = "power"
    GAS = "gas"; TELECOM = "telecom"; PUMP_STATION = "pump_station"
    REGION = "region"; WORK_SITE = "work_site"

class LifecycleState(StrEnum):
    PLANNED = "planned"; IN_SERVICE = "in_service"
    UNDER_MAINTENANCE = "under_maintenance"; DECOMMISSIONED = "decommissioned"

class TwinObject(StrictModel):
    twin_id: str                      # TW-<kind>-<slug>
    kind: ObjectKind
    name: str
    region_id: str | None
    geometry_ref: str | None          # 外部要素 ID，不做几何计算
    lifecycle_state: LifecycleState
    state_as_of: datetime             # 状态观测时间（带时区）
    asset_ids: list[str]              # 指向 DataAsset
    attributes: dict[str, str]        # 埋深/管径/等级等标量事实

class Predicate(StrEnum):
    SERVES = "serves"; DEPENDS_ON = "depends_on"; LOCATED_IN = "located_in"
    MAINTAINED_BY = "maintained_by"; AFFECTS = "affects"
    REGULATED_BY = "regulated_by"; CONFLICTS_WITH = "conflicts_with"

class SemanticTriple(StrictModel):
    triple_id: str
    subject_id: str                   # twin_id 或 org_id
    predicate: Predicate
    object_id: str
    confidence: float                 # 0~1，来自本体抽取分档
    valid_from: datetime
    valid_until: datetime | None      # 半开区间 [from, until)
    knowledge_version: str            # 与知识库版本对齐
    evidence_refs: list[str]          # asset_id#locator 形式，至少 1 条
    review_status: Literal["candidate", "confirmed", "deprecated"]

class SemanticQuery(StrictModel):
    query_id: str
    as_of: datetime                   # 时态查询基准
    seed_ids: list[str] = []
    kind_filter: list[ObjectKind] = []
    predicates: list[Predicate] = []
    max_hops: int = 2
    min_confidence: float = 0.6
    caller_labels: list[str] = []     # 权限过滤，先过滤再排序
```

### 3.3 `TwinSemanticStore` 接口

```python
class TwinSemanticStore:
    def __init__(self, objects, triples, *, snapshot_version: str) -> None: ...
    def snapshot_hash(self) -> str                      # 对象+三元组规范化 JSON 的 sha256
    def get_object(self, twin_id: str) -> TwinObject    # 不存在抛 KeyError
    def neighbors(self, seed_ids, *, query: SemanticQuery) -> list[SemanticTriple]
    def query(self, query: SemanticQuery) -> ToolResult  # data 内含 triples/objects/snapshot_hash
```

规则：

1. 时态判定 `valid_from <= as_of < valid_until`；`valid_until is None` 视为长期有效。
2. 只返回 `review_status == "confirmed"` 的三元组；`candidate` 仅在被显式要求时返回并打标，不参与推演。
3. 权限过滤在 `min_confidence` 和排序**之前**执行，防止越权项因排名靠前而泄漏。
4. 每次查询结果必须回传 `snapshot_hash`，供溯源记录锚定"当时看到的是哪一版语义世界"。
5. 空结果返回 `status="partial"` + 结构化原因，不返回伪造邻居。

### 3.4 与现有模块的衔接

- `ontology_bootstrap.py` 产出的 `ontology_candidates.json` 经人工 `confirmed` 后直接构造 `SemanticTriple`，不新增第二套抽取链路。
- `data_asset.py` 的 `asset_id` + `locator` 即 `evidence_refs` 的取值规范，不新增定位语法。

## 4. 跨部门事件推演设计（`src/decision_poc/deduction.py`）

### 4.1 推演链固定为六跳

现象 → 受影响孪生对象 → 责任部门 → 职责冲突 → 资源需求 → 处置动作 → 二次风险

六跳写死在 `DEDUCTION_STAGES` 常量里，每跳有独立的状态与失败原因。跳数固定是为了让"路径正确率"这个指标可计算——自由跳数无法定义 ground truth。

### 4.2 规则是数据，不是代码

```python
class DeductionRule(StrictModel):
    rule_id: str
    stage: Literal["affected", "owner", "conflict", "resource", "action", "residual_risk"]
    when: dict[str, list[str]]        # 例 {"predicate": ["located_in"], "kind": ["drainage"]}
    then: dict[str, str]              # 例 {"owner_org": "ORG-DRAIN-01"}
    priority: int                     # 冲突时取小者
    confidence: float
    source_ref: str                   # 应急预案/职责文件的 asset_id#locator
```

- 规则命中只做字典匹配 + 图邻接，不调模型；同一 `stage` 多条规则命中时按 `priority` 取首条，其余记入 `shadowed_rule_ids` 备查。
- 规则缺失导致某跳为空 → 该节点状态 `insufficient`，推演树继续但结论降级，并在最终输出中列为缺口，而不是静默补全。

### 4.3 推演产物

```python
class DeductionStep(StrictModel):
    step_id: str
    stage: str
    conclusion: dict[str, str]
    rule_id: str | None               # None 表示由语义查询直接得出的事实
    premise_step_ids: list[str]
    evidence_refs: list[str]          # 非空校验在构造时执行
    confidence: float
    status: Literal["supported", "insufficient", "conflict"]

class DeductionResult(StrictModel):
    event_id: str
    root: EventContext
    steps: list[DeductionStep]
    conflicts: list[DepartmentConflict]   # 同一对象多部门职责重叠/空白、资源争用
    resource_gaps: list[ResourceGap]      # 需求量 vs 可用量
    residual_risks: list[str]
    uncovered_stages: list[str]
    snapshot_hash: str
```

### 4.4 职责冲突的三种判定

1. **重叠**：同一 `twin_id` 存在 2 条以上 `MAINTAINED_BY` 且部门不同 → 需明确主责。
2. **空白**：受影响对象无任何 `MAINTAINED_BY` → 推演直接标记 `conflict`，不给处置动作。
3. **资源争用**：两个部门的处置动作请求同一资源且超出库存 → 记 `ResourceGap`，量化缺口数值。

三类冲突都必须进入最终输出，不允许被"综合判断"抹平——这是跨部门场景的演示价值所在。

## 5. 可信溯源设计（`src/decision_poc/provenance.py`）

### 5.1 断言优先，引用随后

```python
class ClaimKind(StrEnum):
    FACT = "fact"; INFERENCE = "inference"; RULE_OUTPUT = "rule_output"

class Claim(StrictModel):
    claim_id: str
    text: str
    kind: ClaimKind
    evidence_refs: list[str] = []     # 允许为空，但为空即 unsupported
    produced_by: str                  # module:rule_id / model_id
```

`ProvenanceRecord` 只接受**已通过完整性校验**的 claim。校验规则：

| claim 类型 | 最低引用要求 |
| --- | --- |
| fact | ≥1 条 `asset_id#locator`，且该 asset 的 `checksum_sha256` 在清单中可查 |
| rule_output | ≥1 条 `rule_id`，且该规则的 `source_ref` 可定位 |
| inference | ≥2 条，且必须包含其所有前驱 claim 的引用 |

不满足 → `status="unsupported"`，该 claim 不进入对外答案，并计入"引用完整率"分母。

### 5.2 哈希链与回放

```python
class ProvenanceRecord(StrictModel):
    record_id: str
    decision_id: str
    claim_ids: list[str]
    asset_fingerprints: dict[str, str]    # asset_id -> sha256
    knowledge_version: str
    semantic_snapshot_hash: str
    upstream_record_hash: str | None      # 前一条记录哈希，形成链
    record_hash: str                      # 自身规范化 JSON 的 sha256
```

- `ProvenanceLedger.append(record) -> ProvenanceRecord`：自动填 `upstream_record_hash` 与 `record_hash`。
- `ledger.verify() -> VerifyReport`：从头重算哈希，返回首个断链位置与受影响 `record_id` 列表（不抛异常，异常会掩盖审计信息）。
- `ledger.replay(decision_id) -> ReplayBundle`：输出重建该结论所需的最小证据集（资产指纹 + 知识版本 + 快照哈希 + 规则版本），用于"数据变了结论是否还成立"的答辩追问。

### 5.3 三重拦截

推演结果对外输出前必须过三道闸，任一不过即降级为"仅展示证据，不给建议"：

1. **引用完整**：所有 claim 可定位；
2. **版本一致**：`semantic_snapshot_hash` 与记录时一致，且 `knowledge_version` 未过期；
3. **权限合规**：所有引用的资产/三元组对调用方可见。

## 6. MCP 工具与 Skill 映射

| MCP Tool | 输入 | 输出（`ToolResult.data`） | 对应 Skill |
| --- | --- | --- | --- |
| `semantic_query` | `SemanticQuery` | triples / objects / snapshot_hash | 资产检索 |
| `twin_neighbors` | twin_id + max_hops | 邻接三元组 + 对象摘要 | 事件影响分析 |
| `event_deduce` | `EventContext` | `DeductionResult` | 跨部门推演 |
| `conflict_check` | `DeductionResult` | conflicts / gaps / 建议主责 | 资源冲突检查 |
| `provenance_verify` | decision_id | VerifyReport | 证据审计 |
| `evidence_locate` | evidence_ref | asset 摘要 + locator 片段 | 证据详情 |

六个工具全部返回既有 `ToolResult` 信封（`status` / `data` / `error` / `stats` / `context`），不新增信封类型——这是 Skill 可复用、可替换的前提。

## 7. 目录结构与模块归属

```
src/decision_poc/
├─ semantic.py         新增：TwinObject / SemanticTriple / TwinSemanticStore
├─ deduction.py        新增：DeductionRule / DeductionStep / 六跳引擎
├─ provenance.py       新增：Claim / ProvenanceRecord / ProvenanceLedger
├─ urban_demo.py       新增：三个模块装配 + 一条演示主线
├─ contracts.py        仅追加 Claim 相关可选字段，不改现有模型语义
├─ graph.py            可选注入推演与溯源，缺省行为不变
tests/
├─ test_semantic.py / test_deduction.py / test_provenance.py / test_urban_demo.py
samples/assets/
└─ urban/              新增演示数据（道路/排水/供电/燃气/通信 + 职责与预案）
```

## 8. 子任务拆分（CodeArts 可直接执行）

### Task 1：语义层内核
- 目标：`semantic.py` 提供带时态、权限、版本的孪生实体与三元组查询。
- 范围：仅新增 `semantic.py`、`tests/test_semantic.py`。
- 输入：本规格第 3 节；`samples/assets/urban/` 样例数据。
- 输出：`TwinObject` / `SemanticTriple` / `SemanticQuery` / `TwinSemanticStore`。
- 测试标准：时态半开区间边界、未确认三元组不参与、权限先于排序过滤、空结果 `partial`、`snapshot_hash` 稳定性（同数据两次查询哈希一致）。
- 禁止改动：`contracts.py` 现有模型、`data_asset.py` 字段。
- 回传：变更说明 + 测试输出 + 覆盖率。

### Task 2：事件推演引擎
- 目标：六跳推演可跑通并输出推演树、冲突、资源缺口。
- 范围：`deduction.py`、`tests/test_deduction.py`。
- 测试标准：六跳顺序固定；规则缺失产出 `insufficient` 而非静默补全；三类职责冲突均可复现；`evidence_refs` 为空即构造失败；跳数上限生效。
- 禁止改动：语义层公开接口；不得在推演中调用任何模型。
- 回传：推演树样例 JSON + 冲突用例。

### Task 3：溯源账本
- 目标：claim 校验、哈希链、verify、replay 全通。
- 范围：`provenance.py`、`tests/test_provenance.py`。
- 测试标准：篡改任意字段 `verify()` 定位到正确断点；`inference` 引用不足 2 条被拒；`replay` 输出的证据集足以重建结论；账本 append-only（不可变对象）。
- 禁止改动：`DataAsset` 指纹算法。
- 回传：篡改检测用例 + verify 报告样例。

### Task 4：演示主线装配
- 目标：`urban_demo.py` 用一条暴雨积水事件跑通 语义查询→推演→溯源 全链路，输出可打印的结构化报告。
- 范围：`urban_demo.py`、`samples/assets/urban/`、`tests/test_urban_demo.py`。
- 测试标准：端到端确定性（同输入两次运行输出字节一致）；报告含冲突清单与证据卡片；任一闸门不过时降级路径可测。
- 回传：运行日志 + 报告样例。

### Task 5：MCP 工具封装
- 目标：六个工具按第 6 节契约暴露，返回 `ToolResult`。
- 测试标准：错误路径返回 `status="error"` 且 `error.retryable` 判定正确；参数校验失败不产生副作用。
- 回传：工具签名表 + 调用样例。

### Task 6：评测与文档
- 目标：把"检索命中、路径正确率、引用完整率、冲突拦截率、响应时间"接入 `evaluation.py`，更新 README 与文档导航。
- 测试标准：指标基于显式 fixture ground truth，不由检索分数反推正确性。
- 回传：指标基线表 + 失败案例集。

## 9. 验收清单

- [ ] 语义层查询结果带 `snapshot_hash`，同数据可复现。
- [ ] 推演树每个节点 `evidence_refs` 非空，且引用可在资产清单中解析。
- [ ] 三类职责冲突（重叠/空白/争用）各有可复现用例。
- [ ] 篡改任一条溯源记录，`verify()` 能定位断点。
- [ ] 引用不足、版本过期、越权引用三种情况均触发降级而非放行。
- [ ] 端到端两次运行输出一致（确定性）。
- [ ] 六个 MCP 工具契约与第 6 节一致，错误路径有结构化返回。
- [ ] 现有 96 项测试全绿，`graph.py` 缺省行为未变。

## 10. 指标映射（对齐赛题建议指标）

| 赛题指标 | 本设计落点 |
| --- | --- |
| 检索 Top-k 证据命中率 ≥85% | `semantic_query` + 关键词/邻接检索评测集 |
| 多跳路径正确率 ≥80% | 六跳固定 stage 的 ground truth 比对 |
| 结论引用完整率 100% | claim 引用校验闸门（硬门禁，非统计） |
| 冲突/过期证据拦截率 100% | `verify()` + 三重拦截 |
| 端到端响应 ≤30s | `urban_demo` 计时断言 |

## 11. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 六跳规则写死后场景无法扩展 | 演示泛化性质疑 | 规则全部数据化（`DeductionRule`），新增场景只加 JSON 不改代码 |
| 语义层与孪生实时状态漂移 | 结论引用过期事实 | 查询强制 `as_of` + `snapshot_hash` 锚定 |
| 演示数据自造，评委质疑真实性 | 可信度 | 资产全部经 `data_asset.py` 登记，保留 checksum 与 locator，标注来源与合成说明 |
| 哈希链被当成区块链宣传 | 技术表述失真 | 文档统一表述为"append-only 哈希链审计日志"，不宣称不可篡改存储 |
| 任务 1~3 并行导致接口漂移 | 返工 | 接口以本规格为准，Task 4 之前禁止改签名 |

## 12. 待用户确认

1. 演示数据是否使用合成数据（建议：是，标注来源），还是必须接入真实市政公开数据？
2. 是否需要在初赛阶段就交付可视化 Demo，还是先交 CLI 结构化报告 + 证据卡片 JSON？
3. 语义层是否允许使用 `networkx`（现无依赖），或坚持零新依赖用自建邻接索引？
