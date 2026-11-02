# Nexent 侧工具注册（P10 增补）

## 结论：**已真机注册并跑通**（2026-10-30 更新）

> 历史：本文档较早版本记录的是"Docker 起不来、未真机注册"的阻塞状态。该阻塞已解除：
> Docker Desktop 恢复后，MCP 服务以 SSE 方式跑在 `nexent_network` 容器里，13 个工具已注册进平台
> （tool_id 41–52）、挂到 Agent `urban_deduction_agent`（agent_id=3）并发布 v3；
> 真机对话 **conversation_id=21**，六跳结论与离线 **8/8 一致**，平台调用日志含
> `event_deduce` / `allocation_plan` / `scene_render`。
>
> 完整证据见 [真机集成报告](reports/nexent-live-integration-report.md)，装配步骤见
> [deploy/nexent/README.md](../deploy/nexent/README.md)。

已就绪的产物（本轮之后仍然有效）：

| 产物 | 状态 | 用途 |
| --- | --- | --- |
| `samples/nexent/tools.json` | 已生成 | 13 个工具的 schema、serverInfo、4 个分组。**生成物，勿手改** |
| `samples/nexent/skills.json` (1.2) | 已重新生成 | 新增 `scene_render` / `scene_compare` / `asset_lookup` 三个独立工具绑定（改的是 `skills.py` 的 `skill_manifest()`，不是手改 JSON） |
| `tests/test_nexent_tools.py` | 已通过 | 锁住"注册清单与代码同源"，防止走了散 |
| `docs/nexent-integration-boundary.md` | 既有 | 适配层职责边界，仍然有效 |

两张表都是**生成物**，改代码后重新导出，不要手改：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_server --export-tools samples/nexent/tools.json
uv run --locked python -m decision_poc.skills --manifest > samples/nexent/skills.json
```

## 前置：还有一个传输缺口，不只是 Docker

Docker 不可用（见文首）是当下的阻塞，但即使 Docker 恢复，
直接注册也跑不通：**Nexent 的「MCP 服务接入」只支持 HTTP/SSE，而我们的 MCP 是 stdio。**

```
离线：SkillPipeline（Python）→ MCP 工具（进程内，stdio）
平台：Nexent Agent（LLM 编排）→ MCP 工具（必须 HTTP/SSE 远程端点）
```

补 `mcp_sse` 传输层的设计见 [城市内涝主链路上 Nexent 的集成设计](nexent-urban-integration-design.md)。
本文只负责"工具清单与分组怎么注册"；**先补传输，再谈注册**，顺序不能颠倒。

## 为什么用生成物而不是手写

手抄 schema 一定会漂：加了工具忘同步、改了必填项没更新、分组写错。所以清单由代码导出：

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_server --export-tools samples/nexent/tools.json
# 已写出 samples\nexent\tools.json（13 个工具 / 4 个分组）
```

`tests/test_nexent_tools.py` 会比对 `tools.json` 与 `UrbanToolkit().tools()`，
不一致即测试失败——**忘了重新导出会被测试拦住**。

## 环境恢复后的注册步骤

### 1. 起 Nexent 并自检

```powershell
# Docker Desktop 起来后
docker ps --format "{{.Names}}\t{{.Status}}"
# 期望看到 nexent-northbound / nexent-postgresql / nexent-elasticsearch

Invoke-RestMethod "http://localhost:5013/nb/v1/health"
# 期望 {"status":"healthy","service":"northbound-api"}
```

完整的 Key 生成、清记忆、失败对照见 [演示运行手册](demo-runbook.md)。

### 2. 分组注册，不要一次全挂

| 阶段 | 挂哪些工具 | 理由 |
| --- | --- | --- |
| 推演 | `decision` 组（9 个） | 智能体的推理预算花在证据与推演上 |
| 呈现 | 追加 `presentation` 组（`scene_render`、`scene_compare`） | 决策定稿后才需要演 |
| 初始化 | `bootstrap` 组（`asset_lookup`） | 只核对资产，低频 |

服务端可用 `--groups` 直接限制可见工具：

```powershell
uv run --locked python -m decision_poc.mcp_server --groups decision
uv run --locked python -m decision_poc.mcp_server --groups decision,presentation
```

这样即使平台侧注册了全量，推演阶段智能体也看不见渲染工具。

### 3. 注册前先做一次离线冒烟

不依赖 Nexent，直接验证 MCP 进程能正常列工具、能被调用：

```powershell
$env:PYTHONPATH = "src"
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | uv run --locked python -m decision_poc.mcp_server --groups decision
'{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | uv run --locked python -m decision_poc.mcp_server --groups presentation
```

期望：第二条只返回 `scene_render`、`scene_compare` 两个工具。

### 4. 验收标准（缺一条就不算接入完成）

- 一轮问答产出六跳 + 分配方案 + 证据链，且与 CLI（`decision_poc.pipeline`）结果可核对；
- `scene_render` 出现在 `artifacts/visual/tool_log.jsonl` 里，且 `origin = "mcp"`
  （证明是智能体调的，不是人点的）；
- 留 `conversation_id`、请求截图、`artifacts/visual/jobs/<snapshot_id>.json` 三样证据。

## 表述边界（答辩时不要越界）

- ✅ 可以说：13 个 MCP 工具已实现并可被任意 MCP 客户端调用；注册清单由代码生成并有一致性测试。
- ✅ 可以说：已在本机 Nexent v2.7.0 真机注册 13 个工具并跑通内涝主链路（conversation_id=21，8/8 一致）。
- ✅ 可以说：2 个 SKILL.md 已上传到本机 Nexent 技能仓库（skill_id=2/3，正文逐位一致，见 `samples/nexent/live/skills.json`）。

真机证据已留档：`samples/nexent/live/`（Agent 列表、会话、MCP 调用日志、汇总 JSON）+
`samples/eval/nexent_urban_smoke.json`；平台界面截图 3 张已留档（`docs/reports/captures/nexent/`，脚本 `deploy/nexent/capture_platform_ui.py`），问答过程录屏仍待补。
