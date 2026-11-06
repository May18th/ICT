# Nexent 侧装配与真机验证（可复现）

把本仓库的城市孪生 MCP 服务接入本机 Nexent v2.7.0，并验证"平台 Agent 调 MCP 工具 → 六跳结论与离线一致"。

已验证结果（2026-11-06 复测，Agent **v19**，模型 **DeepSeek-Chat**）：**8/8 项一致，conversation_id=120**，平台实际调用 `event_deduce` 1 次，工具日志与本轮一一对应（`samples/nexent/live/mcp_tool_log.jsonl` 仅 1 行，快照 `d102aa80…`）。
> 模型切换原因：openPangu-2.0-Pro 走华为 ModelArts，2026-11-06 因账户欠费停服，改为平台已注册的 DeepSeek-Chat（`https://api.deepseek.com/v1`，独立计费）。切换后深度思考段消失，同一提示词下一轮即通过，作答预算也从 16384 回调到 8192。
完整过程与排障见 [真机集成报告](../../docs/reports/nexent-live-integration-report.md)。

## 0 前置

- Docker Desktop 已启动，Nexent 全栈在跑（`docker ps` 里应有 `nexent-northbound`、`nexent-config`、`nexent-web` 等）；
- 北向健康：`curl http://localhost:5013/nb/v1/health` 返回 `{"status":"healthy",...}`；
- 本仓库可运行：`uv run --locked pytest -q` 全绿。

## 1 构建并启动 MCP 容器（SSE）

Nexent 只认 HTTP/SSE 的 MCP 服务，所以用容器把 `mcp_sse` 跑在 Nexent 同一网络里：

```powershell
Set-Location D:\华为ICT\agent_poc
docker build -t urban-twin-mcp:dev -f deploy/mcp_sse/Dockerfile .

docker rm -f urban-twin-mcp 2>$null
docker run -d --name urban-twin-mcp --network nexent_network --restart unless-stopped urban-twin-mcp:dev

# 健康检查：应返回 14 个工具
docker exec urban-twin-mcp python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode()[:160])"
```

> 为什么不用 `host.docker.internal` 直连宿主机：Windows 上 Docker→宿主机入站被防火墙拦（加规则需要管理员权限），
> 把服务放进 `nexent_network` 用容器名互访更稳，也更接近真实部署。

## 2 平台侧装配（幂等）

平台管理 API 需要 JWT，密钥只存在于 `nexent-config` 容器里，所以在容器内用后端自己的 venv 执行装配脚本。
`setup_nexent.py` 与 `upload_skills.py` 共用 `platform_client.py`（JWT 签发 + HTTP 入口），三个文件一起拷：

```powershell
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/setup_nexent.py    nexent-config:/tmp/setup_nexent.py
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/setup_nexent.py all"
```

脚本依次做四件事（重复执行不会重复建）：

| 步骤 | 作用 | 幂等策略 |
| --- | --- | --- |
| `register-mcp` | 注册 MCP 服务 `urban-twin-mcp`（URL `http://urban-twin-mcp:8000/sse`） | 同名已存在则复用 |
| `sync-tools` | `scan_tool` 把 14 个 MCP 工具同步进平台工具库 | 已存在则覆盖状态 |
| `create-agent` | 创建/复用 `urban_deduction_agent`（**upsert**：带 `agent_id` 回写提示词），挂 2 个工具，发布版本 | 同名 Agent 复用；重复挂载无害 |
| `mint-key` | 生成北向 API Key | 每次生成新 Key（旧 Key 可继续用） |

输出里记下：

- MCP：`[mcp] id=2 status=True enabled=True`
- Agent：`[agent] id=3 name=urban_deduction_agent version=...`
- Key：`[key] NEXENT_API_KEY=nexent-...`

> 也可分步执行：`setup_nexent.py register-mcp` / `sync-tools` / `create-agent` / `mint-key` / `status`。

## 2.1 上传 SKILL.md 到平台技能仓库（幂等）

两个技能（`samples/nexent/skill/*/SKILL.md`）上传到平台"Skill 仓库"，平台侧自己解析 frontmatter、
自己落盘正文；脚本只负责传输与复核，不做本地改写。

```powershell
docker cp deploy/nexent/upload_skills.py nexent-config:/tmp/upload_skills.py
docker exec nexent-config sh -c "rm -rf /tmp/nexent_skills"
docker cp samples/nexent/skill nexent-config:/tmp/nexent_skills
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/upload_skills.py all --json /tmp/skills_evidence.json"
docker cp nexent-config:/tmp/skills_evidence.json samples/nexent/live/skills.json
```

期望输出（首次 `created`，再跑一次是 `unchanged`，证明幂等）：

```text
[skills] 平台已有 ['ontology-builder']
[skill] urban-six-hop-deduction: created (http=201 skill_id=3)
[skill] scarcity-allocation-evolution: created (http=201 skill_id=2)
[skills] 平台复核：['ontology-builder','scarcity-allocation-evolution','urban-six-hop-deduction']；缺失 []；正文逐位一致=True
```

证据落 `samples/nexent/live/skills.json`：每个技能记 `file_sha256`（本地 SKILL.md）、`content_sha256`、
`stored_content_sha256`（平台存储正文）与 `content_matches`。这条证据由
`python -m decision_poc.evidence_check` 反向核对：**改了 SKILL.md 却没重新上传，自检直接失败**
（`platform_skills.local_file_hashes_match`）。

边界与现状：技能**已绑定到 `urban_deduction_agent` 的草稿和已发布版本**（`ag_skill_instance_t`
的 `version_no=0` 与当前发布版本都 `present=True enabled=True`，用 `bind_skills.py verify
--require-published` 复核，退出码 0）。但要说清它**在运行时到底起什么作用**：实测模型不会主动
`read_skill_md`，它读的是"这个 Agent 挂了哪些工具"，所以技能绑定改变不了工具调用行为——
详见 [真机集成报告](../../docs/reports/nexent-live-integration-report.md) 的 2026-11-03 一节。

## 3 真机验证（结论级）

```powershell
$env:PYTHONPATH = "src"
$env:NEXENT_BASE_URL = "http://localhost:5013"
$env:NEXENT_API_KEY = "<上一步生成的 Key>"
$env:NEXENT_AGENT_NAME = "urban_deduction_agent"
uv run --locked python -m decision_poc.nexent_urban --attempts 2 --json samples/eval/nexent_urban_smoke.json
```

期望输出：

```text
Nexent 城市内涝主链路：8/8 项一致；conversation_id=<n>
```

退出码：`0` 一致；`1` 调用/解析失败；`2` 缺配置；`3` 解析成功但与离线结论不一致（**不允许"返回了 JSON 就算过"**）。

## 4 固化证据

```powershell
$env:NEXENT_CONVERSATION_ID = "<上一步输出里的 conversation_id>"
uv run --locked python deploy/nexent/capture_evidence.py
```

产出到 `samples/nexent/live/`：

| 文件 | 内容 |
| --- | --- |
| `agents.json` | 北向已发布 Agent 列表（含 `urban_deduction_agent`、版本号） |
| `conversation_<id>.json` | 真实对话：用户问题 + Agent 六跳 JSON 结论 |
| `mcp_tool_log.jsonl` | 平台调用 MCP 工具的日志（工具名、状态、耗时、快照哈希） |
| `live_evidence.json` | 汇总：Agent、会话、工具调用、8 项校验结果 |

## 4.1 平台界面截图 / 录屏（可核对，不是"人工截一张图"）

`capture_platform_ui.py` 在宿主机跑（用本机已装的 Microsoft Edge，不额外下载浏览器），登录平台前端后
逐页截图，并把"这页必须出现哪些文字"写成断言：任一文字没出现即退出码 1，截图不能冒充通过。

```powershell
$env:NEXENT_WEB_URL        = "http://127.0.0.1:3000"
$env:NEXENT_ADMIN_EMAIL    = "admin@nexent.com"   # Agent 所有者所在租户的用户
$env:NEXENT_ADMIN_PASSWORD = "<平台密码>"          # 只从环境变量读，脚本不打印、不入库
uv run --with playwright python deploy/nexent/capture_platform_ui.py `
    --out docs/reports/captures/nexent --video
```

期望输出（三页都 OK 才退 0）：

```text
[ui] skill-space: OK     # 平台 Skill 仓库里能看到 urban-six-hop-deduction / scarcity-allocation-evolution
[ui] agents: OK          # 平台智能体列表里能看到 城市内涝跨部门决策智能体（V3 已发布）
[ui] mcp-space: OK       # 「我的MCP」里 urban-twin-mcp 连通性校验成功、14 个工具
[ui] 证据：docs\reports\captures\nexent\platform_ui.json
```

产物：`skill-space.png` / `agents.png` / `mcp-space.png`（MCP 页会先点一次卡片上的「连通性校验」，
所以图里是"连接成功"而不是陈旧状态）、`platform_tour.webm`（加 `--video` 时的页面浏览录屏）、
`platform_ui.json`（每页 URL、命中文字、PNG 字节数与 sha256）。

`evidence_check` 的 `platform_ui` 一项会重算这三张图的 sha256：**图被替换或重新截过却没重生成证据，自检失败**。

> 权限坑：`suadmin@`（平台总管理员）进 `workbench` 系列页面会报"无权限访问"，只有租户用户
> （如 `admin@nexent.com`）能看智能体/技能页；总管理员只能在「资源管理」里按租户看资源。
> 密码只放环境变量，不要写进仓库；忘记时可让平台管理员重置该账号。

## 5 现场演示顺序（约 2 分钟）

1. 打开 Nexent 前端 `http://localhost:3000`，选 `urban_deduction_agent`，提问核心事件；
2. 展示回答里的六跳 JSON + C1/C2/C3 + 缺口 2 台；
3. 切到 `samples/nexent/live/mcp_tool_log.jsonl`，指出 `event_deduce` / `allocation_plan` 是平台调用的；
4. 跑 `python -m decision_poc.nexent_urban` 输出 8/8，证明与离线结论一致；
5. 打开 `/evolution` 面板收尾（策略进化与回滚）。

## 6 已知边界与排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| httpx 报 `Invalid port: ':1;*.cn;...'` | 系统 `NO_PROXY` 含 `10.*;192.168.*` 等通配，httpx 解析代理规则失败 | 传输层已 `trust_env=False`（不读系统代理）；代理需显式传 |
| Agent 最终回答是 `<\|tool_call_start\|>...` 文本 | openPangu 工具调用协议需修复重试 | Agent 开 `enable_protocol_repair_retry`；提示词改成"先调 event_deduce，再只输出 JSON"两步式 |
| 工具调用走 `parallel_executor` 后卡住 | 模型自行发起并行子任务 | 提示词明确禁止 `parallel_executor` |
| `twin_neighbors` 返回 NOT_FOUND | 模型把事件编号当 `twin_id` 传 | 提示词可补"twin_id 用 `TW-incident-<工单号>`"；当前校验不依赖该工具 |
| 界面里 MCP 显示"未连接" | 卡片上的连通性是**上次校验**的快照，容器重启后不会自动重测 | 点卡片右上角「连通性校验」；`capture_platform_ui.py` 会自己点一次再截图 |
| Skills 未出现在平台 | 旧轮次只装配了 MCP 工具，`samples/nexent/skill/*/SKILL.md` 没上传 | 已解决：见 §2.1（skill_id=2/3，正文逐位一致） |

## 7 诚实边界

- 本目录脚本面向**本机 Docker 演示环境**，管理员用户与 JWT 由容器环境提供；生产环境要换成正式账号与权限体系。
- 真机结论只覆盖 `WO-20261029-007` 一个事件与 8 项一致性检查；扩事件需要扩数据集与评测用例。
- 主 Agent 只挂 **2 个工具**（`event_deduce` + `evidence_locate`）。历史真机快照的工具库为 13 个；
  当前代码清单为 14 个，平台重扫需在 Docker 恢复后执行。
  收窄挂载不是偷懒：openPangu 看到 `twin_neighbors` / `conflict_check` / `allocation_plan`
  就会去调（提示词明令禁止也拦不住），随后走 `parallel_executor` 把回答截断在 `<|tool_call_start|>`。
  工具列表才是模型真正读的"提示词"。
- 两个 SKILL.md 已进平台技能仓库**且已绑定到已发布版本**；但实测模型不读技能文档，
  所以只能说"技能已绑定、可在平台内浏览与复用口径"，不说"平台 Agent 会按技能文档执行流程"。
- 协同 Agent（6 个工具）的输出格式不稳定：实测 **2/3** 次返回可解析 JSON 且指纹逐字一致，
  失败形态是工具调用协议退化。口径是"可重跑、格式需容错"，不宣称 100%。
- 平台界面截图/录屏都来自本机 Nexent v2.7.0 前端；换环境必须重跑并重新生成证据。

## 多智能体协同 Agent（`collab_run`，路径 B）

MCP 工具数已到 **14**（含 `collab_run`，分组 `collaboration`）。平台侧两条装配线：

| 命令 | 作用 |
| --- | --- |
| `python /tmp/setup_nexent.py all` | 注册 MCP → 同步 14 工具 → 建主 Agent（挂决策/呈现工具）→ 建协同 Agent → 签发 Key |
| `python /tmp/setup_nexent.py create-collab-agent` | 只建/更新协同 Agent `urban_collab_agent` 并挂 `collab_run` 等 6 个工具 |

协同 Agent 的提示词是"两步式"：先 `collab_run`，再**逐字转述**动作卡、联合发布对、处置顺序与
`kernel_fingerprint`；不得新增卡片、不得改写冲突，指纹必须与工具返回逐位一致。

角色白名单 `ROLE_TOOL_GROUPS` 是**运行期视图**，不导出到 `tools.json`；部门 Agent 若单独建（路径 A），
只能看到本组工具。Nexent v2.7 是否支持 Agent-as-Tool **未验证**，故默认走路径 B。

> **历史真机状态**：2026-11-03 已在 Docker 恢复后完整复跑（平台工具库 13 个 / 主 Agent v10 / 协同 Agent v4）；
> 当时 `scene_status` 尚未加入平台工具库。当前代码与 MCP 清单为 14 个，需 Docker 恢复后重扫，
> 结果见文末「2026-11-03 复跑」一节；主 Agent 挂载工具数已从 12 收窄到 2，原因见该节。

## 2026-11-02 真机复跑（Docker 恢复后）

| 项 | 结果 |
| --- | --- |
| MCP 容器 | `urban-twin-mcp` 重建后 `/health` 返回 **14 个工具**（含 `collab_run`） |
| 平台工具库 | `scan_tool` 命中 **13/13**，缺失 0 |
| 主 Agent | `urban_deduction_agent` 发布 **v5**，挂 12 个结论类工具（`collab_run` 显式 detach） |
| 协同 Agent | `urban_collab_agent` agent_id=4 发布 **v1**，挂 6 个工具（含 `collab_run`） |
| 主链路复核 | `python -m decision_poc.nexent_urban` → **8/8 项一致，conversation_id=59** |
| 协同复核 | `collab_run` 真机返回 JSON，`kernel_fingerprint=d7c16d572ca7a33e` 与离线**逐字一致**，conversation_id=60 |

已知限制如实记录（证据见 `samples/nexent/live/collab_live.json`）：
- 协同 Agent 输出格式不稳定：2 次调用中 1 次返回可解析 JSON（指纹一致）、1 次未按要求输出 JSON；
- 环境侧：本机内存仅 15.3 GB，`nexent-data-process` 占 4.5 GB 时宿主只剩 0.5 GB，导致 northbound 请求被掐断；
  临时 `docker stop nexent-data-process kg-mcp` 后恢复；`localhost:5013` 端口映射失效时需 `docker restart nexent-northbound`；
- 未验证：Nexent 的 Agent-as-Tool（路径 A），无证据不宣称。

## 2026-11-03 复跑（Docker 恢复后，全链路）

| 项 | 结果 |
| --- | --- |
| MCP 容器 | 重建后 `/health` 返回 **14 个工具**；编排见 `deploy/docker-compose.mcp.yml` |
| 平台工具库 | `scan_tool` 命中 **13/13**，缺失 0 |
| 主 Agent | `urban_deduction_agent` 发布 **v10**，只挂 2 个工具（`event_deduce` + `evidence_locate`） |
| 主链路 | **8/8 项一致，conversation_id=78**，退出码 0 |
| 技能绑定 | 两个技能在草稿与 v10 发布版本均 `present=True enabled=True`（`bind_skills.py verify --require-published` 退出码 0） |
| 协同 Agent | 发布 **v4**，挂 6 个工具；3 次调用 **2 次**指纹逐字一致（`d7c16d572ca7a33e`、26 卡 / 15 序 / 6 角色） |

### 这一轮真正卡住的地方（都实测过，不是猜想）

1. **技能绑定后主链路从 8/8 掉到 0/8**。原因不是模型变笨：SKILL.md 原写"必须调
   `twin_neighbors` / `conflict_check` / `allocation_plan`"，模型照做后走 `parallel_executor`，
   回答截断在 `<|tool_call_start|>`，一个 JSON 都出不来。
2. **改提示词拦不住**。DUTY_PROMPT 加"禁止 python_interpreter / read_skill_md / parallel_executor"
   之后，conv 74 仍开 `parallel_executor`，conv 75 把调用写成 `event_deduce(event_id=...)`
   这种函数语法。提示词对工具选择的影响力远低于工具列表本身。
3. **真正的约束是挂载哪些工具**。把主 Agent 收窄到 `event_deduce` + `evidence_locate` 后，
   v10 一次跑通 8/8。
4. **装配脚本两个隐性坑**：`create_agent` 原来只在 Agent 不存在时创建，复用已有 Agent 时
   **不回写 prompt**（改了提示词却发布旧提示词）；`mint_api_key` 从 `/tmp` 跑会
   `ModuleNotFoundError: database`（缺 `sys.path`）。两处都已修。
5. **`temperature=0` 是反效果**：设成 0 后模型在工具调用后返回**空回答**（conversation 76/77，
   `history` 里 assistant `content` 长度为 0），已回退为默认值。

### 证据目录（单一事实源）

`samples/nexent/live/` 只保留本轮：`conversation_78.json`（纯 JSON 结论，3596 字符）、
`live_evidence.json`、`mcp_tool_log.jsonl`（2 次 `event_deduce`，snapshot `2085018615b3`）、
`agents.json`、`skills.json`、`collab_live.json`。旧的 `conversation_21.json` /
`conversation_70.json` / `nexent_urban_live.json` 已删除，避免"多份结论互相矛盾"。

`mcp_server._log` 已加 `ts` 字段（把某次调用归属到某一轮会话），但**本轮镜像因 registry
不可达未能重建**（`python:3.12-slim` 本地不存在、拉取返回 Bad Gateway），所以当前日志仍无 `ts`；
归属改用"容器启动时间 + 文件 mtime + 调用前清空日志"保证：`live_evidence.json` 的 `tool_log`
记了 `container_started_at` 与 `file_mtime`，本轮日志是在跑验证前清零的。

### 一键起 MCP（不再裸 `docker run`）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy/up_mcp.ps1
# 需要重建镜像时加 -Rebuild；只想体检不动手加 -KeepOld
```

脚本做四件事：Docker/网络体检 → 判定同名容器归属 → `docker compose up -d` → 等到 healthy
且**容器内 `/health` 的 `tools_count == 14`**。退出码 0 才算起好。
底层编排仍是 `deploy/docker-compose.mcp.yml`，也可以直接：

```powershell
docker compose -f deploy/docker-compose.mcp.yml up -d
docker compose -f deploy/docker-compose.mcp.yml ps   # healthcheck 校验 tools_count == 14
```

**为什么需要外面这层脚本**：编排里 `container_name: urban-twin-mcp` 是固定名（容器间靠这个名字互访，
Nexent 侧注册的地址就是 `http://urban-twin-mcp:8000/sse`），不能靠改名规避冲突。历史上这个容器是裸
`docker run` 起的、没有 compose 标签，compose 见到同名容器直接报
`Conflict. The container name "/urban-twin-mcp" is already in use`，一键部署当场失败。
`up_mcp.ps1` 补的就是这一步：比对 `com.docker.compose.project` 标签，不是本编排建的就把
`docker inspect` 快照存到 `out/backup/urban-twin-mcp.manual.inspect.<时间戳>.json` 再 `docker rm -f`，
由 compose 重新接管；`compose up` 仍报冲突时再清一次重试。重复执行是幂等的（已归属本编排则只做校验）。

healthcheck 判据是"14 个工具都在"，不是"进程没死"——容器活着但工具没注册时，平台侧只表现为
"智能体变笨"，不会报错。日志与渲染产物挂到宿主 `artifacts/visual/`，取证不必再 `docker cp`
（沙箱里 `docker cp` 常被 npipe 权限拦）。
