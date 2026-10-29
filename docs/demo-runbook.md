# 演示运行手册（Nexent 真实调用）

适用环境：本机 Docker 部署 Nexent **v2.7.0**（`nexent-northbound` 5013、`nexent-web` 3000）。
全程约 3 分钟，所有命令均可直接粘贴到 PowerShell。

## 0. 前置

- Docker Desktop 已启动，`nexent-northbound` / `nexent-postgresql` / `nexent-elasticsearch` 三个容器在跑。
- 仓库位于 `D:/华为ICT/agent_poc`，已执行过 `uv sync --locked`。

## 1. 环境自检（约 30 秒）

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
$env:NEXENT_BASE_URL   = "http://localhost:5013"
$env:NEXENT_AGENT_NAME = "workbench_main"
$env:NEXENT_API_KEY = (docker exec nexent-postgresql psql -U root -d nexent -t -A `
  -c "select access_key from nexent.user_token_info_t where token_id=2 and delete_flag='N';").Trim()

Invoke-RestMethod "$env:NEXENT_BASE_URL/nb/v1/health"
(Invoke-WebRequest "$env:NEXENT_BASE_URL/nb/v1/agents" `
  -Headers @{ Authorization = "Bearer $env:NEXENT_API_KEY" } -UseBasicParsing).StatusCode
```

期望：健康检查返回 `{"status":"healthy","service":"northbound-api"}`；第二条返回 `200`。
两项都过才继续，否则查第 5 节。

## 2. 清记忆（每次演示前必做，约 10 秒）

```powershell
$esPass = (docker exec nexent-elasticsearch printenv ELASTIC_PASSWORD).Trim()

docker exec nexent-postgresql psql -U root -d nexent -c "update nexent.memory_records_t set delete_flag='Y', update_time=now() where coalesce(delete_flag,'N')<>'Y';"

1..50 | ForEach-Object { docker exec nexent-elasticsearch curl -s -o /dev/null -u "elastic:$esPass" -X DELETE "localhost:9200/mem_bge-m3_1024/_doc/$_" }

docker exec nexent-elasticsearch curl -s -u "elastic:$esPass" -X POST "localhost:9200/mem_bge-m3_1024/_refresh" | Out-Null
docker exec nexent-elasticsearch curl -s -u "elastic:$esPass" "localhost:9200/mem_bge-m3_1024/_count"
```

期望最后一行 `"count":0`。

注意两个坑（实测踩过）：

- 循环必须写成**单行**；用反引号断行会让 PowerShell 把参数拆开，curl 只收到 `-u` 而丢了凭据。
- **不要写 `-w ""`**：PowerShell 会丢弃空字符串参数，curl 把后面的 `-u` 当成 `-w` 的格式串，表现为 HTTP 401。

**跳过这步的后果**：`evidence.source` 会变成 `agent_memory`，答案复用上一场结论，
看起来像在“背稿”。只软删 Postgres 无效，检索走 Elasticsearch。

## 3. 跑一次决策（约 60–90 秒）

```powershell
uv run --locked python -m decision_poc.nexent_smoke
echo "exit=$LASTEXITCODE"
```

期望：`"status": "accepted"`、有 `conversation_id`、`decision` 不为 `null`、`exit=0`。

## 4. 结果判读（三看）

| 看哪里 | 合格标准 | 不合格说明 |
| --- | --- | --- |
| `decision` | 非 null，选项 / 证据 / 风险齐全 | null → 看 `decision_error` |
| `evidence[].source` | ISO 标准、厂商手册、行业规范 | `agent_memory` → 第 2 步没清干净 |
| `events.final_answer` | 等于 1 | 为 0 → 流没走完 |

补充：`answer` 开头可能出现 `xxx</think>{...}` 之类的截断残留，是平台侧拼接问题，
解析层已容错，不影响 `decision`。

## 5. 失败对照表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| HTTP 401 | Key 无效或已被撤销 | 按附录 A 重新生成 |
| HTTP 400，提示 `allow_chat_metadata` | 请求体带了 `metadata` | 传输层已改用 `meta_data`，勿手改回 |
| `X-Stream-Status: conflict` / 429 | 上一轮还在跑或触发限流 | 等 10 秒重试，不要自动重试 |
| `NexentStreamError: 未产生 final_answer` | 模型/工具链中断 | 看 `events`，整轮重跑一次 |
| `decision_error: JSONDecodeError` | Agent 回了自然语言 | 确认用的是 `workbench_main`，不是 `equipment_decision_agent` |
| `decision_error: ValidationError` | 字段缺失或多余 | 看 error 明细，必要时收紧 query 结构 |

## 6. 演示收尾（可选）

```powershell
# 撤销临时 Key
docker exec nexent-postgresql psql -U root -d nexent -c "delete from nexent.user_token_info_t where token_id=2;"
```

- 换 Agent 只改 `$env:NEXENT_AGENT_NAME`：`equipment_decision_agent` 输出四段式中文报告，
  **不是** JSON，不会产生 `decision`。
- 想把记忆彻底留空，演示结束再跑一次第 2 节。

## 附录 A：重新生成 Key（用平台函数，不手写 SQL）

```powershell
$py = @'
from database.token_db import generate_access_key, create_token
print(create_token(access_key=generate_access_key(), user_id="84b8db49-7c40-4287-8648-629ca1409693")["token_id"])
'@
$tokenId = ($py | docker exec -i nexent-northbound sh -c "cd /opt/backend && .venv/bin/python -") -join ""
"token_id=$tokenId"
```

把第 1 节里的 `token_id=2` 换成新 id 即可。

## 答辩口径（三句话）

1. 契约：按官方 `/nb/v1` 实现，并用本机 v2.7.0 的 `openapi.json` 逐字段核对。
2. 可靠性：SSE 聚合、`Idempotency-Key`、401/429/conflict 分类处理，无 `final_answer` 一律判失败。
3. 合规：真实调用已验证（`conversation_id=15`），证据可追溯，记忆污染有明确清理路径。
