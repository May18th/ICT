# P5 MCP Server（回传）

> 对应验收点：「是否使用 MCP + Skills，而不是普通聊天机器人」。
> 代码：`src/decision_poc/mcp_server.py`；测试：`tests/test_mcp_server.py`（32 项）。全量 **255 passed**。
> 注：本报告记录 P5 当时的 7 个工具。P6 扩展新增第 8 个工具 `counterfactual_run`（反事实重放），
> 见 [P6 反事实回传](p6-counterfactual-report.md)；工具表与测试计数已随之更新。

## 1. 结论先写

| 项 | 结果 |
| --- | --- |
| 工具数 | **7 个**（任务单要求），全部返回既有 `ToolResult` 信封 |
| 传输 | stdio + JSON-RPC 2.0，换行分隔；**零新依赖**（不引入 MCP SDK） |
| 协议方法 | `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call` |
| 错误契约 | 协议级 5 个标准码 + 业务级 10 个 `ErrorCode`，`retryable` 判定正确 |
| 权限边界 | `semantic_query` / `twin_neighbors` 按 `caller_labels`；`evidence_locate` 按资产 `visibility` |
| 可观测 | 每次调用一条日志（工具、状态、错误码、耗时、快照哈希），`trace_id` 透传 |
| 子进程实测 | 真实 spawn 起来跑完整握手 + 查询，退出码 0 |

## 2. 为什么不用 MCP SDK

验收里有一条是「能从干净环境按 README 复现」。这个项目 15 个资产、7 个工具，引一个协议库换来的收益很小，代价是多一条依赖链、多一个版本漂移点。

协议本身只用到一个子集：握手、列工具、调工具、通知、错误。手写的风险是写错帧格式，所以用**两种方式**验证——直接调 `handle()` 验协议语义，再真的 spawn 一次子进程验帧格式（编码、缓冲、退出码）。

传输层做了拆分：`McpServer.handle()` 与 IO 无关，`serve_stdio()` 只是套在上面的循环。**Nexent 要 HTTP/SSE 时，是给同一个 `handle()` 再写一个循环，不是重写服务。**

## 3. 七个工具

| 工具 | 输入 | 输出要点 | 状态 |
| --- | --- | --- | --- |
| `semantic_query` | `query_id` / `as_of` / `seed_ids` / 谓词与类型过滤 / `caller_labels` / `block_expansion_through` | triples、objects、`snapshot_hash`、`knowledge_version` | 可用 |
| `twin_neighbors` | `twin_id` / `as_of` / `max_hops` / `caller_labels` | 邻接三元组 + 对象摘要 | 可用 |
| `evidence_locate` | `evidence_ref`（`asset_id#locator`）/ `caller_labels` | 表格行（含解析后的字段）、文段行号区间、图片标注区域 | 可用 |
| `claim_trace` | `claim_id` | 来源资产 + 定位 + 指纹、规则出处、记录 id、决策 id、快照哈希 | 可用 |
| `provenance_verify` | 可选 `decision_id` | 哈希链校验报告；带 decision_id 时附最小重放证据集 | 可用 |
| `event_deduce` | `event_id` / `as_of` / 种子 | — | `NOT_IMPLEMENTED`（P6） |
| `conflict_check` | `decision_id` | — | `NOT_IMPLEMENTED`（P6） |

两个占位不是「假装能用」：它们返回 `status="error"` + `NOT_IMPLEMENTED` + `retryable=True`，调用方可以据此重试或降级，而不是拿到一份编造的推演结果。这正好满足验收里那句「失败时不返回伪造数据」。

## 4. 补掉的一个真权限漏洞

`evidence_locate` 最初的实现只校验 `asset_id` 存在、`locator` 前缀匹配。问题是：**只要知道 ref，就能把受限资产的正文取走**——两个部门规程标注了 `visibility=ORG-DRAIN/ORG-EMERG` 与 `ORG-GAS/ORG-EMERG`，语义层挡住了关系，但证据定位接口把原文直接吐出来。

语义层的权限过滤在这条路径上等于白做。

修法：`evidence_locate` 增加 `caller_labels`，按证据资产的 `visibility` 判定；不匹配返回 `PERMISSION_DENIED`，并在 `data.required_labels` 里说明需要哪些标签（不泄漏内容，只说明门槛）。

实测：
- `caller_labels=[]` → `PERMISSION_DENIED`，`required_labels=["ORG-DRAIN","ORG-EMERG"]`
- `caller_labels=["ORG-DRAIN"]` → 返回正文片段

顺带修掉一个隐蔽问题：CSV 带 BOM，`evidence_locate` 返回的行字典第一个键会是 `\ufefforder_id`，下游按 `order_id` 取值全部落空。现在表头统一剥 BOM。

## 5. trace_id 与调用日志

任务单要求「工具调用日志和 `trace_id` 传播」。

- 请求侧：`tools/call` 的 `params.trace_id`（或 `_meta.trace_id`）→ `ToolResult.context.trace_id`；
- `ToolContext` 只**新增**一个可选字段，不动现有字段语义，也不动 `ToolResult` 结构与三种 status；
- 日志每条含 `tool / trace_id / status / error_code / latency_ms / knowledge_version / snapshot_hash`，可按 `--log` 落 JSONL。

日志里带 `snapshot_hash` 不是装饰：排查「同一个问题两次答案不一样」时，先看两次查询锚定的语义世界是否同一版，否则会去查错方向。

延迟是真实测量的（`time.perf_counter`），语义层内部仍固定 0 —— 那里是纯计算，稳定的输出便于断言。

## 6. 实测样例

```
initialize → {"protocolVersion":"2025-06-18","serverInfo":{"name":"urban-twin-mcp","version":"0.5.0"}}

semantic_query(seed=TW-incident-WO-20261029-007, hops=2, block=region)
  → status=ok, triple_count=13, snapshot_hash=a25666ac…

evidence_locate(AS-A-03-work_order-8a8ea14d#A-03/work_order/v1#row:2)
  → status=ok, row.order_id=WO-20261029-007, row.severity=high,
    excerpt="兴华路 K0+320 积水深约 30cm，雨水口反涌"

evidence_locate(AS-A-03-emergency_plan-cbaa17ca#…/v1.0#line:33-35)
  → excerpt="4.1 先组织抽排降低水位。 / 4.2 … / 4.3 …"

evidence_locate(<map>#A-03/schematic_map/v1#region:250,245,220,30)
  → region.label="积水区", note="R-101 K0+200-600，对应工单 WO-20261029-007"

event_deduce → status=error, code=NOT_IMPLEMENTED, retryable=true
```

## 7. 已知边界（P6/P7 前必须处理）

1. **没有 HTTP/SSE 传输**：Nexent 侧要接 `/nb/v1` 时需要一个 SSE 循环包同一个 `handle()`。现在没做，是因为没有 Nexent 部署可联调，写出来也无法验证——按项目既有规矩（`nexent_smoke` 缺凭据直接退出码 2），没验证的代码不进主线。
2. **无鉴权**：`caller_labels` 由调用方自报。本地 stdio 形态下这是合理的（信任边界=本地进程），但一旦上 HTTP，必须换成服务端签发的身份，否则权限过滤可被自报绕过。这条要写进 P7 的部署说明。
3. **会话状态在进程内**：`UrbanToolkit` 启动时装配一次，数据改动后需要重启或另起实例。P6 推演链若要在同进程内看到新版本知识，需要加一个 `reload` 工具或热加载。
4. **`knowledge_diff` 还没有对应工具**：它需要多版本 registry（`get_knowledge_diff` 在 Python 层可用），而 toolkit 目前只挂单版语义层。P7 演示「数据变更→结论重评」时再暴露成第 8 个工具。
5. **工具入参只做 pydantic 校验**，没有 JSON Schema 级别的深度校验（如 `as_of` 的格式在 schema 里只是 string）。非法值会在 handler 里被抓成 `VALIDATION_FAILED`，但客户端拿到的错误信息不如 schema 校验友好。
