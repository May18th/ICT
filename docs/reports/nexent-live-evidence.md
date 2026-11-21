# Nexent 平台实跑证据包

> 自动生成于 2026-11-21 18:33 +0800，**禁止手改**。生成命令：`uv run --locked python scripts/nexent_evidence_pack.py`
>
> 数据来自 `samples/nexent/live/` 的平台抓取产物与北向服务日志，均为**历史快照**。
> 本文不给结论性评价，只把散落记录整理成可逐条回查的事实。每条后面标了来源。

## 摘要

| 项 | 值 | 来源 |
|---|---|---|
| 平台地址 | `http://localhost:5013` | 〔源: live_evidence.json → nexent_base_url〕 |
| 主推演 Agent | `urban_deduction_agent` / 城市内涝跨部门决策智能体 | 〔源: agents.json → data[].name〕 |
| 当前版本 | **v32**（`urban-mcp-v5`） | 〔源: agents.json → current_version_no〕 |
| 模型 | `DeepSeek-Chat` | 〔源: agents.json → model_name〕 |
| 已记录 Agent | 6 个 | 〔源: agents.json〕 |
| MCP 工具 | 15 项，分 4 组 | 〔源: src/decision_poc/mcp_server.py → TOOL_GROUPS〕 |
| 主链路一致性 | 8/8 | 〔源: live_evidence.json → checks_passed〕 |
| 工具调用记录 | 6 次 | 〔源: live_evidence.json → tool_calls〕 |
| 北向日志跨度 | 2026-10-22 01:56:17 ~ 2026-11-21 18:31:48 | 〔源: northbound/（21 个文件）〕 |
| 北向请求累计 | 511 次（服务端 access log） | 〔源: northbound〕 |

## A1 平台与模型

平台上已发布的 Agent 与各自版本。**版本号是平台侧的 `current_version_no`，不是代码里的常量。**

| Agent | 显示名 | 版本 | 版本名 | 模型 | 可用 | 用途 |
|---|---|---|---|---|---|---|
| `urban_collab_control_no_tools` | 对照：无 MCP 工具的协同总控 | v2 | `control-collab-no-tools-v1` | DeepSeek-Chat | 是 | 实验对照组。提示词/模型与协同 Agent 完全相同，仅解绑协同工具，用于证明动作卡与处置顺序来自 collab_run |
| `urban_asset_qa` | 内涝资产台账问答 | v10 | `urban-asset-qa-v3` | DeepSeek-Chat | 是 | 基于平台知识库回答设施状态、路段属性、部门职责与预案条款；每条结论标出处 |
| `urban_control_no_tools` | 对照：无 MCP 工具的城市内涝智能体 | v2 | `control-no-tools-v1` | DeepSeek-Chat | 是 | 实验对照组。提示词/模型与主 Agent 完全相同，仅解绑 15 个 MCP 工具，用于证明八项一致来自工具而非模型先验 |
| `urban_llm_decision_agent` | LLM 决策对照智能体 | v2 | `llm-decision-v2` | DeepSeek-Chat | 是 | 类型化决策任务的 LLM 对照：在确定性推演给定的选项内选择并解释，与 P8 A/B 同口径 |
| `urban_collab_agent` | 城市内涝应急协同总控 | v10 | `urban-collab-v1` | DeepSeek-Chat | 是 | 把六跳推演拆成 1 总控 + 5 专业部门的动作卡，编排联合发布与处置顺序 |
| `urban_deduction_agent` | 城市内涝跨部门决策智能体 | v32 | `urban-mcp-v5` | DeepSeek-Chat | 是 | 基于 Nexent + MCP 的城市内涝跨部门推演：六跳结论、三类冲突、稀缺资源分配与证据链 |

平台版本说明（原文）：Nexent v2.7.0 真机：平台 Agent（v31）调用 MCP 工具后产出六跳结论，与离线推演逐项比对

## A2 · 工具分组

分组是互斥分区：一个工具只属于一组。`presentation` 类只在决策定稿后启用，
目的是不让智能体在推演循环里把推理预算花在换镜头上。

| 分组 | 数量 | 工具 |
|---|---|---|
| `decision` | 10 | `semantic_query`、`twin_neighbors`、`doc_search`、`evidence_locate`、`claim_trace`、`provenance_verify`、`event_deduce`、`counterfactual_run`、`conflict_check`、`allocation_plan` |
| `presentation` | 3 | `scene_render`、`scene_status`、`scene_compare` |
| `bootstrap` | 1 | `asset_lookup` |
| `collaboration` | 1 | `collab_run` |
| **合计** | **15** | — |

## A3 · 工具调用流水

平台侧抓到的每一次 MCP 调用。`snapshot_hash` 相同表示读的是同一份数据快照。

| 指标 | 值 |
|---|---|
| 调用总数 | 6 |
| 状态分布 | ok × 6 |
| 来源分布 | mcp × 6 |
| 成功占比 | 100.0% |
| 延迟 p50 / p95 | 19 ms / 36 ms |
| 数据快照（去重） | 1 个 |
| 知识库版本 | kb-v1.0.0 |

按工具：

| 工具 | 次数 |
|---|---|
| `event_deduce` | 4 |
| `doc_search` | 2 |

这张表是**单次会话**的抓取样本（一次对话里智能体实际发起的工具调用）。
平台整体的请求量看 A7 的服务端日志——那是全部会话的累计，两者口径不同，不要相加。

<details><summary>逐条调用记录</summary>

| 时刻 | 工具 | 状态 | 延迟 | snapshot |
|---|---|---|---|---|
| 2026-11-13T03:58:39+0000 | `event_deduce` | ok | 19 ms | `d102aa807648` |
| 2026-11-13T03:58:47+0000 | `event_deduce` | ok | 7 ms | `d102aa807648` |
| 2026-11-13T03:58:49+0000 | `doc_search` | ok | 36 ms | `d102aa807648` |
| 2026-11-13T04:05:31+0000 | `event_deduce` | ok | 20 ms | `d102aa807648` |
| 2026-11-13T04:05:32+0000 | `doc_search` | ok | 34 ms | `d102aa807648` |
| 2026-11-13T04:05:41+0000 | `event_deduce` | ok | 14 ms | `d102aa807648` |

</details>

## A4 无工具对照组

**这一节证明的是：决策结论来自 MCP 工具，不是模型先验。**

做法：平台上另发两个 Agent，提示词与模型和主 Agent **完全相同**，只把工具解绑。
如果结论照样给得出来，说明工具是装饰；如果给不出来，说明工具是唯一来源。

| 对照组文件 | 对话 | 重试 | 检查通过 | 未通过项 | 转人工 | 冲突数 | 动作数 |
|---|---|---|---|---|---|---|---|
| `control_no_tools.json` | 136 | 2 | **2/8** | 冲突编号、冲突类型、资源缺口、隔离先于抽排、关键处置动作齐全、证据非空 | 是 | 0 | 0 |
| `control_main_smoke.json` | 200 | 1 | **2/8** | 冲突编号、冲突类型、资源缺口、隔离先于抽排、关键处置动作齐全、证据非空 | 是 | 0 | 0 |

`control_no_tools.json` 的结论原文：

> 无法完成六跳推演：本轮运行环境未注入 MCP 工具 event_deduce（工具调用不可见，报 NameError），六跳事实、三类冲突、资源缺口与处置顺序均无法取证，按规则不输出结论。

`control_main_smoke.json` 的结论原文：

> 事件六跳事实与三类冲突无法获取：event_deduce 与 evidence_locate 工具均未挂载到本运行时，无法核对原文依据，故不作任何结论性推断。

对照组的意义在数字上：无工具时检查基本全灭、冲突与动作都是 0 条、主动标记转人工。
〔源: control_no_tools.json → checks / answer.handoff_required〕

## A5 同一题多轮重跑的一致性

同一决策题（`N01`）在平台上重跑 3 次，逐字段比对。

| 结论 | 字段 |
|---|---|
| 完全一致 | `schema_version`、`method`、`nexent_base_url`、`agent_name`、`case_id`、`event_id`、`as_of`、`expected_option_id`、`options`、`task_digest`、`schema_valid`、`selected_option_id`、`decision_error`、`selected_match` |
| 有漂移 | `confidence`、`summary`、`risks`、`evidence_cited` |

漂移字段（原文过长，这里截断；完整内容见源文件）：

- `confidence`：0.86 ｜ 0.86 ｜ 0.93
- `summary`：2026-10-29T09:40 橙色预警、积水 30cm 时点，推演给出的第 1 优先动作（action_priori… ｜ 橙色预警期间，兴华路 K0+320 积水深度 30.0cm，已超 25cm 封路阈值（假设值，待核实），且该点受兴华路燃… ｜ 2026-10-29T09:40:00+08:00 时点 N01 常规内涝事件（WO-20261029-007，橙色预警…
- `risks`：基坑长时间覆盖/围堰若排水不畅，可能因积水浸泡导致边坡失稳；积水 30cm 下燃气浓度检测受限，存在泄漏未被及时发现的叠… ｜ 封路须由 ORG-ROAD 与 ORG-TRAFFIC 联合发布（冲突 C1），单一部门发布无效，审批或存在时延；DF-… ｜ 在施燃气主干管更换作业（开挖 2.5m）叠加橙色预警与 30cm 积水，存在燃气泄漏与次生燃爆风险，须先确认无泄漏再进入…
- `evidence_cited`：4 ｜ 7 ｜ 6

决策字段（选项、结构合法性、是否与期望一致）没有漂移；漂移集中在措辞、
置信度与引用条数上——模型每次组织证据的方式不同，但落点相同。

〔源: llm_decision_platform_run1..3.json〕

## A6 多事件回归

采集于 2026-11-06T18:54:26+0800，Agent `urban_deduction_agent` 当时版本 v19，模型 `DeepSeek-Chat`。

| 事件 | 对话 | 重试 | 一致性 | 失败项 |
|---|---|---|---|---|
| `WO-20261029-007` | 141 | 1 | **8/8** | — |
| `WO-20261029-009` | 140 | 1 | **8/8** | — |
| `WO-20261030-021` | 142 | 1 | **8/8** | — |
| `WO-20261026-013` | 143 | 1 | **8/8** | — |

记录备注（原文）：验收脚本原先只认 WO-20261029-007：key_actions 写死「隔离风险源/抽排/封闭积水路段」，009 属于基坑+燃气场景、没有封控类动作，被误判 7/8。改成按动作族从离线推演派生后，四次对话均 8/8。

注意表里的版本号是**采集当时**的版本，与 A1 的当前版本不是同一个时刻。
〔源: multi_event_regression.json → agent_version〕

## A7 北向服务日志时间线

日志目录 `D:\华为ICT\Nexent-data\logs\northbound`，共 21 个文件，跨度 2026-10-22 01:56:17 ~ 2026-11-21 18:31:48。

| 指标 | 值 |
|---|---|
| 服务启动次数 | 55 |
| 北向请求总数 | 511 |
| 状态分布 | 200 × 482、400 × 2、401 × 10、403 × 2、404 × 14、429 × 1 |
| 创建会话数 | 275（最大 id 312） |

按天：

| 日期 | 启动 | 北向请求 |
|---|---|---|
| 2026-10-22 | 9 | 0 |
| 2026-10-23 | 3 | 0 |
| 2026-10-24 | 1 | 0 |
| 2026-10-26 | 1 | 0 |
| 2026-10-29 | 3 | 40 |
| 2026-11-02 | 7 | 15 |
| 2026-11-03 | 1 | 66 |
| 2026-11-04 | 3 | 8 |
| 2026-11-05 | 1 | 9 |
| 2026-11-06 | 2 | 62 |
| 2026-11-07 | 1 | 12 |
| 2026-11-11 | 5 | 31 |
| 2026-11-13 | 3 | 100 |
| 2026-11-14 | 2 | 37 |
| 2026-11-15 | 1 | 0 |
| 2026-11-16 | 3 | 0 |
| 2026-11-17 | 1 | 5 |
| 2026-11-18 | 1 | 10 |
| 2026-11-19 | 2 | 25 |
| 2026-11-20 | 3 | 28 |
| 2026-11-21 | 2 | 63 |

**这些是服务端 access log，不是本地 mock 记录。**请求从容器网络进来，
每次创建会话都有递增 id，刷新服务重启与请求的时间关系可以对照。

## 附：证据文件清单

| 文件 | 字节 | 修改时间 | sha256 前 12 |
|---|---|---|---|
| `agent_export.json` | 1817 | 2026-11-21 13:15 | `40c8e94bbb5c` |
| `agents.json` | 7521 | 2026-11-21 13:15 | `4a454f722672` |
| `asset_qa_multihop.json` | 4048 | 2026-11-21 13:15 | `ee96abd8d01c` |
| `asset_qa_reconcile.json` | 5320 | 2026-11-21 13:15 | `68fdf59948a1` |
| `chain_live.json` | 2105 | 2026-11-21 18:31 | `8ac9b2b00480` |
| `collab_ab.json` | 2941 | 2026-11-21 13:15 | `45885d1cf630` |
| `collab_live.json` | 2691 | 2026-11-21 18:31 | `69ca52f1c40a` |
| `collab_v10_retest.json` | 1313 | 2026-11-21 13:15 | `479fe01c470e` |
| `collab_v12_retest.json` | 2138 | 2026-11-21 13:15 | `68692968c8f4` |
| `collab_v12_retest_run2.json` | 1640 | 2026-11-21 13:15 | `0ed74a7365c5` |
| `collab_v7.json` | 1314 | 2026-11-21 13:15 | `93fcc79c0ee6` |
| `collab_v9_retest.json` | 1314 | 2026-11-21 13:15 | `49dadd5bfc18` |
| `control_main_smoke.json` | 2196 | 2026-11-21 13:15 | `323ec3530247` |
| `control_no_tools.json` | 2292 | 2026-11-21 13:15 | `7c837f3b7325` |
| `conversation_119.json` | 4822 | 2026-11-21 13:15 | `4a7405e1a2ce` |
| `conversation_120.json` | 4931 | 2026-11-21 13:15 | `29972ef2d51a` |
| `conversation_148.json` | 4662 | 2026-11-21 13:15 | `d34051275aa9` |
| `conversation_179.json` | 4755 | 2026-11-21 13:15 | `18b0c5573470` |
| `conversation_78.json` | 6627 | 2026-11-21 13:15 | `7c013c7944de` |
| `live_evidence.json` | 3732 | 2026-11-21 13:15 | `4d9566ca4180` |
| `llm_decision_platform.json` | 3149 | 2026-11-21 13:15 | `fc848a623c74` |
| `llm_decision_platform_run1.json` | 2971 | 2026-11-21 13:15 | `17832824de90` |
| `llm_decision_platform_run2.json` | 2974 | 2026-11-21 13:15 | `91bdc4a6d82b` |
| `llm_decision_platform_run3.json` | 3149 | 2026-11-21 13:15 | `fc848a623c74` |
| `main_v19_baseline.json` | 4693 | 2026-11-21 13:15 | `7373eef5a49d` |
| `multi_event_regression.json` | 2415 | 2026-11-21 13:15 | `b02f5a56491d` |
| `precedent_ab.json` | 3657 | 2026-11-21 13:15 | `719f5775a2bf` |
| `precedent_live.json` | 4654 | 2026-11-21 13:15 | `060fe3adb9d7` |
| `skill_bindings.json` | 1113 | 2026-11-21 13:15 | `5b1614d0045c` |
| `skills.json` | 2825 | 2026-11-21 13:15 | `7ed2eb823223` |
| `urban_asset_qa.export.json` | 9922 | 2026-11-21 13:15 | `32a878d6e6c9` |
| `urban_asset_qa.export_meta.json` | 324 | 2026-11-21 13:15 | `5b60fdaf8129` |
| `urban_collab_agent.export.json` | 4090 | 2026-11-21 13:15 | `2f63c902266f` |
| `urban_collab_agent.export_meta.json` | 327 | 2026-11-21 13:15 | `954ee147a1ee` |

哈希用于确认「这份报告读的就是这些字节」，换人复核时对一下即可。
