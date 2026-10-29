# P4 图谱与可信溯源（回传）

> 对应验收点：「每个结论能否追溯到来源和版本」「冲突、过期、证据不足是否安全处理」。
> 代码：`src/decision_poc/semantic.py`、`src/decision_poc/provenance.py`，`knowledge_evolution.py` 追加 `get_knowledge_diff`。
> 测试：`tests/test_semantic.py`（26）、`tests/test_provenance.py`（24）、`tests/test_p4_chain.py`（6）。全量 **223 passed**。

## 1. 结论先写

| 项 | 结果 |
| --- | --- |
| 语义层规模 | 62 孪生对象 / 100 三元组，15 类实体、8 类谓词全部落地 |
| 时态 | 行级有效期优先，气象预警 08:00–14:00 内可查、之后 `OUT_OF_WINDOW` |
| 权限 | 绑定证据资产的 `visibility`，且在置信度与排序**之前**过滤 |
| 引用闸门 | 无证据 / 未知资产 / 引用格式错 / 推理引用不足 / 缺前驱引用 / 规则不可定位 —— 6 类全部拒收，`append` 抛错不落账 |
| 哈希链 | `verify()` 能定位被篡改记录与断链位置，不抛异常 |
| 追溯 | claim → 资产 → 行号 → 指纹 → 规则来源 → 记录 → 决策，全部一次取回 |
| 影响分析 | 改 1 个单元格，只点名依赖该行的结论，不牵连无关结论 |

## 2. 这一轮改掉的四个真问题

### 2.1 占位实现里的权限是自证

原 `semantic.py` 在 `SemanticTriple` 上放 `visibility_labels` 字段，但**没有任何构建器填它**，于是权限测试里手写一个 label 就通过，真实数据里永远是空 → 永不生效。

改成权限绑定到**证据资产的治理字段**（`AssetGovernance.visibility`，P0 已冻结）。理由：可见性是资产的属性，同一个 label 在资产和三元组各存一份，迟早不一致；而溯源、权限审计都从资产出发。

同时修掉占位实现的另两处：邻接只走 subject→object 单向（反向查不到）、`under_construction`/`precedes` 两个谓词不在枚举里被静默丢弃——后者恰好是推演最需要的两条边。

### 2.2 区域节点当跳转中介，影响范围一步退化

从工单出发 2 跳：**41 条**三元组，其中绝大多数是 `located_in`——因为 工单 →(located_in)→ A-03 →(located_in)→ 全片区设施。

区域是归属标签，不是传导介质。新增 `block_expansion_through`，`SemanticQuery.impact()` 预设屏蔽 `Region` 作为**中转**（仍可作为结果出现、仍可作为种子）。同一查询：**41 → 13**。`DF-PS-01` 从 33 → 4。

答辩时这句话是可量化的：「不屏蔽区域穿越，影响范围在一跳内就等于全片区清单」。

### 2.3 资产级有效期 ≠ 行级有效期

`weather_alerts.csv` 的资产级 `valid_to` 是 `None`，每行的有效期在 `valid_from`/`valid_to` 列里。按资产算，**所有气象预警永久有效**——这恰好是时态层要防的"过期事实当现行事实"。

改为行级优先：实体行带 `valid_from`/`valid_to` 就用行级窗口，否则退回资产治理窗口。关系窗口跟随主语行（`affected_by` 反转后主语变了，所以用原始 subject 取窗口）。

实测：`WA-20261029-01` 窗口 08:00–14:00；`as_of=12:00` 返回 1 条，`as_of=15:00` 返回 `partial + OUT_OF_WINDOW`。

### 2.4 同一个 locator 写两遍能骗过"至少 2 条引用"

`inference` 要求 ≥2 条引用。原实现数 `len(evidence_refs)`，把同一 `asset#locator` 写两遍就通过了——闸门形同虚设。

改为数**不同出处**（`len(set(...)) < 2`）。这条在真实数据里立刻暴露：`DF-PS-02` 的 `maintained_by` 和 `located_in` 来自同一份台账同一行，天然只算 1 条证据。所以集成用例改成跨两个文件（工单 + 设施台账）取证。

## 3. 跨版本血缘：为什么用 locator 而不是 asset_id

P3 已经踩过一次：`asset_id` 是内容寻址的，改一个单元格整份文件换 id。

这条在血缘上后果更严重：结论记录时引用的是**旧** asset_id，数据改动后 diff 给出的是**新** asset_id，两边永远匹配不上，`affected_decisions` 会安静地返回空——看起来"没有结论受影响"，实际上全受影响。这是最坏的失败模式：错得不明显。

解法：`KnowledgeDiff.affected_locators` 输出 locator（`A-03/drainage_facility/v1#row:2` 这种稳定逻辑身份），`affected_decisions` 接受 asset_id 或 locator。实测：给 `DF-PS-01` 补 `owner_org` 后，v1.0.0 → v1.0.1 的 diff 点名 `DEC-20261029-01`（依赖设施台账），道路结论 `DEC-20261029-02` 不受牵连。

## 4. 引用闸门（写进测试，不靠人盯）

| claim 类型 | 拒收原因码 | 触发条件 |
| --- | --- | --- |
| `fact` | `NO_EVIDENCE` | 无引用 |
| 任意 | `MALFORMED_REF` | 缺 `#locator` 或某段为空 |
| 任意 | `UNKNOWN_ASSET` | 资产不在账本指纹表（= 溯源不到来源） |
| `inference` | `INFERENCE_TOO_FEW_REFS` | 不同出处 < 2 |
| `inference` | `INFERENCE_MISSING_PREMISE_REFS` | 未包含前驱 claim 的全部引用 |
| `inference` | `UNKNOWN_PREMISE` | 前驱 claim 未注册 |
| `rule_output` | `UNKNOWN_RULE` | 规则 id 未注册 |
| `rule_output` | `RULE_SOURCE_NOT_LOCATABLE` | 规则 `source_ref` 指向的资产不可查 |

`ProvenanceLedger.append()` 遇到任一 unsupported 直接抛 `EVIDENCE_INSUFFICIENT` 并列出 claim id，**不落账**。这样"引用完整率 100%"是可断言的事实，而不是跑完再看统计。

## 5. 哈希链与篡改定位

`verify()` 做两件事并各自出结论：

1. 重算每条记录的 `record_hash`，不一致 → `HASH_MISMATCH:{record_id}`；
2. 检查 `upstream_record_hash` 是否等于前一条的 `record_hash`，不等 → `CHAIN_BROKEN`。

返回 `VerifyReport{ok, checked, first_broken_record_id, broken_record_ids, reasons}`，**不抛异常**。审计工具如果遇到第一个问题就中断，等于把后面的问题一起藏起来。

两种篡改都有用例：
- 改字段（`decision_id`）→ `HASH_MISMATCH`，定位到正确 record_id；
- 重封自身哈希、只改 upstream → 自身校验通过，`CHAIN_BROKEN` 定位到该条。

`adopt()` 用来原样载入既有记录（不重算封印），因为记录与审计发生在不同进程——P5 的 MCP 一次请求一个进程，没有这个能力就无法审计历史。

## 6. 一个刻意保留的诚实边界

两个受限规程文件（`procedure_waterlogging.md`、`procedure_gas_protection.md`）标了 `visibility=ORG-DRAIN/ORG-EMERG` 与 `ORG-GAS/ORG-EMERG`，但**它们今天没有产出任何关系**——P2 的抽取只覆盖表格行和预案步骤，不覆盖规程条款正文。

所以权限过滤在真实数据上目前"无从泄漏"。我没有把测试写成躲开这一点，而是写成 `test_restricted_assets_never_leak`：遍历所有引用受限资产的关系，逐个用越权身份查询并断言查不到。今天它空转通过；**P6 一旦把规程条款抽成 `DeductionRule`（`source_ref` 指向规程），这条测试立刻变成实打实的守门**。

这是 P6 的输入约束：规程规则必须带 `source_ref`，否则推演结论无法溯源，也过不了这道测试。

## 7. 关键接口

```python
store = build_urban_semantic_store()                       # 62 对象 / 100 三元组
store.query(SemanticQuery.impact(query_id, as_of, seed_ids, caller_labels=[...]))
#   -> ToolResult{data: triples / objects / snapshot_hash / reason}

ledger = build_ledger(rules={...})                          # 资产指纹取自 P1 清单
ledger.register([Claim(...)])                               # -> [ClaimCheck]
ledger.append(decision_id, claim_ids, semantic_snapshot_hash, processors=[...])
ledger.trace_claim(claim_id)                                # -> ClaimTrace（资产/行号/指纹/规则/记录）
ledger.verify()                                             # -> VerifyReport
ledger.replay(decision_id)                                  # -> ReplayBundle（最小证据集）
ledger.affected_decisions(diff.affected_locators)           # 数据变更 -> 需重算的结论

get_knowledge_diff(registry, "kb-v1.0.0", "kb-v1.0.1")      # -> KnowledgeDiff
```

## 8. 已知边界（P5/P6 前必须处理）

1. **语义层不产生新关系**：规程条款、预案阈值规则目前只是文档，P6 必须抽成 `DeductionRule` 并带 `source_ref`。
2. **`AFFECTS` 方向已归一**（`X affected_by Y` → `Y affects X`），P6 写规则时按"源头→受影响"理解，别按原始 CSV 方向。
3. **`deprecated` 三元组永不参与**，即使 `include_candidates=True`；P6 若要展示"废止预案怎么说"，需要单独接口，不能靠放宽过滤。
4. **`neighbors` 的 `kind_filter` 在遍历之后过滤**，因此它只裁结果、不裁路径。要做"只沿设施类节点扩散"需要第二个控制项（`allow_expansion_through`），等 P6 有明确需求再加。
5. **账本未做磁盘加载的完整往返测试**（`save` 有、`adopt` 有，但"保存→新进程加载→verify"链条在 P5 接 MCP 时补）。
