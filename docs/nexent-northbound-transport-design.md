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

`tests/test_nexent_transport.py` 共 16 项，全部用 `httpx.MockTransport` 回放，不触网：

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
13. 噪声答案里提取 JSON（含 `<|tool_call_start|>` 标记与前置散文）。
14. 证据内容含嵌套花括号时正确截断。
15. `</think>` 残留清洗。
16. 完全无 JSON 的答案报错。

另有 `tests/test_nexent_smoke.py` 4 项：缺凭据退出码 2、注入传输成功退出码 0、
传输失败退出码 1、`missing_env` 的空值识别。
以及 `tests/test_nexent_sdk_transport.py` 5 项（fake SDK）：聚合出决策、无 `final_answer`、
`error` 事件、SDK 未安装、模型凭据缺失。

## 6. 备选路径：SDK 内嵌（无可用平台环境时）

现实约束：CI / 沙箱容器（如华为云 AI 开发者空间）是 K8s 沙箱里的 ARM64 容器，
拿不到 Docker daemon，起不了 Nexent 全栈（Postgres + Redis + ES + MinIO + 前后端）。
此时走 SDK 内嵌，`src/decision_poc/nexent_sdk_transport.py`：

```python
transport = NexentSdkTransport(model_id="...", api_key="...", api_base="https://.../v1")
NexentAdapter(invoke=transport).run(request)
```

- 可选依赖，不写进 `pyproject.toml`：`uv pip install "nexent==1.8.1"`（PyPI 实测存在，要求 Python ≥3.10）。
  未安装时构造即抛 `NexentConfigError` 并给出安装命令，离线路径零影响。
- 走官方 SDK 的 `agent_run` 事件流，按 `final_answer` / `error` 聚合，语义与 HTTP 路径一致：
  无 `final_answer` 一律 `NexentStreamError`，不返回 accepted。
- **代价**：这条路径验证的是 Agent 运行时，不是 `/nb/v1` 北向契约。要给平台契约背书，仍须
  `NexentHttpTransport` + 真实部署。
- **未在真机验证**：实现依据官方 SDK 文档（`agent_run` / `AgentRunInfo` / `ModelConfig` 字段），
  首次真机运行需核对事件字段名；我方聚合逻辑由 fake SDK 单测覆盖（5 项）。
- 不能在有运行中事件循环的进程里同步调用（`asyncio.run` 限制），会明确报错而不是死等。

## 7. 待确认事项

1. ~~是否加 `httpx` 依赖~~ → 已加 `httpx==0.28.1`。
2. ~~Agent 输出契约~~ → 已定为严格 JSON，失败降级为 `decision_error`。
3. **真机联调**：需要提供 Nexent 部署地址 + 发布后生成的 API Key + 已发布 Agent 名称，
   才能跑一次真实 smoke。在此之前只有 Mock 单测覆盖。
   脚本已就位：`uv run --locked python -m decision_poc.nexent_smoke`；
   缺凭据时列出缺失环境变量并返回 2，不发任何请求；调用失败返回 1；成功返回 0 并打印聚合结果。

## 8. 本机部署实测（2026-10-29）

对一台本地 Docker 部署的 Nexent **v2.7.0**（`nexent-northbound` 容器映射 `5013->5013`，
前端 `3000->3000`）做了只读核对，结论：

1. `GET /nb/v1/health` → `200 {"status":"healthy","service":"northbound-api"}`，健康检查无需鉴权。
2. `GET /openapi.json` → `Nexent Northbound API v1.0.0`，路径与本设计第 2.1 节一致。
3. `/nb/v1/chat/run` 实际 schema：`required: [agent_name, query]`；可选
   `conversation_id` / `attachments` / `model_id` / `metadata` / `meta_data` / `tool_params`；
   请求头参数 `Idempotency-Key` 存在且非必填。**与传输层实现逐字段一致。**
4. `GET /nb/v1/agents` 无 Bearer → `401`，与 `NexentAuthError` 映射一致。
5. 数据库 `nexent.user_token_info_t` 为空 → 未生成过 API Key；`nexent.ag_tenant_agent_t`
   已有 `equipment_decision_agent`（设备故障决策智能体，`current_version_no=2`）与
   `workbench_main`。

待办只有一步：在前端「智能体开发 → 发布 → 生成 API 密钥」拿到 Access Key，或者用平台自身函数
在容器内生成（`database.token_db.generate_access_key()` + `create_token()`，格式 `nexent-` + 24 hex，
共 31 字符），当前本机测试 Key 为 `nexent.user_token_info_t.token_id=2`。

```powershell
$env:NEXENT_BASE_URL   = "http://localhost:5013"
$env:NEXENT_API_KEY    = "<Access Key>"
$env:NEXENT_AGENT_NAME = "workbench_main"
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.nexent_smoke
```

### 8.1 真机调用结果（三次，实测）

| # | Agent | 结果 |
| --- | --- | --- |
| 1 | `equipment_decision_agent` + `metadata` | **400**：该 Agent `allow_chat_metadata=false`，带 `metadata` 直接被拒 |
| 2 | `equipment_decision_agent` 去掉 `metadata` | **200**，但 `final_answer` 停在“第一步：检索”，内容是一段 Python 代码调用 `knowledge_base_search`（`model_output_code` 53 段），`decision_error` |
| 3 | `workbench_main` 同一 query | **200**，`decision` 校验通过：3 个选项、3 条证据、`risk_level=high` |

由此修正两条实现：

- `metadata` 改为只走 `meta_data`（审计用，平台始终接受），决策 ID 另由 `Idempotency-Key` 承载。
- `final_answer` 会夹带 `</think>` 残留与工具调用标记，解析器改为「标签清洗 → 代码围栏剥离 →
  逐候选 JSON 块校验」，工具调用那段 JSON 会被跳过，直到找到能通过 `DecisionOutput` 校验的块。

### 8.2 结论：Agent 提示词是最后一道门

`equipment_decision_agent` 的 `duty_prompt` 强制「检索 → 推理 → 结论」三步且要求先调工具，
`constraint_prompt` 又限定“500 字以内、结论/依据/推理链/建议四段”，**与 JSON 输出契约直接冲突**，
所以它永远不会返回我们的结构。`workbench_main` 没有这些约束，一次就吐出了合规 JSON。

要让它也能走同一契约，最小改动是在该 Agent 的 `constraint_prompt` **追加一条覆盖规则**：

```text
0. 若调用方在提问中给出了明确的 JSON 结构要求，则最终输出必须且只能是该 JSON：
   不得输出“结论/依据/推理链/建议”分段文本，不得输出代码块或工具调用标记；
   证据与风险一律填进 JSON 的 evidence / risks 字段。此条优先于第 6 条的字数限制。
```

不改提示词也能演示：对外统一用 `workbench_main` 走决策契约，`equipment_decision_agent`
保留原来的中文决策报告能力，两者各自成立。

### 8.3 换 Key 后的回归（2026-10-29 10:45）

撤掉 `token_id=1` 后，用平台自身的 `generate_access_key()` + `create_token()` 重建 Key
（`token_id=2`，31 字符），回归结果：

| 检查 | 结果 |
| --- | --- |
| `GET /nb/v1/agents` 带新 Key | `200` |
| `python -m decision_poc.nexent_smoke` | `accepted`，`conversation_id=13`，退出码 0 |
| `DecisionOutput` 校验 | 通过：3 选项、`selected_option_id=opt1`、`confidence=0.88`、`risk_level=medium` |
| `answer` | 1431 字符，仍带 `ipment issue.</think>` 截断残留（解析器已容错） |

**新发现：记忆污染**。这次三条 `evidence` 的来源全部是「Agent 记忆-历史处置经验 / 设备历史 /
约束条件」，说明 `workbench_main` 开了长期记忆，把上一轮（`conversation_id=11`）的结论固化后
直接复用。后果：同一 `request_id` 重放得到的不是独立推理，演示时答案会“背稿”。

#### 记忆污染的清理路径（实测）

只软删 Postgres **无效**：记忆召回走 Elasticsearch，索引 `mem_bge-m3_1024`，
文档 `_id` 即 `memory_id`，`_source.metadata` 里带 `conversation_id` / `agent_id` / `status`。
软删 `nexent.memory_records_t.delete_flag='Y'` 后重跑，`evidence` 依旧来自 `agent_memory`。

真正生效的做法是先删 ES 文档（本例按 `_id` 删除），再对齐 DB 标记：

```bash
# ES 必须带凭据，凭据在容器环境变量 ELASTIC_PASSWORD
docker exec nexent-elasticsearch sh -lc 'curl -s -u "elastic:${ELASTIC_PASSWORD}" \
  -X DELETE "localhost:9200/mem_bge-m3_1024/_doc/1"
  '
# 删完务必 refresh，再 _count 核对
```

注意：ES 的 `_delete_by_query` 在 Windows + PowerShell 下传 JSON body 极易被引号吃掉
（表现为 `Validation Failed: 1: query is missing`），按 `_id` 逐个 DELETE 最稳。
清理后 `count=0`，再跑一次 smoke：

| 检查 | 清理前（conv 13） | 清理后（conv 15） |
| --- | --- | --- |
| `evidence.source` | `agent_memory` ×3 | ISO 10816-1 / 轴承温度行业规范 / 设备劣化曲线 |
| `decision` | opt1，conf 0.92 | opt-b，conf 0.85，4 选项 / 4 风险 |
| 结论 | 复用上轮结论 | 独立推理 |

另外：**每次成功调用都会重新写入短期记忆**（conv 15 跑完又产生新记录），
所以“清一次就永久干净”不成立；演示前清一次即可，或换新 `agent_name`。


## 9. 来源

- 官方后端 API 参考：`http://modelengine-group.github.io/nexent/zh/backend/api-reference.html`（端口 5013、`/nb/v1` 端点、Bearer 鉴权）
- 源码 `develop` 分支，核对于 2026-10-29：
  - `backend/gateway/api/northbound.py`（路由、`/chat/run` 全部 Body 字段、SSE 头、`Idempotency-Key`）
  - `backend/gateway/authentication/northbound_auth.py`（Bearer 校验、AK/SK 未启用、错误映射）
  - `backend/services/northbound_service.py`（`conversation_created` 首发事件、幂等键组合规则、限流）
  - `backend/contracts/constants/environment.py`（幂等 TTL、限流阈值、工具超时）
- 流式事件语义与澄清事件：Nexent 官方发布指南（`/chat/run` SSE 示例、`human_interaction`、`X-Stream-Status: conflict`）
