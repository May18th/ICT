# P3 本体演化闭环：候选 → 确认 → 发布 → 回滚（回传）

> 对应验收点：「本体是否支持人工确认、发布和回滚」。
> 代码：`src/decision_poc/knowledge_evolution.py`；测试：`tests/test_knowledge_evolution.py`（16 项）。

## 1. 结论先写

| 项 | 结果 |
| --- | --- |
| 基准版本 | `kb-v1.0.0`，62 实体 / 100 关系，`snapshot_hash=01f4f37f51d3` |
| 数据改动 → 候选生成 | 改 1 个单元格产出 2 条候选（修复前是 18 条，见 §3） |
| 影响评估 | 审批前给出受影响实体、邻接实体数、风险标记、待重算 claim |
| 逐条确认 | 支持 KEEP / DROP 混合决策；未表态的 `review` 级变更默认**暂缓**而非采纳 |
| 版本发布 | `kb-v1.0.1`，`parent_version` 指向 v1.0.0，`snapshot_hash=420ee10b1a14` |
| 回滚 | 回到 v1.0.0 后哈希与原始基准**逐位一致**，历史不删 |
| 跨进程 | 未决提案可从磁盘重载，人工确认不必与提案同进程 |
| 全量测试 | 170 passed |

## 2. 数据结构（冻结）

```
EvolutionProposal
├── proposal_id        确定性 id（base_version + 触发资产），无随机数
├── trigger_asset_ids  哪几份数据触发了它
├── base_version       基于哪个版本算的 diff
├── changes[]          KnowledgeChange{kind: add|remove|modify, candidate_id, label, evidence_asset_ids}
├── impact             ImpactAssessment{affected_names, affected_types, neighbor_count,
│                                        stale_claim_ids, risk_flags, reason}
└── status             pending | published | rejected
```

三点取舍：

1. **提案是 diff，不是抽取结果快照**。审批人只看变了什么，不看全量候选。
2. **影响评估在审批前算完**。`neighbor_count` 走图 1 跳扩散，回答"改这条会动到什么"。
3. **`stale_claim_ids` 现在就是钩子**。P3 没有 claim，字段由调用方注入的 `claim_index` 填充；P4 把血缘索引接上后，同一个字段直接变成"哪些历史结论失效"的答案，契约不用改。

## 3. 一个必须修的 bug：内容寻址把 1 条变更放大成 18 条

**现象**：只给 `DF-PS-01` 补一个 `owner_org`，提案报 17 条 modify + 1 条 add。

**根因**：`asset_id` 是内容寻址的（`AS-A-03-drainage_facility-cd569425`，尾 8 位是内容哈希）。改一个单元格 → 整份文件换 id → 该文件产出的**每个**候选的 `evidence.asset_id` 都变 → 全部判为"属性变化"。

**修法**：比对指纹改用 `locator + quote`，`asset_id` 只作溯源信息保留。

- `locator` 本身已编码逻辑身份（`A-03/drainage_facility/v1#row:3`），文件真换版（`v1→v2`）仍会触发变更；
- 内容编辑不换 `locator`，只有真实的属性/引文差异才浮出来。

**效果**：18 → 2。`tests/test_knowledge_evolution.py::test_content_hash_churn_does_not_inflate_proposal` 锁死这条，且先断言"asset_id 确实随内容变了"，防止样本失效后测试变成空转。

## 4. 三个场景实测

| 场景 | 数据改动 | 候选 | 影响评估 |
| --- | --- | --- | --- |
| A 职责补全 | `DF-PS-01` 的 `owner_org` 由空填为 `ORG-DRAIN` | modify `DrainageFacility:DF-PS-01` + add `ORG-DRAIN -responsible_for-> DF-PS-01` | 影响 2 实体 / 12 邻接，`risk_flags=['responsibility_change']` |
| B 新项目进场 | `construction_plans.csv` 追加 `CP-2026-051` | add 项目 + `located_in B-01` + `R-201 under_construction` + `PL-WTR-202 under_construction`（4 条） | 影响 4 实体 / 10 邻接 |
| C 设备修复 | `DF-PS-01` 状态 `fault_pending_repair → in_service` | modify 1 条 | 影响 1 实体 / 2 邻接 |

场景 A 是最有价值的一条：它证明**职责空白被新数据自然闭合**，而不是靠抽取器猜。同时 `responsibility_change` 标记告诉审批人"这条改动会动到责任链"，这正是要在答辩里讲的"提案自带影响评估"。

## 5. 逐条确认的真实用法

场景 A 的 2 条候选可以分别处置。实测：KEEP 关系、DROP 属性变更后发布，结果是

- 新增 `responsible_for` 关系 ✅
- `DF-PS-01.attributes.owner_org` 仍为 `None` ✅（被 DROP 的变更确实没写进去）

这对应真实流程：工单说"运维主体是排水公司"，但**运维交接单还没归档**，于是只采纳关系、暂不采纳属性。

## 6. 回滚语义

回滚的是**指针**，不是删历史：

```
versions = ['kb-v1.0.0', 'kb-v1.0.1']
history  = [publish kb-v1.0.1 req-proposal-7a25dd88, rollback kb-v1.0.0 -]
```

回滚后 `current.snapshot_hash == 01f4f37f51d3`，与发布前逐位相同。这条是"版本可回退"能被断言的前提——只比实体数不够，必须比哈希。

版本号按数值排序（`kb-v1.0.10` 排在 `kb-v1.0.9` 之后），`KnowledgeVersion.parts` 负责，字符串排序会错。

## 7. 安全默认：未表态不等于同意

`publish` 最初的实现是"未在 `decisions` 里出现的变更一律 KEEP"。这会把废止预案产出的 `review` 级候选直接写进正式版本——等于把"待人工确认"当成"已确认"。

改为按候选自身的评审结论决定默认值：

| 变更情形 | 无显式表态时的默认 |
| --- | --- |
| `add` / `modify`，候选为 `auto_accept` | 采纳 |
| `add` / `modify`，候选为 `review` / `drop` | **暂缓**，记入 `last_deferred`，不写入新版本 |
| `remove`（源头撤回） | 执行删除 |

`test_publish_does_not_auto_accept_review_level_changes` 用"改废止预案 v1 的处置步骤"构造场景：提案产生候选，不做任何显式确认直接发布，断言 `last_deferred` 非空且该动作未进入新版本。

这样做还有一个好处：**默认路径是可自动化的**（干净数据全自动通过），**风险路径必须有人点**（过期/低置信度必须显式 KEEP）。这正是验收点里"过期、证据不足是否安全处理"的可执行答案。

## 8. CLI（跨进程可用）

```bash
export PYTHONPATH=src
python -m decision_poc.knowledge_evolution init     --dataset samples/assets/urban --out artifacts/kb
python -m decision_poc.knowledge_evolution propose  --dataset <改动后目录>        --out artifacts/kb
python -m decision_poc.knowledge_evolution publish  --dataset <改动后目录>        --out artifacts/kb \
        --proposal <id> --drop <change_id> --note "只采纳职责关系"
python -m decision_poc.knowledge_evolution rollback --out artifacts/kb --version kb-v1.0.0
python -m decision_poc.knowledge_evolution status   --out artifacts/kb
```

落盘结构：

```
artifacts/kb/
├── registry.json          current / versions / proposals（含未决）
├── history.jsonl          publish / rollback / reject 审计流水
└── kb-v1.0.0/             entities.jsonl + relations.jsonl + snapshot.json
```

`registry.json` 让 `KnowledgeRegistry.load()` 能恢复未决提案——**这是 P5 MCP Tool 一次请求一个进程还能做人工确认的前提**，不是可选项。

## 9. 已知边界（P4/P5 前必须处理）

1. **`stale_claim_ids` 目前恒为空**：没有 claim 索引。P4 建好 `asset_id → triple → claim` 反向索引后，这里才有内容。
2. **`_trigger_ids` 是启发式**：比对 `bundle` 与 snapshot 的 asset_id 集合差异，集合相等时退化为"全部资产"。P5 应由调用方显式传入触发资产，CLI 只是演示兜底。
3. **`modify` 的 detail 统一写"属性或置信度变化"**：没有细到字段级 diff。要做"哪个字段从什么变成什么"，需要在 `KnowledgeChange` 加 `field_diffs`，属 P4 溯源范畴。
4. **提案不做冲突检测**：新候选与既有关系矛盾（如两个部门同时 `responsible_for` 同一泵站）现在只会出现两条关系，不报冲突。冲突判定是 C1/C2/C3 的一部分，归 P6 推演层。
