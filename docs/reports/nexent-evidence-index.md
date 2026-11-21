# Nexent 证据索引

更新时间：2026-11-21。这个索引把当前快照、历史快照和未完成取证分开，避免把不同时间的版本号混成一次运行。

## 当前快照

| 事实 | 证据 | 状态 |
|---|---|---|
| 主 Agent 当前发布版本为 v34（`urban-mcp-v5`） | `samples/nexent/live/agents.json` 的 `urban_deduction_agent.current_version_no` | 2026-11-20 北向接口快照 |
| 协同 Agent 当前发布版本为 v12（`urban-collab-v1`） | `artifacts/ledger/platform_versions.json` 的 `urban_collab_agent.current_version`、`samples/nexent/live/agents.json` | 2026-11-21 北向接口实测 |
| `urban_asset_qa` 当前发布版本为 v11（`urban-asset-qa-v4`） | `artifacts/ledger/platform_versions.json` 的 `urban_asset_qa.current_version` | 2026-11-21 北向接口实测；提示词增加 final_answer 角标清理要求 |
| 主链与协同链在同一事件内串联通过 | `samples/nexent/live/chain_live.json`，conversation `313 → 314`，`chain_ok=true`，指纹 `d7fa402fe22e596c` | 当前 Docker 实测；同一事件 `WO-20261029-007` |
| 协同链重复运行稳定 | `samples/nexent/live/collab_live.json`，conversation `315/316/317` | 当前 3/3 解析、指纹和约束校验通过 |
| 多事件回归覆盖四个工单 | `samples/nexent/live/multi_event_regression.json`，`agent_version=34`，conversation `319/320/321/322` | 2026-11-21 19:45 北向实测，四个事件各一次对话均 8/8，未重试 |
| 平台问答命中三类来源 | `samples/nexent/live/platform_qa_current.json` | 2026-11-21 北向接口实测 3/3 预期来源命中；本次 2/3 问答无角标、1/3 仍返回 `[[a1]]`，提示词约束不是稳定门禁 |
| 平台知识库版本快照已同步 | `deploy/nexent/sync_policy_ledger.py --apply --latest-only`、`artifacts/ledger/platform_versions.json` | 已将 `platform-versions://snapshot` 更新到 v34 |
| 15 个 MCP 工具分组与代码一致 | `samples/nexent/tools.json`、`src/decision_poc/mcp_server.py` | 已有快照 |
| 平台技能绑定与三张界面截图 | `samples/nexent/live/skill_bindings.json`、`docs/reports/captures/nexent/` | 截图为历史采集，换环境需重拍 |

## 架构边界

这部分记录已经核实的设计边界，不列入“尚未取得的外部证据”。

| 边界事实 | 证据 | 口径 |
|---|---|---|
| 平台当前是 3 个业务 Agent 与 3 个对照 Agent 平级运行 | 2026-11-21 Northbound `GET /nb/v1/agents` 只读探针；`samples/nexent/live/agents.json` | 6 个 Agent 均 `is_available=true`；`is_main_agent=true` 只表示可作为对话入口，不表示主从关系 |
| 当前 6 个 Agent 未发布可发现的 A2A Agent Card | 2026-11-21 对 Agent 名称执行 `/nb/a2a/{name}/.well-known/agent-card.json`、`/nb/a2a/{name}`、`/nb/v1/agents/{name}/agent-card.json` 探针均为 `404`；列表不含 `endpoint_id` | 当前租户不宣称 A2A 或 sub-agent 关系已启用 |
| 跨角色协同在 MCP 工具层内确定性完成 | `src/decision_poc/mcp_server.py::_collab_run`、`src/decision_poc/collab/orchestrator.py::orchestrate` | 一次 `collab_run` 进程内调用产出动作卡、交接消息、联合发布对与裁决顺序；不经过 Agent 间 LLM 对话 |
| 平台具备 A2A 源码能力不等于本项目已启用 A2A | `docs/nexent-northbound-transport-design.md` 的 A2A 契约；本节当前租户探针结果 | 平台能力、当前租户配置和本项目运行链路分开表述 |

## 历史快照

| 事实 | 证据 | 口径 |
|---|---|---|
| 主链 8/8 与工具调用明细 | `samples/nexent/live/live_evidence.json`、`docs/reports/nexent-live-evidence.md` | 抓取文件保留原始时点，报告当前版本行读取 `agents.json` |
| 多事件回归（v19） | `samples/nexent/live/multi_event_regression.json` 的旧版本由 git 历史保留 | 采集时 Agent v19，不能当作 v34 的回归；v34 结果见当前快照表 |
| 三张平台截图 | `docs/reports/captures/nexent/platform_ui.json` | 截图采集时页面期望为 v31，不能证明当前页面仍为 v31 |

## 尚未取得的外部证据

- **问答全过程**录屏仍未取得。2026-11-21 已录到界面演示（MCP health、六个 Agent 与版本号、知识库索引、`/evolution` 台账），但问答段落无法录：平台前端渲染回答时抛 `Minified React error #185`（Maximum update depth exceeded），主推演、协同、以及缩短到 250 字的问法均复现，属平台前端缺陷而非内容过长。问答证据仍以 JSON 为准。
- Nexent 分层记忆的真实平台读写闭环尚未取得独立运行快照，当前仅有离线同构契约。
- `data-gate-report.md` 中一项高程基准、逐字段来源补齐，以及前池库容曲线的图纸或实测来源，仓库和公开检索均没有对应原始资料。
- 平台运行时系统提示词要求检索型回答保留 `[[字母+数字]]` 引用标记，当前没有发现 Agent 配置项可关闭。`urban_asset_qa` 提示词已要求提交前删除角标，但 2026-11-21 三问实测仍有 1/3 返回 `[[a1]]`。网页端已有展示层清理，北向原始 `final_answer` 仍需按 `marker_free` 字段验收。

## 检查命令

```powershell
uv run --locked python scripts/nexent_evidence_pack.py --check
uv run --locked python scripts/nexent_evidence_pack.py
```

报告生成器不修改历史抓取 JSON，只读取快照并重新生成可回查的 Markdown。
