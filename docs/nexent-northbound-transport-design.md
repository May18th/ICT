# Nexent 北向传输层设计（真实客户端）

状态：**已实现**（2026-10-29，`src/decision_poc/nexent_transport.py`）。核对日期：2026-10-29。

## 1. 结论

"缺少 Nexent API/SDK 协议信息"这个阻塞不成立。Nexent 的对外契约是公开的北向 API（`/nb/v1`），
接口路径、鉴权头、请求体字段、SSE 事件格式、幂等与限流参数都能从官方文档和 `develop` 分支源码直接确认，
不需要猜测。真正缺的只是把这份契约落成仓库里的一份文档和一个传输层实现。

离线边界靠"传输函数注入"保证，不靠"不知道协议"保证。把契约写清楚反而让离线边界更硬：
单测用 stub，真机联调用唯一一处 HTTP 客户端，中间没有第二套编排逻辑。

## 2. 已核对的契约

### 2.1 端点（前缀 `/nb/v1`，默认端口 5013）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/nb/v1/health` | 健康检查 |
| GET | `/nb/v1/agents` | 已发布 Agent 列表 |
| GET | `/nb/v1/agents/{agent_name}` | 按名称查已发布 Agent（含 `agent_id`、`latest_version_no`） |
| POST | `/nb/v1/chat/run` | 对话主入口，SSE 流式 |
| GET | `/nb/v1/chat/stop/{conversation_id}` | 停止对话 |
| POST | `/nb/v1/chat/attachments/upload` | 附件上传（multipart，字段名 `files`，返回 `s3_url`） |
| GET | `/nb/v1/conversations`、`/nb/v1/conversations/{id}` | 会话列表 / 历史 |
| POST | `/nb/v1/generate_title`、PUT `/nb/v1/conversations/{id}/title` | 标题 |
| GET | `/nb/v1/models` | 租户可用模型 |

A2A 通道：`/nb/a2a/{endpoint_id}/.well-known/agent-card.json`、
`/nb/a2a/{endpoint_id}/v1`（JSON-RPC 2.0：`SendMessage` / `SendStreamingMessage` / `GetTask`）。
PoC 阶段不用 A2A，只用 `/chat/run`。

### 2.2 鉴权

- `Authorization: Bearer <access_key>`，API Key 在 Nexent 前端「智能体开发 → 发布」处生成。
- 服务端用 access_key 查 `user_token_info_t` 得 `user_id`，再查 `user_tenant_t` 得 `tenant_id`，
  租户决定资源隔离和审计。**API Key 隐含租户，不能跨租户复用。**
- 可选请求头 `X-Request-Id`（缺省服务端生成 uuid），响应会回带 `X-Request-Id`。
- 源码里还有一套 AK/SK 头（`X-Access-Key` / `X-Timestamp` / `X-Signature`，HMAC 签名），
  但 `get_aksk_config()` 直接抛 `AK/SK authentication is not configured`，**当前版本不可用，不要用**。
- 失败映射：`401`（API Key 无效/失效）、`429`（限流或幂等冲突）、`400`（缺 user 或 tenant）。

### 2.3 `/nb/v1/chat/run` 请求体

全部字段是 `Body(embed=True)`，即扁平 JSON，没有外层包装：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `agent_name` | string | 是 | 已发布 Agent 的名称 |
| `query` | string | 是 | 本轮用户输入 |
| `conversation_id` | int | 否 | 不给则新建会话 |
| `attachments` | list | 否 | `s3_url` 字符串数组，或带完整元数据的附件对象 |
| `model_id` | int | 否 | 覆盖 Agent 默认模型 |
| `metadata` | object | 否 | 运行时元数据，Agent 可见；有大小校验，超限报 `CHAT_METADATA_TOO_LARGE` |
| `meta_data` | object | 否 | 仅审计/用量日志，Agent 不可见 |
| `tool_params` | object | 否 | 单次请求覆盖工具参数，结构 `agents.{agent}.tools.{tool}.{param}` |

请求头另支持 `Idempotency-Key`。旧版 `agent` 字段会被 `reject_legacy_agent_fields` 直接拒绝。

### 2.4 响应（SSE）

`Content-Type: text/event-stream`，响应头带 `X-Request-Id`、`conversation_id`、`X-Accel-Buffering: no`。
每个事件是一行 `data: {json}`，结构：

```
data: {"type":"model_output_thinking","content":"正在分析数据","unit_index":1}
data: {"type":"final_answer","content":"分析完成","unit_index":2}
```

| `type` | 语义 | 客户端处理 |
| --- | --- | --- |
| `conversation_created` | 新会话第一个事件（代码里显式先发） | 取 `content.conversation_id` 存为上下文 |
| `model_output_thinking` | 思考过程，**增量** | 必须累积拼接，不能按帧取末段 |
| `model_output_deep_thinking` | 深思考模型，延后成块吐出，期间可能无事件 | 是特性，不要判卡死 |
| `model_output` | 正文输出 | 累积 |
| `final_answer` | 最终答案，**一次性完整** | 整段替换/落库，不做增量拼接 |
| `human_interaction` | 结构化澄清：`content` 为 `{schema_version:1, questions:[...]}`，最多 5 问，题型 `text`/`single_choice`/`multiple_choice` | 流结束后把答案作为同会话下一条普通 query 发送，无专用恢复接口 |
| `search_content` | 知识库检索分片（11 字段，含 `filename`/`url`/`score`/`cite_index`/`tool_sign`） | 用于来源卡片 |
| `card`、`tool`、`execution_logs` | 工具调用与执行日志 | 观测用 |
| `error` | 错误事件 | 不得当作成功 |

会话正在运行时再次发送，响应头出现 `X-Stream-Status: conflict`，应保留草稿让用户稍后重发，**不要自动重试**。

### 2.5 幂等、限流、超时

- 幂等只在**并发重复启动**上生效：带 `Idempotency-Key` 时用该键；不带时服务端用
  `tenant:conversation:agent:query:metadata_hash` 组合键。重复请求仍在跑 → `429 Duplicate request is still running`。
- TTL `NORTHBOUND_IDEMPOTENCY_TTL_SECONDS` 默认 `600` 秒。
- 限流 `NORTHBOUND_RATE_LIMIT_PER_MINUTE` 默认 `120` 次/分钟/租户（`NORTHBOUND_RATE_LIMIT_ENABLED` 默认 true）。
- 服务端**不提供**统一的总请求超时；SSE 必须由客户端自己管：连接超时 + 分段空闲超时，
  不能用一个大 `read timeout` 卡整轮对话。
- 停止接口返回成功只代表"停止请求已受理"，worker 实际退出是异步的。
- 相关常量：`RUNTIME_MCP_TOOL_TIMEOUT_SECONDS=60`、`RUNTIME_PARALLEL_EXECUTOR_TIMEOUT_SECONDS=120`。

## 3. 映射到现有 PoC

| PoC 侧 | Nexent 侧 | 说明 |
| --- | --- | --- |
| `DecisionRequest.request_id` | Header `Idempotency-Key` | 一次决策一次键，重试用同一个键 |
| `DecisionRequest.goal` + `context` + `constraints` | `query`（结构化模板拼装） | 不塞进 `metadata`，避免超出元数据大小限制 |
| 运行追踪 | `metadata={"decision_request_id": ...}` | 仅放少量定位信息 |
| Agent 名称 | `agent_name`（配置项，不硬编码） | 需先在 Nexent 侧创建并发布 Agent |
| 返回值 | `{"status":"accepted","conversation_id":...,"answer":...}` | 满足适配层 `dict` 契约，`run_id` 由 `conversation_id` 担任 |

决策结构化输出建议：要求 Nexent Agent 的 `final_answer` 直接输出严格 JSON，本地再用现有 Pydantic
模型校验解析；解析失败走本地结构化失败路径，不进 LangGraph 内部重排。

## 4. 传输层实现（已落地）

实现文件 `src/decision_poc/nexent_transport.py`，未改现有 `NexentAdapter`：

```python
class NexentHttpTransport:
    def __init__(self, *, settings: NexentSettings, connect_timeout=5.0,
                 idle_timeout=120.0, max_attempts=2, backoff_seconds=0.5,
                 client: httpx.Client | None = None): ...

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """签名与 NexentAdapter(invoke=...) 一致，返回聚合后的结构化结果。"""
```

已实现的约束：

1. 依赖 `httpx==0.28.1`（已在 `pyproject.toml`/`uv.lock` 显式声明）；不引入 Nexent SDK。
2. 按行切 `data:`，只认 `conversation_created`、`final_answer`、`error`；`final_answer` 到达即停止读取。
3. 无 `final_answer` → `NexentStreamError`；`error` 事件 → `NexentStreamError`；均不返回 accepted。
4. 重试仅限连接错误、`429`、`502/503/504`；`401` 与 `X-Stream-Status: conflict` 不重试；
   流已开始后一律不重试。`Idempotency-Key` 与 `X-Request-Id` 在重试间保持同一个值。
5. 凭据只经 `NexentSettings.from_env()` 读取：`NEXENT_BASE_URL`（默认 `http://localhost:5013`）、
   `NEXENT_API_KEY`、`NEXENT_AGENT_NAME`；缺失即 `NexentConfigError`。
6. 默认不联网：不构造 transport 就不会有任何 HTTP 调用，`baseline`/`showcase` 路径不变。
7. Agent 输出契约定为**严格 JSON**：`parse_decision_output()` 直接走 `DecisionOutput` 校验；
   不合格（含非 JSON）不抛异常打断链路，而是返回 `decision=None` + `decision_error`，
   交给上层按结构化失败处理。

错误类型：`NexentConfigError` / `NexentAuthError` / `NexentConflictError` /
`NexentTransportError` / `NexentStreamError`，共同基类 `NexentError`。

## 5. 测试（已落地，全部离线）

`tests/test_nexent_transport.py` 共 12 项，全部用 `httpx.MockTransport` 回放，不触网：

1. SSE 聚合：`conversation_created` + `final_answer` → accepted、`conversation_id`、`decision` 解析成功。
2. 契约头/体：`Authorization: Bearer`、`Idempotency-Key`、`agent_name`、query 模板。
3. `401` → `NexentAuthError`，只请求一次。
4. `429` + `Retry-After` → 重试一次成功，两次 `Idempotency-Key` 相同。
5. `503` 连续 → 重试有上限，抛 `NexentTransportError`。
6. 只有 thinking 事件、无 `final_answer` → `NexentStreamError`。
7. `error` 事件 → `NexentStreamError`。
8. `X-Stream-Status: conflict` → `NexentConflictError`，只请求一次。
9. 非 JSON `final_answer` → 返回 accepted 但 `decision=None` + `decision_error`。
10. `NexentAdapter(invoke=transport)` 端到端（含 ```json 围栏剥离）。
11. `NexentSettings.from_env()` 缺 Key / 缺 Agent 名 → `NexentConfigError`。
12. `build_decision_query` / `parse_decision_output` 辅助函数。

另有 `tests/test_nexent_smoke.py` 4 项：缺凭据退出码 2、注入传输成功退出码 0、
传输失败退出码 1、`missing_env` 的空值识别。

## 6. 待确认事项

1. ~~是否加 `httpx` 依赖~~ → 已加 `httpx==0.28.1`。
2. ~~Agent 输出契约~~ → 已定为严格 JSON，失败降级为 `decision_error`。
3. **真机联调**：需要提供 Nexent 部署地址 + 发布后生成的 API Key + 已发布 Agent 名称，
   才能跑一次真实 smoke。在此之前只有 Mock 单测覆盖。
   脚本已就位：`uv run --locked python -m decision_poc.nexent_smoke`；
   缺凭据时列出缺失环境变量并返回 2，不发任何请求；调用失败返回 1；成功返回 0 并打印聚合结果。

## 7. 来源

- 官方后端 API 参考：`http://modelengine-group.github.io/nexent/zh/backend/api-reference.html`（端口 5013、`/nb/v1` 端点、Bearer 鉴权）
- 源码 `develop` 分支，核对于 2026-10-29：
  - `backend/gateway/api/northbound.py`（路由、`/chat/run` 全部 Body 字段、SSE 头、`Idempotency-Key`）
  - `backend/gateway/authentication/northbound_auth.py`（Bearer 校验、AK/SK 未启用、错误映射）
  - `backend/services/northbound_service.py`（`conversation_created` 首发事件、幂等键组合规则、限流）
  - `backend/contracts/constants/environment.py`（幂等 TTL、限流阈值、工具超时）
- 流式事件语义与澄清事件：Nexent 官方发布指南（`/chat/run` SSE 示例、`human_interaction`、`X-Stream-Status: conflict`）
