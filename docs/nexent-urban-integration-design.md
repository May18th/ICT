# 城市内涝主链路上 Nexent 的集成设计（考点⑤）

- 日期：2026-10-30
- 背景：`docs/competition-gap-audit.md` 把"基于 Nexent 可运行"列为最高风险项——本仓库此前只有设备振动
  场景在 Nexent 上跑通，城市内涝主链路（六跳 + 冲突 + 分配 + 进化）从未进过平台。
- 目标：让**同一个智能体**在 Nexent 上完成内涝推演，且结论与离线可复现结果逐项一致。

## 1 根因与结论

Nexent 的「MCP 服务接入」只支持 HTTP/SSE（远程链接 / 容器 / API 转 MCP），而我们的 MCP 只有 stdio。
这不是能力缺口，是**传输缺口**：

```
离线：SkillPipeline（Python）→ MCP 工具（进程内调用）
平台：Nexent Agent（LLM 编排）→ MCP 工具（必须 HTTP/SSE）→ 结论
```

补上 `mcp_sse` 传输后，Nexent 侧看到的就是**同一份工具定义**（12 个），不存在第二套实现。

## 2 目标架构

```
┌──────────────────────────── Nexent（Docker，v2.7.0 本机） ────────────────────────────┐
│  Agent「urban-deduction-agent」                                                      │
│    ├─ 模型：openPangu / 平台已配置模型                                                │
│    ├─ 工具：MCP「urban-twin-mcp」→ http://host.docker.internal:8000/sse（13 个工具）  │
│    ├─ 技能：urban-six-hop-deduction（六跳 SOP）                                       │
│    │        scarcity-allocation-evolution（分配/进化/回滚 SOP）                       │
│    └─ 提示词：要求调工具取证、按 JSON 作答、错误码不得降级                            │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ /nb/v1/chat/run（SSE）
                                       ▼
┌──────────────────── 本仓库（宿主机，离线内核） ────────────────────┐
│  mcp_sse（HTTP/SSE 传输）→ McpServer → 13 个工具                    │
│  （semantic_query / event_deduce / conflict_check / allocation_plan │
│    / scene_render / scene_compare / ...）                            │
│  工具日志：artifacts/visual/tool_log.jsonl                            │
└──────────────────────────────────────────────────────────────────────┘
```

关键取舍：

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 传输 | 标准库手写 SSE，不引 FastMCP | 零新依赖；工具定义只保留 `mcp_server.py` 一份 |
| Skill | SKILL.md 写"编排 SOP"，不写业务代码 | 业务判定在 MCP 工具里；Skill 只约束 Agent 的调用顺序与失败处理 |
| 验证 | `nexent_urban` 做**结论比对**，不只看 JSON 可解析 | "跑通"不等于"跑对"；缺口/冲突/序位必须与离线一致 |
| 状态 | LLM 编排 + 确定性工具 | 不让模型生成推演结论，避免"看起来像但不可复核" |

## 3 落地步骤（现场可复现）

### 3.1 启动 MCP SSE（宿主机）

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_sse --port 8000
# MCP SSE 已启动：http://127.0.0.1:8000/sse
```

健康检查（应返回 13 个工具、含 `event_deduce` / `allocation_plan`）：

```powershell
curl.exe http://127.0.0.1:8000/health
```

### 3.2 在 Nexent 注册 MCP

1. 智能体开发 → 选择 Agent 的工具 → 右侧「MCP 配置」；
2. 名称 `urban-twin-mcp`，URL 填 `http://host.docker.internal:8000/sse`
   （Docker 里的 Nexent 访问宿主机必须用 `host.docker.internal`，不能用 127.0.0.1）；
3. 「连通性校验」→ 保存；工具列表应出现 13 个工具。

### 3.3 上传 Skills

技能仓库 → 我的 Skill → 创建 → 上传技能文件，逐个上传：

- `samples/nexent/skill/urban-six-hop-deduction/SKILL.md`
- `samples/nexent/skill/scarcity-allocation-evolution/SKILL.md`

### 3.4 创建并发布 Agent

- 工具：勾选 13 个 MCP 工具 + 2 个 Skills；
- 提示词（最小可用版本，可直接粘贴）：

```
你是城市应急决策智能体。用户给出事件编号与时点后：
1) 必须调用 MCP 工具取证（event_deduce / twin_neighbors / conflict_check /
   allocation_plan / evidence_locate），不得凭记忆作答；
2) 按 urban-six-hop-deduction 技能的六跳顺序组织结论，三类冲突全部保留；
3) 需要分配稀缺资源时按 scarcity-allocation-evolution 技能调用 allocation_plan；
4) 工具返回 PERMISSION_DENIED / EVIDENCE_INSUFFICIENT 时如实声明，不得删除依据后照给结论；
5) 最终严格按给定 JSON 结构输出，不要 Markdown 围栏和解释。
```

- 发布 → 生成 Access Key。

### 3.5 真机验证（结论级）

```powershell
$env:NEXENT_BASE_URL   = "http://localhost:5013"
$env:NEXENT_API_KEY    = "<Access Key>"
$env:NEXENT_AGENT_NAME = "urban-deduction-agent"
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.nexent_urban --json samples/eval/nexent_urban_smoke.json
```

期望：`Nexent 城市内涝主链路：8/8 项一致；conversation_id=<n>`，退出码 0。
任一不一致会打印 `! 检查项: 期望 X 实际 Y` 并返回 3——**不允许看到 JSON 就算过**。

## 4 八项一致性检查

| 检查 | 期望（来自离线推演） |
| --- | --- |
| 六跳齐全 | observed / affected / owner / conflict / resource / action |
| 冲突编号 | C1 / C2 / C3 |
| 冲突类型 | overlap / gap / contention |
| 资源缺口 | 1 台 |
| 隔离先于抽排 | true |
| 关键处置动作 | 隔离风险源 / 抽排 / 封闭积水路段 均出现 |
| 证据非空 | 至少一条 `asset_id#locator` |
| 事件编号 | WO-20261029-007 |

## 5 答辩要用的证据（缺一不可）

| 证据 | 获取方式 |
| --- | --- |
| MCP 服务在线 | `/health` 截图（含 13 工具）+ 终端启动日志 |
| Nexent 已接入 MCP | MCP 配置页截图（URL 与连通性校验通过） |
| Agent 挂载工具/Skills | Agent 编辑页工具列表截图 |
| 平台对话 | 浏览器对话截图（question + 工具调用 + JSON 结论） |
| 结论一致 | `nexent_urban` 的 8/8 输出 + JSON 报告 |
| 工具调用留痕 | `artifacts/visual/tool_log.jsonl`（每次工具调用一条） |
| 会话编号 | `conversation_id`（北向响应头/事件） |

## 6 验收口径

- [ ] `/health` 返回 13 个工具，SSE 握手可被独立的 MCP 客户端完成（已离线测试：10 项）。
- [ ] Nexent MCP 配置「连通性校验」通过，工具列表可见。
- [ ] Agent 一次对话产出六跳 JSON，且 8/8 与离线一致（conversation_id 留档）。
- [ ] 工具日志里能看到 `event_deduce`、`conflict_check`、`allocation_plan` 至少各一次。
- [ ] 若 Agent 未调工具直接作答（凭记忆），结论比对必须失败——这是"是不是真编排"的区分点。

## 7 风险与边界

| 风险 | 对策 |
| --- | --- |
| Nexent v2.7.0 的 MCP 入口与文档版本不一致 | 以实测 UI 为准；若 v2.7.0 无 MCP 配置入口，退路是"API 转 MCP"：把工具封成 HTTP REST 再转 MCP |
| LLM 输出格式漂移 | 查询里带完整 JSON 结构 + 失败即失败（退出码 3），不降标准 |
| 工具调用失败导致答案不完整 | SKILL 明确失败语义；`nexent_urban` 会把缺项列为失败 |
| SSE 服务无鉴权 | 只绑 127.0.0.1，仅本地演示；上公网需反代 + 鉴权，不在本次范围 |
| 并行开发冲突 | `mcp_sse.py` 为独立模块，不改 `mcp_server.py`；工具增减自动跟随主线 |

## 8 当前状态（2026-10-30 更新：真机已通）

- 已完成（离线可验证）：SSE 传输（`mcp_sse.py` + 10 项测试）、2 个 SKILL.md、`nexent_urban` 查询/解析/比对（11 项测试）、
  `NexentHttpTransport` 支持注入 `query_builder`（默认行为不变）、`trust_env=False` 避免系统代理打挂本地调用。
- **已完成（真机）**：本机 Nexent v2.7.0 上注册 13 个 MCP 工具（tool_id 41–53）、创建并发布 Agent
  `urban_deduction_agent`（agent_id=3，v10）、一次真实对话六跳结论与离线 **8/8 一致**（conversation_id=78），
  平台调用日志含 `event_deduce`（含一次协议修复重试）。
  证据：`samples/nexent/live/`；报告：[真机集成报告](reports/nexent-live-integration-report.md)。
- **已完成（平台侧）**：2 个 SKILL.md 已上传平台技能仓库（skill_id=2/3，幂等脚本 `deploy/nexent/upload_skills.py`），证据 `samples/nexent/live/skills.json`；平台界面截图 3 张 + sha256 证据（`deploy/nexent/capture_platform_ui.py` → `docs/reports/captures/nexent/`），报告 [平台侧证据报告](reports/platform-evidence-report.md)。
  `twin_neighbors` 的 `twin_id` 参数提示词修正。

### 8.1 实际装配路径（与 §3 的差异）

原 §3 写的是 UI 点选步骤；真机执行时改为可复现脚本（平台 API + JWT），流程等价但可审计：

1. `deploy/mcp_sse/Dockerfile` 构建 `urban-twin-mcp:dev`，以 `--network nexent_network` 运行；
2. `deploy/nexent/setup_nexent.py`（在 `nexent-config` 容器内）依次：注册 MCP → `scan_tool` 同步 13 工具 →
   创建/复用 Agent 并挂工具 → 发布版本；
3. 宿主机 `python -m decision_poc.nexent_urban` 做结论级校验；
4. `deploy/nexent/capture_evidence.py` 固化证据。
   详见 [deploy/nexent/README.md](../deploy/nexent/README.md)。

## 9 多智能体协同 Agent（P12 / T5，路径 B）

- 工具：13 个（新增 `collab_run`，分组 `collaboration`）；`tools.json` 由代码导出，4 个互斥分组。
- 新增 Agent `urban_collab_agent`（`deploy/nexent/setup_nexent.py create-collab-agent`）：
  挂 `collab_run` + `event_deduce` + `conflict_check` + `allocation_plan` + `twin_neighbors` + `evidence_locate`。
- 提示词两步式：先 `collab_run`，再转述 `cards / co_sign_pairs / merge_order / conflicts / kernel_fingerprint`。
- 为什么单开一个 Agent：主 Agent 的 8 项结论一致性校验依赖固定 JSON 结构，
  把"部门动作卡"塞进同一份回答会让两种产出互相污染。
- 未验证项：Nexent v2.7 的 Agent-as-Tool（路径 A，5 个部门 Agent 真机互调）**没有证据**，
  规格里已标"无法确认"，因此只交付路径 B。
