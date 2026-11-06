# Nexent 侧装配与真机验证（可复现）

把本仓库的城市孪生 MCP 服务接入本机 Nexent v2.7.0，并验证"平台 Agent 调 MCP 工具 → 六跳结论与离线一致"。

已验证结果（2026-11-06 复测，主 Agent **v19** / 协同 Agent **v7**，模型 **DeepSeek-Chat**）：

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 主链路 | **8/8 项一致**，conversation_id=134，attempts=1（协议层零重试） | `samples/nexent/live/main_v19_baseline.json` |
| 反事实对照 | 同一提示词解绑 14 个 MCP 工具后 **2/8**；需要工具取数的 6 项全塌 | `samples/nexent/live/control_no_tools.json` |
| 多事件回归 | 4 个事件全部 **8/8**（007/009/013/021，各自一次通过） | `samples/nexent/live/multi_event_regression.json` |
| 协同链路 | 连跑 **2/2**，`kernel_fingerprint=ab94413cf94bc9a5` 与离线逐字一致（26 卡 / 15 序 / 6 角色） | `samples/nexent/live/collab_v7.json` |

> 两段主链路对话都真实存在：**120** 是换 DeepSeek-Chat 后的首测（材料里引的是它，文件
> `samples/nexent/live/conversation_120.json`），**134** 是同日 17:56 重采的基线（上面的证据文件）。
> 都是 8/8、attempts=1，差别只是采集时点。

平台实际调用 `event_deduce` 1 次，工具日志与该轮一一对应（`samples/nexent/live/mcp_tool_log.jsonl` 仅 1 行）。
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

## 2 平台侧装配（单入口，幂等）

平台管理 API 需要 JWT，密钥只存在于 `nexent-config` 容器里，所以在容器内用后端自己的 venv 执行装配脚本。
四个文件一起拷（`bind_skills.py` 会 `import upload_skills`，少拷一个就 `ModuleNotFoundError`）：

```powershell
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/setup_nexent.py    nexent-config:/tmp/setup_nexent.py
docker cp deploy/nexent/upload_skills.py   nexent-config:/tmp/upload_skills.py
docker cp deploy/nexent/bind_skills.py     nexent-config:/tmp/bind_skills.py
docker cp samples/nexent/skill             nexent-config:/tmp/nexent_skills
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/setup_nexent.py all"
```

`all` 依次跑六步，任何一步非 0 就停下（不会带着半成品去发 Key）：

| 步骤 | 作用 | 幂等策略 |
| --- | --- | --- |
| `register-mcp` | 注册 MCP 服务 `urban-twin-mcp`（URL `http://urban-twin-mcp:8000/sse`） | 同名已存在则复用 |
| `sync-tools` | `scan_tool` 把 14 个 MCP 工具同步进平台工具库 | 已存在则覆盖状态 |
| `create-main` | 创建/复用 `urban_deduction_agent`（**upsert**：带 `agent_id` 回写提示词），挂 2 个结论类工具并发布 | 同名 Agent 复用；重复挂载无害 |
| `create-collab` | 创建/复用 `urban_collab_agent`，挂 6 个协同工具并发布 | 同上 |
| `skills` | 上传 SKILL.md → 绑 Agent 草稿 → **重新发布** → 复核已发布版本 | 上传按正文哈希判 `unchanged`；绑定是覆盖写 |
| `mint-key` | 生成北向 API Key | 每次生成新 Key（旧 Key 可继续用） |

`skills` 必须排在两次发布之后：绑定只能写草稿（`version_no=0`），运行期读的是发布快照。
少最后那次重新发布，表里草稿有、快照没有，**看起来绑了、实际不生效**。这条顺序以前靠人记，
现在由 `tests/test_nexent_setup_steps.py` 锁住。

> 分步排障：`register-mcp` / `sync-tools` / `create-agent` / `create-collab-agent` /
> `bind-skills` / `mint-key` / `status`。单条命令会自带它的前置步骤（例如 `bind-skills`
> 会先 register + sync），所以单独跑一次也是完整状态。

装配完用 `status` 自检，三档退出码：

```powershell
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/setup_nexent.py status"
```

| 退出码 | 含义 |
| --- | --- |
| 0 | MCP / 14 工具 / 两个 Agent / 技能草稿与发布快照全部就位 |
| 1 | 有硬失败（缺工具、Agent 不存在、技能 verify 不过） |
| 2 | 有项**无法断言**（如技能目录没挂进容器）——这不是通过 |

分 1 和 2 是因为旧版 `status` 只查 MCP / 工具 / Agent，技能根本不在它视野里，
"没检查"和"检查通过"返回同一个 0。

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
--require-published` 复核，退出码 0）。

它在运行时到底起什么作用，分三层说，别混成一句：

| 层 | 事实 | 依据 |
| --- | --- | --- |
| 注入什么 | 平台把每个 enabled 技能的 `name` + `description` 拼进 `<available_skills>` 块，随系统提示下发，并指示模型"先审视 description 判断是否匹配" | `Nexent/sdk/nexent/core/agents/context/formatting.py:124-144` |
| 没注入什么 | `SKILL.md` 正文**不会**自动进 prompt；要读得靠 `read_skill_md("skill_name")` | 同上 `:146` |
| 我们怎么处理 | 主 Agent 的 `DUTY_PROMPT` **显式禁止** `read_skill_md`（`setup_nexent.py:96`），所以正文不参与推理 | `deploy/nexent/setup_nexent.py` |

第三行是**有意取舍**，不是平台缺陷：2026-11-03 让模型读到正文后，它照着正文去调
`twin_neighbors`/`conflict_check`/`allocation_plan`，走 `parallel_executor`，回答截断在
`<|tool_call_start|>`，8/8 直接掉到 0/8。所以现在的口径是"只让 description 起作用，正文关掉"。

由此能宣称与不能宣称要分开：

- **能宣称**：技能绑定有效（草稿与发布快照都有实例）；技能的 description 会进入模型上下文，
  影响它对任务的理解。
- **不能宣称**："技能正文参与了推理"或"技能驱动了工具选择"。当前 8/8 证明的是 MCP 工具链与
  输出协议有效，技能正文全程没进上下文。现场被追问时按这个边界答。

两个技能的 `tool_ids` 都是空的——技能本身没挂工具，工具是按 Agent 挂的。

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

## 4.2 导出平台作品包（初赛「配置能力」项）

官方赋能直播（`BV18Kxv6jEuF`，17:05）要求初赛交「配置能力」= 用平台导出功能导出作品。
讲师口语说的「DSR / DCR 文件」**平台上不存在同名格式**，实际是下面这个 zip。

```bash
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/export_bundle.py    nexent-config:/tmp/export_bundle.py
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/export_bundle.py \
    --agent-id 3 --out /tmp/urban_deduction_agent.zip --json /tmp/agent_export.json"
docker cp nexent-config:/tmp/urban_deduction_agent.zip samples/nexent/live/urban_deduction_agent.zip
docker cp nexent-config:/tmp/agent_export.json       samples/nexent/live/agent_export.json
```

- 包结构：`agent.json` + `skills/<name>.zip`（本仓库实测 7 890 B / 2 个技能）。
- **整包 sha256 不可复算**（zip 条目头带导出时刻时间戳，连导 5 次得 5 个值）；材料里用
  `agent_export.json` 的 `bundle.content_sha256`（实测 `adf49c00…`，连导 5 次稳定）。
- 导出包只存 `mcp_info` 的 **server 地址**，不含工具实现 → 复现包必须同时给
  `src/decision_poc/mcp_server.py` / `mcp_sse.py` 与容器启动方式。
- 口径提醒：平台注册 14 个工具，但主 Agent 只挂 2 个（`MAIN_TOOLS`）、协同 Agent 挂 6 个
  （`COLLAB_TOOLS`），见 `setup_nexent.py:76-134` 的注释。

细节与交叉核对结论见 [`docs/reports/agent-export-evidence.md`](../../docs/reports/agent-export-evidence.md)。

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
- 主 Agent 只挂 **2 个工具**（`event_deduce` + `evidence_locate`）。平台工具库当前是 14 个
  （`setup_nexent.py status` 显示 `命中 14/14`），主 Agent 的挂载是这 14 个里的 2 个子集。
  收窄挂载不是偷懒：openPangu 看到 `twin_neighbors` / `conflict_check` / `allocation_plan`
  就会去调（提示词明令禁止也拦不住），随后走 `parallel_executor` 把回答截断在 `<|tool_call_start|>`。
  工具列表才是模型真正读的"提示词"。
- 两个 SKILL.md 已进平台技能仓库**且已绑定到已发布版本**（草稿 v0 与发布快照各 2 条实例）。
  运行时效果分三层说，别压成一句：
  1. **会注入**：平台把每份 enabled 技能的 `name` + `description` 拼进 `<available_skills>`
     随系统提示下发，并指示"先审视 description 判断是否匹配"
     （`Nexent/sdk/nexent/core/agents/context/formatting.py:124-144`）；
  2. **不注入**：`SKILL.md` 正文要调用 `read_skill_md("skill_name")` 才进 prompt（同文件 `:146`）；
  3. **我们禁了**：主 Agent 的 `DUTY_PROMPT` 显式禁止 `read_skill_md`（`setup_nexent.py:96`），
     所以本轮运行确实没有读取正文——这是 11-03 读正文导致 8/8→0/8 后的有意取舍，不是平台不支持。

  因此口径是"技能已绑定，`description` 参与任务理解"，不说"平台 Agent 按技能正文执行流程"。
- 协同 Agent（6 个工具）的输出稳定性：v6 时代实测 **2/3**（失败形态是工具调用协议退化）；
  v7（DeepSeek + `final_answer` 交卷）连跑 **2/2**。样本量小，口径仍是"可重跑、格式需容错"，不宣称 100%。
- 平台界面截图/录屏都来自本机 Nexent v2.7.0 前端；换环境必须重跑并重新生成证据。

## 反事实对照：8/8 是工具给的，还是模型本来就会

8/8 只证明"端到端一致"，不能排除"这套题模型凭常识就能答"。评审会问这句，所以留了一个对照组。

做法（`no_tools_control.py`）：新建 `urban_control_no_tools`（agent_id=38），**提示词、模型、输出预算、
发布流程与主 Agent 完全相同**，只把 14 个 urban 工具全部 `enabled=false`，再问同一个事件、同一句话。

```powershell
docker cp deploy/nexent/no_tools_control.py nexent-config:/tmp/no_tools_control.py
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/no_tools_control.py"

# 宿主侧用同一套验收脚本，只换 Agent 名
$env:NEXENT_AGENT_NAME = "urban_control_no_tools"
uv run --locked python -m decision_poc.nexent_urban --attempts 2 --json samples/nexent/live/control_no_tools.json
```

实测 **2/8**（主 Agent 同一时点 8/8，conversation 134）。塌掉的是需要工具取数的 6 项：
冲突编号、冲突类型、资源缺口、隔离先于抽排、关键处置动作、证据非空。通过的两项不需要工具——
`事件编号一致`是把输入回填，`六跳齐全`是照抄提示词里那六个字段名。

第 1 次调用连 JSON 都没产出；第 2 次模型按约束拒答，原话：

> 无法完成六跳推演：本轮运行环境未注入 MCP 工具 event_deduce（工具调用不可见，报 NameError），
> 六跳事实、三类冲突、资源缺口与处置顺序均无法取证，按规则不输出结论。

这同时说明提示词里"拿不到工具返回就不输出结论"那条约束是生效的——它没有硬编一套看起来很对的答案。

边界：只覆盖 `WO-20261029-007` 一个事件；结论是"这个配置下，去掉工具就拿不到那 6 项"，
不是"任何模型没有工具都必然答错"。对照组 Agent 保留在平台上（display_name 已标"对照"），
便于换环境后重跑同一实验，不作为业务入口。

## 多智能体协同 Agent（`collab_run`，路径 B）

MCP 工具数已到 **14**（含 `collab_run`，分组 `collaboration`）。平台侧两条装配线：

| 命令 | 作用 |
| --- | --- |
| `python /tmp/setup_nexent.py all` | 注册 MCP → 同步 14 工具 → 建主 Agent → 建协同 Agent → **上传技能 + 绑草稿 + 重新发布 + 复核** → 签发 Key（六步任一失败即停） |
| `python /tmp/setup_nexent.py status` | 只读自检：MCP / 14 工具 / 两个 Agent / 技能草稿与发布快照；0 全绿、1 有失败、2 无法断言 |
| `python /tmp/setup_nexent.py bind-skills` | 只重跑技能链（改过 SKILL.md 后用它；会重新发布一版，版本号 +1） |
| `python /tmp/setup_nexent.py create-collab-agent` | 只建/更新协同 Agent `urban_collab_agent` 并挂 `collab_run` 等 6 个工具 |

协同 Agent 的提示词是"两步式"：先 `collab_run`，再**逐字转述**动作卡、联合发布对、处置顺序与
`kernel_fingerprint`；不得新增卡片、不得改写冲突，指纹必须与工具返回逐位一致。

验收脚本 `scripts/nexent_collab_live.py`（多次调用算成功率，不是跑通一次就宣称稳定）：

```powershell
$env:NEXENT_BASE_URL = "http://localhost:5013"
$env:NEXENT_API_KEY  = "<mint-key 生成的 Key>"
uv run --locked python scripts/nexent_collab_live.py --attempts 2 --json samples/nexent/live/collab_v7.json
```

判定只看一件事：从最终回答里解析出含 `kernel_fingerprint` 的 JSON，且与离线 `collab_run` 逐字一致。
平台两种合法外壳（裸 JSON / `final_answer` 包装）都认，否则会把动作历史里的片段当结论。

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
