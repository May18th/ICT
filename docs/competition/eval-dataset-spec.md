# 评测数据集说明（初赛「评测」项）

- 日期：2026-11-06
- 官方依据：初赛要跑基础评测，且**需要评测数据集** `[官方赋能直播 16:48]`，来源见 [官方口径落库](official-brief-2026-11-06.md)
- 一句话结论：**数据集不是没有，是没包装成可交付形态**。用例集、期望值、口径推导、消融对照全都已存在（`samples/eval/` + `docs/urban-scenario-ground-truth.md`），缺的只是这份说明书 + 一条复跑命令。

## 1 两个评测用例集

| 用例集 | 文件 | 规模 | 测什么 |
| --- | --- | --- | --- |
| 推演评测集（P8） | `samples/eval/urban_cases.json` | **22 例** | 六跳结论、冲突识别、证据引用、越权/过期拒绝 |
| 分配策略评测集（P9） | `samples/eval/allocation_cases.json` | **12 例** | 跨区抽排资源分配、调运、优先级、关键缺口 |

### 1.1 推演评测集字段（`urban_cases.json`）

| 字段 | 含义 |
| --- | --- |
| `case_id` | 用例编号（`N`正常 / `C`冲突 / `I`资源不足 / `P`越权 / `E`过期） |
| `title` | 用例意图，评委读这一行就知道在测什么 |
| `kind` | `normal` / `conflict` / `insufficient` / `permission` / `expired` |
| `event_id` / `as_of` / `caller_labels` | 输入三元组：工单号、时点、调用方身份 |
| `expect` | 期望断言；**只声明可核查项，没写的字段不检查** |

`expect` 是一组可选断言键，22 例实际用到：`status`(19) / `publishable`(15) / `six_hops`(6) / `min_actions`(3) / `min_affected`(1) / `must_objects`(1) / `must_locators`(1) / `depth_cm`(3) / `warning_level`(5) / `citation_completeness`(1) / `gap`(5) / `conflict_kinds`(6) / `conflict_ids`(2) / `error_code`(3) / `refused`(5) / `no_warning`(4)。

覆盖分布：`normal` 7 / `insufficient` 6 / `expired` 4 / `conflict` 3 / `permission` 2；涉及 6 个事件（`WO-20261029-007` 11 例为主事件，其余 5 个事件覆盖不同时点与边界，含一个 `WO-2099-000` 未来时间用例）。

### 1.2 分配策略评测集字段（`allocation_cases.json`）

| 字段 | 含义 |
| --- | --- |
| `case_id` / `title` | 编号与意图 |
| `split` | `train` 8 / `holdout` 4 —— **有留出集，不是全量调参** |
| `basis` | 该用例期望值的**依据条款**（对应 `docs/urban-scenario-ground-truth.md` 跳 5 / CF2 等） |
| `signals` | 输入：各区水深、是否封路、是否有危险源、需求、本区可用 |
| `expect` | `allocation_by_district` / `transfers` / `priority_order` / `critical_unmet`，12 例全有 |

文件内自带声明：「期望值由 ground truth 跳 5 / CF2 口径推得，**不由实现反推**」——这句是防「用实现结果定义标准」的关键，答辩可直接引用。

## 2 真 / 合成声明（必须随数据集一起交）

| 数据 | 性质 |
| --- | --- |
| 推演评测集的事件与时点 | 基于仓库内 `samples/scenarios/vd02/` 场景数据，源头为**合成场景 + 2 份真实公开预案**（`asset_manifest` 里逐项标注来源与 sha256） |
| 分配策略评测集 12 例 | **受控合成场景**（文件 note 原文如此），不来自真实历史工单 |
| 真实事件 `WO-20261029-007` | 仅用于端到端演示，**不进分配评测集** |

不能说的话：这 22 + 12 例不是真实业务采样，不要说「用真实工单验证」。

## 3 五级消融基线（`samples/eval/ablation.json`，22 例全口径）

| 配置 | 标签 | six_hop | conflict | safety | expiry | citation | auditable | publishable | deliverable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `rules_only` | L1 规则基线 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.68 |
| `semantic_only` | L2 语义层 | 0.00 | 0.00 | 0.12 | 1.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `single_skill` | L3 单一推演技能 | 0.64 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 | 0.00 | 0.64 |
| `multi_skill` | L4 多技能编排 | 0.64 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 | 0.00 | 0.64 |
| `full_audited` | L5 全量 + 审计门（现状） | 0.64 | 1.00 | 1.00 | 1.00 | 0.64 | 0.64 | 0.36 | 0.64 |

`full_audited` 的分子/分母（全口径 22 例）：six_hop 14/22、conflict 6/6、safety 8/8、expiry 4/4、citation 14/22、auditable 14/22、publishable 8/22、deliverable 14/22。

统计方式：`leave-one-out`（留一法），`stats_mode` 字段写在文件里，`ablation.json.stats` 给出每个指标的 `loo_min` / `loo_max` / 被留出的用例。

## 4 两套 `six_hop_rate` 不矛盾（评委最容易追问的一处）

| 出处 | 值 | 口径 |
| --- | --- | --- |
| `ablation.json` / `latest.json.metrics`（全口径） | 0.64 | 14 / **22**，分母含越权、过期、资源不足这些**本就不该出六跳**的用例 |
| `latest.json.metrics.six_hop_rate`（合格口径） | 1.00 | 14 / **14**，分母只算 `eligible_cases` |

一句话说法：**「需要出六跳的 14 个用例全出、全对；剩下 8 个是不该出六跳的负向用例，正确行为是拒绝或降级，所以全口径下是 14/22。」**

## 5 现状指标快照（`samples/eval/latest.json`）

| 指标 | 值 |
| --- | --- |
| 用例通过 | 22 / 22，失败 0 |
| 资产解析 | 19 / 19，未识别列 0 |
| 本体三元组 | 123 条，8 类关系 |
| 定位符命中率 | 1.0 |
| 引用完整率 | 1.0（无依据主张 0） |
| 延迟 | p50 10.6 ms / p95 14.6 ms |
| 分类安全率 | conflict 3/3、expired 4/4、insufficient 6/6、normal 7/7、permission 2/2 |
| 数据集快照指纹 | `d102aa8076489dd431bfbabc882cec56047a3dfdb0010c52b92a2807e0c19891` |

快照指纹的作用：数据集一改，这个哈希就变，评测报告与数据集必须同步更新——评委可据此判断我们有没有偷换数据。

## 6 复跑命令

```bash
# 1) 推演评测（22 例）——入口是 urban_eval，不是 evaluation（后者无 CLI）
PYTHONPATH=src uv run --locked python -m decision_poc.urban_eval \
    --cases samples/eval/urban_cases.json

# 2) 分配策略评测（12 例，含 train/holdout）——入口是 policy_evolution
PYTHONPATH=src uv run --locked python -m decision_poc.policy_evolution \
    --cases samples/eval/allocation_cases.json

# 3) 五级消融对照（生成 ablation.json）
PYTHONPATH=src uv run --locked python -m decision_poc.ablation --check

# 4) 对外数字自检（108 项，改任何对外数字都会红）
PYTHONPATH=src uv run --locked python -m decision_poc.evidence_check
```

实测：第 4 条输出 `对外数字自检：108/108 项一致（期望指纹 f256ae66388432c4）`。

第 1 条实测输出（2026-11-06 实跑）：

```
用例 22/22 通过，失败 0；六跳 100%，引用完整 100%，检索命中 100%
资产解析 19/19，未识别列 0，三元组 123 / 8 类，p50 9ms p95 11ms
```

第 2 条实测输出（同一轮）：

```
第一次运行：训练偏差 0.2264 → 0.0000；留出 0.2182 → 0.0000；标签 flood-response-v1.2
轮次 3（采纳 2 / 拒绝 1）；关键片区未满足 15 → 6 台；应调拨未调拨 4 → 0
人工评审 REV-001（水务调度负责人（角色扮演））：评审偏差 0.1171 → 0.0000
第二次运行（记忆 M-3979c3901de5，相似度 1.0000）：起点偏差 0.0000，轮次 1
真实事件：v1 {"A-03": 4, "B-01": 2} → {"A-03": 6, "B-01": 0}
```

> 延迟数以 `latest.json` 的快照值为准（p50 10.6 / p95 14.6 ms），实跑会有毫秒级抖动。
> 材料里只写快照值，别写实跑值——否则两次数字不一致会被当成口径不稳。

### 6.1 这段输出就是"可进化"的量化证据

官方赛题叫「**可进化**决策智能体」，这一段是唯一能证明"进化真的发生了"的数字，比任何架构图都好用：

| 证据 | 数字 | 说明 |
| --- | --- | --- |
| 训练偏差下降 | 0.2264 → 0.0000 | 3 轮内收敛，采纳 2 条策略变更、拒绝 1 条 |
| **留出集偏差下降** | 0.2182 → 0.0000 | 关键：**没在留出集上调参**，泛化到了 holdout |
| 业务侧收益 | 关键片区未满足 15 → 6 台；应调拨未调拨 4 → 0 | 从技术指标换算成业务量 |
| 人工评审（角色扮演） | 0.1171 → 0.0000 | **必须标注"角色扮演"**，不是真实用户评分 |
| 跨进程记忆复用 | 相似度 1.0000 → 起点偏差 0.0000，只需 1 轮 | 证明经验可沉淀，第二轮回放不用重学 |

> 边界：最后一行的人工评审是**角色扮演**（评审人身份写在输出里），台账第 5 节已明确它不是真实用户评分。
> 材料里必须与"真实用户评分"分开写，不能合并成一个数。

> 踩坑记录：仓库里 `decision_poc.evaluation` 与 `decision_poc.allocation` **没有 `__main__`**，
> `python -m` 调用会静默退出（退出码 0、无输出），容易误判成"跑过了"。真实入口是
> `urban_eval` / `policy_evolution` / `ablation`。写材料时别把命令写错——评委照抄跑不通比不写还糟。

## 7 交付形态建议（评委怎么核）

初赛材料里给三样东西，评委 3 分钟能核完：

1. **数据集本体**：`urban_cases.json` + `allocation_cases.json`（连带本说明书），说明字段与期望值的推导依据；
2. **一份基线对照表**：即第 3 节五级消融 + 第 4 节口径解释，证明「指标不是凭空来的，去掉任一环节就掉」；
3. **复跑命令**：第 6 节四条，附实测输出。**必须能被评委在干净环境复跑出同一组数字**（当前 `latest.json` 的 22/22 与快照指纹即为对照基准）。

## 8 已知不足（写进材料别回避）

- 用例规模小（22 + 12），且**全部来自合成场景**，没有真实工单采样；属低资源条件下的自建评测集，不是行业基准。
- 分配评测集只有 12 例、4 例 holdout，holdout 仅够做「不调参也能过」的弱验证，不足以支撑统计显著性结论。
- 推演评测集里 11 例集中在同一主事件，事件多样性有限（6 个事件号）。
