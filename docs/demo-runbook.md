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

## 2.5 平台技能仓库 + 界面取证（可选，约 40 秒）

平台侧两类"看得见"的证据：技能是否真在平台仓库里、界面是否真能截到。两步都幂等，不改推理结论。

```powershell
# 技能上传（已上传时输出 unchanged；新上传输出 created）
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/upload_skills.py    nexent-config:/tmp/upload_skills.py
docker exec nexent-config sh -c "rm -rf /tmp/nexent_skills"
docker cp samples/nexent/skill nexent-config:/tmp/nexent_skills
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/upload_skills.py all --json /tmp/skills_evidence.json"
docker cp nexent-config:/tmp/skills_evidence.json samples/nexent/live/skills.json

# 界面截图（需要租户账号口令，只放环境变量，不要写进仓库）
$env:NEXENT_ADMIN_EMAIL    = "admin@nexent.com"
$env:NEXENT_ADMIN_PASSWORD = "<平台密码>"
uv run --with playwright python deploy/nexent/capture_platform_ui.py --out docs/reports/captures/nexent
```

期望：技能末行 `缺失 []；正文逐位一致=True`；截图三行 `OK`，产物在 `docs/reports/captures/nexent/`。
答辩时直接打开 `skill-space.png`（两个技能在平台技能仓库里）与 `mcp-space.png`（连通性校验成功 + 15 个工具）。

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
| `decision_error: JSONDecodeError` | Agent 回了自然语言 | 确认用的是 `workbench_main`，不是 `equipment_decision_agent`（已下线） |
| `decision_error: ValidationError` | 字段缺失或多余 | 看 error 明细，必要时收紧 query 结构 |

## 6. 演示收尾（可选）

```powershell
# 撤销临时 Key
docker exec nexent-postgresql psql -U root -d nexent -c "delete from nexent.user_token_info_t where token_id=2;"
```

- 换 Agent 只改 `$env:NEXENT_AGENT_NAME`：`equipment_decision_agent`（已于 2026-11-05 从平台下线，此处为历史用法）输出四段式中文报告，
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

## 附：P9 决策进化闭环演示（本地，不依赖 Nexent，约 30 秒）

三句话讲清"智能体自己变好了"：偏差归因 → 策略调整 → 前后对比；再补分层记忆、人工评审、门禁与回滚。

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.policy_evolution --report docs/reports/p9-policy-evolution-report.md
```

期望输出（逐字）：

```text
第一次运行：训练偏差 0.2264 → 0.0000；留出 0.2182 → 0.0000；标签 flood-response-v1.2
轮次 3（采纳 2 / 拒绝 1）；关键片区未满足 15 → 6 台；应调拨未调拨 4 → 0
人工评审 REV-001（水务调度负责人（角色扮演））：评审偏差 0.1171 → 0.0000
第二次运行（记忆 M-3979c3901de5，相似度 1.0000）：起点偏差 0.0000，轮次 1
真实事件：v1 {"A-03": 4, "B-01": 2} → {"A-03": 5, "B-01": 1}
```

现场可说的六句：

1. 第 1 轮归因到"调拨"维度：先建立封路阈值优先序（25cm 硬阈值来自预案），偏差 0.2264 → 0.0471，
   产出标签 `flood-response-v1.1`。
2. 第 2 轮残留偏差指向"仅危险源也算关键"：补 `weight_hazard`，偏差 → 0，标签 `v1.2`；留出集同步下降，不是背训练集。
3. 第 3 轮是收敛探针（把调拨门槛降到 0.05），被门禁拒绝——**改坏了不生效**，active 版本不变。
4. 人工评审 `REV-001`（角色扮演）要求"从 B-01 调 1 台给 A-03、B-01 保留 1 台底盘"：该评审已变成门禁硬约束，
   评审用例偏差 0.1171 → 0；不满足它的候选一律被拒。
5. 第二次会话从分层记忆 warm start：起点偏差 0.0000、只需 1 轮探针——**记忆存的是已验证有效的策略**。
6. 回滚到 v1，偏差逐位回到 0.2264；报告里每个数字都能用 `--json samples/eval/p9_evolution.json` 复算，
   记忆在 `samples/eval/decision_memory.json`，加 `--reuse-memory` 可演示跨进程沉淀。

评委若追问"反馈是哪来的"：冻结的合成 ground truth（`samples/eval/allocation_cases.json`，8 训练 + 4 留出，逐例带依据条款）
+ 人工评审结论（`samples/eval/decision_reviews.json`），口径是"受控评测环境下的反馈信号"，**不是**真实市政数据回流；
接真实复盘数据时只替换反馈 provider，门禁与回滚逻辑不动。

评委若追问"记忆是不是真的接了 Nexent"：**没有**。这是 Tenant/User/Agent 三层的离线同构契约，接口对齐 Nexent 记忆分层，
但未真机验证；接平台时只换 `remember / recall` 的实现。这条必须直说，不能包装成"已接入平台记忆"。

### 浏览器对比面板（评委自己能点的版本，约 20 秒）

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.web_demo --port 8787
```

打开 `http://127.0.0.1:8787/evolution`（推演面板页右上角也有「决策进化对比 →」入口）。一屏从上到下：

1. 四个指标卡：训练 0.2264→0.0000、留出 0.2182→0.0000、第二会话起点 0.0000、人工评审 0.1171→0.0000；
2. 第一会话表：四项指标 + 最终标签 `flood-response-v1.2`、轮次 3（采纳 2 / 拒绝 1）；
3. 分层记忆：命中的经验条目、场景相似度 1.0、轮次 3→1；
4. 人工评审：角色扮演的修正意见与评审前后偏差；
5. 版本标签表：`v1.0 → v1.1 → v1.2`，触发反馈与触发评审可核对；
6. 真实事件分配对比：`A-03 4→5 台`、`B-01 2→1 台` 与 40 分钟跨区调拨；
7. 页脚：三条表述边界（受控反馈 / 评审角色扮演 / 记忆离线同构）——评委追问前就写在屏幕上。

面板数据来自 `/api/evolution`，与 CLI 报告同源（都取 `run_showcase()`），展示层不重算指标；
页面无外部依赖，断网也能开。

## 附：P10 可视化闭环演示（决策 → 渲染 → 对比，约 40 秒）

一句话：**不是你在 UE 里切镜头，是智能体调工具让场景变**。渲染工具只接受快照 id 与策略，
不接受位姿 / 材质 / 镜头；作业落盘即返回，引擎没起只体现为 `delivery=pending`。

### 1. 起浏览器端（评委能自己点）

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.web_demo --port 8787
```

打开 `http://127.0.0.1:8787/`。页面上「渲染基线 v1 / 渲染进化 v4 / 对比两版」三个按钮，
下面是**工具调用日志**面板，每条带 `origin`：`manual` = 人点的，`mcp` = 智能体调的。
这一栏是"谁触发了渲染"的唯一证据，演示时不要藏起来。

#### LLM 决策按钮（可选，真调 DeepSeek）

工具栏的「LLM 决策（DeepSeek）」按钮走 `/api/llm-decision`：用例固定 N01，选项与参照项由确定性推演
（直接复用 `llm_ab.build_task`）生成，模型调用与 P8 A/B 同一条 `generate_decision_live` 路径，temperature 钉 0。

- 未配置 `DECISION_LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY` 时返回 `configured=false`，页面显示"未配置"，不影响其余演示；
- 卡片顶部固定显示 Nexent 平台真机验证行（`/api/platform-evidence`，只读 `samples/nexent/live/llm_decision_platform.json`），**按 JSON 实际字段展示，不要手改**。
  当前入仓 4 条（`llm_decision_platform.json` + `run1/2/3`，2026-11-06 复测）：全部 `selected_option_id=opt-3`、`selected_match=true`；公网页面已于 2026-11-06 18:26 重发并复核过。
  修复前同名的旧记录是 `opt-1` / `selected_match=false`（`git show e351027~1:samples/nexent/live/llm_decision_platform.json`）——
  讲这一段要讲"先错后对"：错在证据没进请求，对在补齐证据原文与职责边界之后；3/3 证明的是**服从性**，不是模型独立算出排序。
- 卡片 `method` 那行原样取自证据 JSON：里面「不动 v10」是 11-06 记录时的原话，意思是这个 LLM 决策 Agent 独立、不占主 Agent 的版本号；
  主 Agent 当前是 v19，别把这句话读成"平台还在 v10"。
- 平台侧模型温度钉不住 0，重跑仍可能漂移，页面这时会如实显示不一致——不要为了好看去改 JSON。
  改过发布包内容必须重新发布，否则公网 `platform-evidence` 仍是上一版快照（同步文件不等于已上线）。

#### 公网演示地址（已发布）

`https://urban-twin-demo.app.workbuddy.host/?sharecode=K4MXY6WYTS`

- 发布包 = `agent_poc/deploy-webdemo/`（`src/` + `samples/` + `main.py` + `requirements.txt` + `.env`），
  **改完源码要先同步 `src/`、`samples/` 到该目录再重新发布**；旧版本发布会复用同一沙箱，链接与 cookie 不变。
- 分享网关要求 URL 带 `?sharecode=`，不带会 403；卡片内嵌预览不带 sharecode，所以常显示 Not Found，
  **验收一律用完整链接在真实浏览器打开**。
- 该服务对 HEAD 返回 501（Python `http.server` 未实现 `do_HEAD`），监控探活要用 GET。
- `.env` 里的 `DEEPSEEK_API_KEY` 只落发布目录（`.gitignore` 已排除 `deploy-webdemo/`），不进仓库；交完材料记得吊销轮换。

### 2. 让智能体触发（MCP 侧）

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_server --groups presentation
```

依次 tools/call：`scene_render`（不带 policy = v1 基线）→ `scene_render`（`policy` 传进化后版本）
→ `scene_compare`。期望：

```text
两次 snapshot_id 不同（内容哈希含策略，换策略即换 id）
delivery 先是 pending，UE ack 之后变 acked
verdict：策略 v1 → v4；分配变化 A-03 4→5 台，B-01 2→1 台；跨区调拨 0 → 1 次
```

### 3. 让 UE 跟着变（不用手敲 show_step）

**优先用文件总线**（同机演示，不赌端口——实测过 8788 被 Windows 拒绝绑定 `WinError 10013`、
8899 已被别的服务占用）：

```python
import urban_twin_state as s

s.selftest_render_job()   # 先校验最新作业的字段契约，打印 PASS/FAIL
s.sync_from_files()       # 取最新作业并演出（读完自动写 ack）
s.watch_files()           # 挂机监听：智能体每调一次 scene_render 就换一次场
```

引擎与 Python 分进程跑时再用 HTTP 通路：

```python
s.sync_from_api()         # GET /api/scene/latest
s.watch()                 # 轮询最新作业
```

无头验证同一条路径：

```powershell
UnrealEditor-Cmd.exe UrbanTwinDemo.uproject -run=pythonscript `
  -script="<repo>/visualization/unreal/Content/Python/urban_twin_state.py" `
  -unattended -nosplash -nullrhi -UrbanSelftest=1
```

> 本轮这台机器上 UE 进程已退出（8000 不监听），上面这条**未实机跑过**。
> 已验证的是脚本对引擎的调用契约：`tests/test_unreal_twin_state.py` 用 stub 替换
> `unreal` 模块，断言场景切换、高亮、面板、ack 的调用序列都正确。

### 现场可说的三句

1. 快照 id 是内容哈希：同一决策同一 id，评委可当场核对"渲的是不是刚才那份结论"。
2. 渲染是异步的：引擎没起不阻塞决策，只是 delivery 停在 pending——**可视化不是决策的强依赖**。
3. 对比是策略差异的出口：v1 与进化版在同一事件上跑，分配向量不同即得差异，数字与 P9 报告逐位一致。

### 材质资产：Blender 只在离线跑

```powershell
# 重生成 4 套场景 + 预览图（离线，约 30 秒）
uv run --locked python -m decision_poc.scene_layout      # 布局变了才需要
& "D:\Program Files\Blender Foundation\Blender5.4\blender.exe" -b `
  -P visualization/blender/generate_urban_twin.py -- --scenarios all --preview
```

`asset_lookup` 工具只核对 glb/blend 是否就绪并给出这条命令，**不会在推演循环里启动 Blender**。

### 两条明确不做

- 不做 UE 像素流：浏览器可交互已由 `/`（无外部依赖）承担，再叠一套推流只增部署风险；
- 不做实时逐帧同步：异步快照回放足够撑起"决策 → 渲染 → 对比"的叙事。

## 附：评委现场自检（一条命令，约 4 秒）

被问"你这些数字怎么保证不是手写的"，直接跑这条——它重跑六跳推演、P8 评测、P9 双会话进化与 MCP 工具清单，
逐项比对冻结期望，任何一项漂移都会打印 `! 路径: 期望 X 实际 Y` 并返回退出码 1：

```powershell
Set-Location D:/华为ICT/agent_poc
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.evidence_check
# 对外数字自检：108/108 项一致（期望指纹 c06b7f1eab2a58e2）
echo "exit=$LASTEXITCODE"
```

可主动演示"自检真的会拦"：把 `samples/eval/evidence_expectations.json` 里
`p9_evolution.train.initial_deviation` 改成 `0.2`，再跑一次——自检报 `! p9_evolution.train.initial_deviation`、
退出码 1；改回去恢复 108/108。这说明期望是冻结口径，不是跟着代码动态变的。

完整逐项对照见 `docs/reports/evidence-check-report.md`（由命令生成，勿手改）。

