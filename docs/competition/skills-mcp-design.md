# Skills / MCP 设计说明

对应赛题要求第 3 条：「深度集成 Nexent 的 MCP 协议与 Skills 编排能力，打造检索-推理双驱动协同智能体执行流」。

本文只写**已经跑通的**设计，不写规划中能力。每条设计后面标出对应的验证证据；验证没做过的会明确标「未验证」。

---

## 一、总体分工：MCP 管事实，Skill 管流程

| 层 | 承担什么 | 不承担什么 |
|---|---|---|
| **MCP 工具**（15 个：11 在线决策 + 4 离线渲染） | 给出**可引用的事实**：事件受影响范围、设施状态、资源缺口、证据出处 | 不做判断，不生成措辞 |
| **Skill**（2 个） | 规定**判据与流程**：什么时候该调哪个工具、缺口的两种情形怎么区分、不可调出的条件 | 不产出事实，不替代工具 |
| **Agent 提示词** | 约束**输出形态**：先调哪个工具、JSON 字段不得增删、以什么方式交卷 | 不重复技能正文的判据细节 |

这个分工的实质是：**工具负责"是什么"，技能负责"怎么判断"，提示词负责"怎么交差"**。三者混在一起会出现"模型自己算数"或"工具调了但结论跑偏"，本项目两种都踩过（见第五节）。

---

## 二、MCP 设计

### 2.1 服务形态

- 服务名 `urban-twin-mcp` v0.6.0，协议版本 `2025-06-18`，传输 SSE；
- 容器内 `python -m decision_poc.mcp_sse --port 8000`，宿主映射 18000；
- 在 Nexent 侧注册为远程 MCP（`http://urban-twin-mcp:8000/sse`），工具由 `scan_tool` 同步进平台工具库；
- 工具实现受 `pytest` 覆盖，工具清单与平台侧一致性有专门核对（15/15 逐名一致）。

### 2.2 工具分区（互斥 allowlist）

工具不是"全部挂给所有 Agent"，而是按**互斥分区**组织（`src/decision_poc/mcp_server.py` 的 `TOOL_GROUPS`）：

| 分区 | 数量 | 工具 | 为什么单独分区 |
|---|---|---|---|
| `decision` | 10 | `semantic_query` `twin_neighbors` `doc_search` `evidence_locate` `claim_trace` `provenance_verify` `event_deduce` `counterfactual_run` `conflict_check` `allocation_plan` | 推演主链路，允许在推理循环里出现 |
| `presentation` | 3 | `scene_render` `scene_status` `scene_compare` | **只在决策定稿后启用**。若在推演时可见，模型会把推理预算花在"要不要换个镜头"上 |
| `bootstrap` | 1 | `asset_lookup` | 只在初始化阶段用，避免运行期反复查资产元数据 |
| `collaboration` | 1 | `collab_run` | 一次调用返回 6 个角色的动作卡与裁决后的处置顺序，避免总控逐个调工具导致顺序漂移 |

互斥是硬约束：一个工具只能属于一个分区，`tests/integration/test_tools_schema.py` 会校验分区互斥性。理由是让"可见工具集"与"当前阶段"绑定，而不是靠提示词请求模型自律。

### 2.3 部门角色白名单（运行期视图）

`ROLE_TOOL_GROUPS` 与 `TOOL_GROUPS` 分开维护：前者是**运行期视图**（同一工具可出现在多个部门视角里），后者是**互斥分区**。两者语义不同，混用会破坏分区一致性测试。

| 角色 | 可见工具 |
|---|---|
| `role_drain`（排水） | `twin_neighbors` `event_deduce` `allocation_plan` |
| `role_road`（道路） | `twin_neighbors` `evidence_locate` |
| `role_traffic`（交警） | `twin_neighbors` `evidence_locate` |
| `role_gas`（燃气） | `twin_neighbors` `claim_trace` |
| `role_power`（电力） | `twin_neighbors` `allocation_plan` |

这张表就是"跨部门协同"在工具层的落地：燃气部门看不到调拨工具，电力部门看不到条款取证工具。`collab_run` 在总控侧一次拿全量动作卡，再按白名单分发。

### 2.4 挂载矩阵（当前平台实际状态）

| Agent | 启用工具数 | 具体工具 | 设计意图 |
|---|---|---|---|
| `urban_deduction_agent`（主，六跳推演 + 协同入口） | **4** | `event_deduce` `evidence_locate` `doc_search` `collab_run` | **有意收窄**。第一个动作必须 `event_deduce`，它一次返回六跳全部事实、三类冲突、资源缺口与 `action_order`。`doc_search`（2026-11-13 加）限「最多 1 次、只提供依据不提供事实」；`collab_run`（2026-11-14 加）只在被问"谁去干/怎么分工"时才调，且**不许逐条抄 26 张动作卡**（会撑爆回答触发截断）。挂载数从 2 → 3 → 4，每次加都是被"要不要多一个入口"逼出来的，不是配置遗漏 |
| `urban_collab_agent`（协同总控） | **6** | `event_deduce` `conflict_check` `allocation_plan` `collab_run` `twin_neighbors` `evidence_locate` | 总控需要在一次会话里处理"谁先动、谁让路、资源怎么分"，工具面必须比主 Agent 宽 |
| `urban_asset_qa`（台账问答） | **1** | `knowledge_base_search`（平台内置，非 MCP） | 职责单一：只回答"台账里写了什么"，不碰推演 |

**「只挂 4 个」是设计取舍，不是配置遗漏。**（2 → 3（`doc_search`）→ 4（`collab_run`）。）这条最容易在评审时被误读成"集成度不够"，所以在说明文档里要主动说明：集成度体现在**工具契约的完备性与分区约束**，不是挂载数量。反例有实测：2026-11-03 把 12 个工具全挂上主 Agent，模型转去调 `parallel_executor`，8/8 掉到 0/8。

**关于 `collab_run` 进主链路（2026-11-14 实测，推翻了旧假设）**：原注释写"挂上会绕过 `event_deduce`，8/8 守不住"。实测下来：**8/8 仍然全过，答案 1484 字符、没有把 26 张卡片抄进去**。原因很直接——`collab_run` 内部走的是同一个确定性内核，六跳字段一样齐。工具日志显示那轮模型确实调的是 `collab_run` 而不是 `event_deduce`：**路径变了，结论没变**。所以现在挂，但用提示词限死触发条件与转述范围。这条也顺带说明了一件事：8/8 校验的是"结论与离线一致"，不是"必须走 event_deduce 这条路"——口径要按前者讲。

### 2.5 工具契约的一道硬约束

所有工具返回统一包成 `{status, data, version}`，`status` 只有 `ok` / `error` 两值，错误码用 JSON-RPC 标准（-32602 等）。这样做的目的是：**模型读不到"半个结果"**。若某次检索部分失败，返回 `status=error` 而不是残缺数据，避免模型在缺字段的情况下自行补全。

### 2.6 三条接入路径的取舍（官方 08:17）

平台支持三条路：① 直连 MCP 服务地址；② 拉远端 server 到本地起容器；③ 导入 OpenAPI，由平台做 **API 转 MCP**。

**我们走 ①②**：自研 `mcp_server.py`（stdio，供本地与测试）+ `mcp_sse.py`（SSE 容器，供平台），平台按 ① 注册
`http://urban-twin-mcp:8000/sse`；容器编排与健康检查由 `deploy/docker-compose.mcp.yml` + `up_mcp.ps1`
提供，等价于 ② 的"自己把 server 跑起来"。

**不用 ③ 是主动选择，不是不会用**，三条理由：

1. **参数契约会失真**。我们的工具有真约束：`predicates` 只接受 8 个枚举谓词、`kind_filter` 只接受固定对象类型、
   `top_k` 有 min/max、`hydrograph` 是嵌套 schema。从 OpenAPI 反推出来的 schema 拿不到这些，
   模型就会传非法值，而错误要到运行时才暴露。
2. **工具级语义会丢**。`status ok/error` 的统一包装、JSON-RPC 错误码、受限资产的 `caller_labels` 校验，
   都是写在实现里的语义。API 转 MCP 只留 HTTP 状态码，模型读不到"为什么失败"。
3. **六跳的 schema 钉不住**。`event_deduce` 一次返回六跳 + 三类冲突 + 缺口 + `action_order`，
   字段名必须与验收判据逐字对齐；走 API 包装后字段名由 OpenAPI 决定，改一次名就让 8 项判据失效。

代价说清楚：①② 要自己维护容器、协议与健康检查（我们为此写了 `mcp_sse.py`、`/health` 的
`tools_count` 校验、`up_mcp.ps1` 一键起）。换来的是契约可控、错误可读、schema 可锁。

### 2.7 推理模式：为什么走 ReAct 不走 planning（官方 07:16）

平台支持 planning 模式与流式输出。我们走 ReAct（推理-行动交错），**这是有意的**。

一句话口径：**planning 的计划树对不上哈希账本；ReAct 的执行轨迹能逐跳对上。**

展开三条：

1. **没有事实的规划只能靠先验，那就是编**。我们的硬约束是"第一个动作必须是 `event_deduce`，
   事实只认工具返回"。ReAct 天然满足——每一步都等工具结果回来才走下一步。
   planning 要求先出计划树，但计划阶段手上还没有事实。协同侧 C 组实验正好是反证：
   无工具 + 要求"凭经验出方案"，它编出一个**格式完全正确**的 16 位指纹（`b7f3c2a9e41d5806`），
   卡片数量只有真实的 8/26。
2. **可溯源要对得上账**。六跳每一跳都要落到 `asset_id#locator`。ReAct 的执行轨迹是线性的、与账本一一对应；
   计划树是预生成的，执行时可能与实际取证路径脱节，被追问"这条结论来自哪一步"就说不清。
3. **收窄工具面比换模式有效**。2026-11-03 的教训是：模型看到不该调的工具就会调，随后走
   `parallel_executor` 把回答截断在 `<|tool_call_start|>`，8/8 掉到 0/8。这是**工具面**问题，
   不是推理模式问题——换成 planning 只会多一层计划生成，坑还在。

边界（主动说）：ReAct 的代价是**步数不可预估**，长链路容易撞上输出截断。我们的对策是
工具面收到 3 个 + 输出预算 8192 + `enable_protocol_repair_retry`，而不是换推理模式。

---

## 三、Skills 设计

### 3.1 两个技能的定位

| 技能 | 正文规模 | 触发条件（description 原文摘要） |
|---|---|---|
| `urban-six-hop-deduction` | 2710 字 | 用户给出事件编号与时点时，需要回答"哪些设施受影响、哪些部门协同、资源够不够、处置按什么顺序、每条结论依据什么"。**必须调 MCP 工具取证，禁止凭记忆作答** |
| `scarcity-allocation-evolution` | 1880 字 | 六跳推演显示跨片区资源存在缺口、需要回答"这几台先给谁"时；或比较 v1 与进化后策略的分配差异、回滚验证。**所有分配必须来自策略工具，不允许按经验口算** |

两个技能里都写了**边界条款**，这是设计上最花心思的部分：

- 六跳技能写明"用户只问六跳结论时不要走分配流程"——防止模型为了"更完整"多调一个 `allocation_plan`，把简单问答拖成两步；
- 分配技能写明"关键片区不可被调出"（`closure_required=True` 或 `has_hazard=True` 默认禁止抽调）、"调拨必须写 40 分钟时延"、"缺口不得抹平"；
- 两个技能都写明口径："反馈来自受控评测环境下的反馈信号，不得表述为真实市政数据回流"。这条是**为了演示时不越界**，写进技能而不是写进 PPT，是因为模型在生成时会复述技能里的表述。

### 3.2 渐进式披露机制（平台行为，实测确认）

平台对技能的处理分两层：

1. **`<available_skills>` 块**：只注入 `name` + `description`，随每轮请求常驻。源码位置 `sdk/nexent/core/agents/context/formatting.py:110` 的 `_format_skills_description()`。
2. **正文按需读**：注入块里附了六步「技能使用流程」，第一步就要求调用 `read_skill_md("skill_name")` 读取 SKILL.md 全文。正文**不会**自动进提示词。

这么设计的好处是每轮 token 成本恒定（两个技能只占约 120 字，而不是 4590 字）；代价是**如果 `read_skill_md` 不可用，技能正文对模型就是不可见的**。

### 3.3 关键取舍：主 Agent 显式禁用 `read_skill_md`

主 Agent 的 `duty_prompt` 里有一条禁止事项：

> 不要用 python_interpreter 读技能文档或写代码；**不要调用 read_skill_md**

这与平台注入的"请调 `read_skill_md` 读正文"是**直接冲突的指令**。冲突是有意为之，理由：

- 主 Agent 的价值在于**同一输入得到同一结论**（12 例冻结用例要逐例复现）；
- 一旦允许读技能正文，模型会在"读文档"和"调工具"之间自由选择，六跳 JSON 的字段对齐率会掉；
- 六跳需要的判据已经**内联在提示词里**（字段清单、字段不得增删、`final_answer` 交卷格式），技能正文是同一套规则的**文档版**，供人阅读与导出包存档。

**因此必须区分两种说法，不能混**：

| 错误说法 | 正确说法 |
|---|---|
| "我们的技能在运行时驱动了推演" | "技能是流程与判据的**文档化载体**，随导出包交付；主 Agent 为保证确定性，把这些判据内联进提示词，并显式禁用正文读取工具" |
| "模型读了技能才按步骤做的" | "模型按提示词的两步式约束执行；技能正文与提示词描述同一套流程，区别是一个给人看、一个给模型看" |

如果评委追问"那技能到底起什么作用"，诚实答案是：**技能承担设计与交付职责（官方要求交 Skills 设计说明与导出包），提示词承担运行期职责**。把这个边界说清楚比含糊其辞更稳。

### 3.4 技能绑定现状

- `urban-six-hop-deduction` 与 `scarcity-allocation-evolution` 均已上传平台技能库（`source=custom`），绑定在 `urban_deduction_agent` 上；
- 另有一个 `ontology-builder` 技能（1835 字）在库中但 `delete_flag=Y`（已废弃），属历史遗留，不要计入交付物；
- 导出包（`<agent_name>.zip`）内 `agent.json` + `skills/` 两个技能目录齐全，这是官方认可的「配置能力」证据。

---

## 四、检索-推理双驱动：三条检索路径的分工

这是本项目与"单靠一个搜索工具"的队伍的主要差别：**三条路径各管一段，不互相替代**。

| 路径 | 实现 | 检索什么 | 谁在用 |
|---|---|---|---|
| **结构化台账检索** | MCP `doc_search` | 自建资产台账（CSV 行级、md 条款级） | 主 Agent 取证 |
| **平台知识库检索** | Nexent 内置 `knowledge_base_search` | 同一批源文件切成的 **118 个切片、3.7 万字**，走 ES 混合检索（向量 bge-m3 + 关键词） | 台账问答 Agent |
| **图谱邻接查询** | MCP `twin_neighbors` | 本体图上的相邻实体（设施↔路段↔管线↔部门） | 主/协同 Agent |

分工理由：台账问题要的是**精确字段值**（`capacity_m3h=1200`），向量检索容易召回语义相近但数值不同的记录；而"哪个部门管泵站"这种问题适合语义检索。所以不让一个工具包办。

知识库入库口径写在 `deploy/nexent/ingest_knowledge.py` 头部：Markdown 按二级标题切、CSV 按行转记录（每行一条，正文带列名语义）。CSV 若整篇当一个 chunk，8 行台账会被压成一个千字块，检索命中后模型拿到的仍是全部行，等于没切。

**实测检索效果**（`artifacts/p1-nexent/_p3_kb.sse`）：

- 问「DF-PS-01 泵站什么状态、设计流量多少、运维主体是谁」，检索命中 `drainage_facilities.csv · DF-PS-01`；
- 回答给出 `state=fault_pending_repair`、`capacity_m3h=1200`、`failed_units=1`、关联工单 `WO-20261026-013`，每条标出处；
- 对"设计流量"明确回答**「台账中无 design_flow 字段，仅有 capacity_m3h=1200，台账中未记录设计流量」**——没有把容量当流量编出来。这条比命中更值钱：它证明"不推测"的约束生效了。

---

## 五、踩过的坑（都是会让人误判成"能力不存在"的）

1. **`version_no` 不能放进 `/agent/update` 请求体**。它会被写进 `UPDATE ... SET version_no`，撞 `ag_tenant_agent_t_pkey`，返回 500。
2. **改草稿不发布等于白改**。`/agent/update` 只写 `version_no=0` 草稿；对话读**已发布版本**。第一次改完重跑发现事件数没变，不是配置没生效，是改错了行。
3. **`verification_config: null` 清不掉**。更新实现里 `if value is None: continue`，传 null 等于没传，只能改库。
4. **知识库接口返回的 `id` 才是索引名**。`POST /api/indices/{name}` 传显示名，但后续 `/documents`、`/files` 必须用返回体里的 `id`（形如 `2-5ab0...`），用显示名会 404 `Knowledge base 'xxx' not found`。
5. **`/api/tool/list` 返回数组不是对象**。按 `{"tools": [...]}` 解析会得到空列表，报"工具库里找不到"。
6. **ES 磁盘水位会让新索引分片无法分配**。D 盘用到 92%（高于 `watermark.high=90%`）时，新建索引直接 red、写入 20 秒超时。表现为"知识库接口超时"，不是接口 bug。

---

## 六、验证与证据

| 验证项 | 方法 | 结果 | 证据 |
|---|---|---|---|
| 工具名一致性 | `TOOL_GROUPS` 并集 vs 平台 `ag_tool_info_t where source='mcp'` | 15/15 逐名一致 | 本文 2.2 表 |
| 工具分区互斥 | 分区一致性测试 | 通过 | `tests/integration/test_tools_schema.py` |
| MCP 服务可用 | 容器 `/health` | 200 | 容器 `urban-twin-mcp:dev` |
| 知识库入库 | `ingest_knowledge.py --apply` | 118/118 条，索引 green | `artifacts/p1-nexent/_kb_ingest.log` |
| 知识库检索命中 | ES `_search` + Agent 实跑 | 命中正确记录，未编造缺失字段 | `artifacts/p1-nexent/_p3_kb.sse` |
| 技能已上传 | `ag_skill_info_t` | 2 个 `source=custom`，正文 2710 / 1880 字 | 数据库查询 |
| 主 Agent 两步式生效 | 实跑 `WO-20261029-007` | 先 `search_memory`、再 `event_deduce(event_id=...)`，13 秒输出结构化六跳。注意 `search_memory` 是**平台内置记忆工具**（`store_memory` / `search_memory` 在每轮准备阶段固定执行），不属于 15 个 MCP 工具，也不占主 Agent 的 3 个挂载名额 | `artifacts/p1-nexent/_p2_content.sse` |

---

## 七、明确不主张的部分（避免过度包装）

- **不主张"技能在运行期驱动了推演"**。正文不进提示词，这是平台机制决定的，见 3.3。
- **不主张"15 个工具全部挂载在主 Agent"**。主 Agent 只挂 3 个（`event_deduce` / `evidence_locate` / `doc_search`），是设计取舍。
- **不主张知识库内容来自真实市政系统**。源文件是 `samples/assets/urban/` 下的构造资产，口径见各文件 `note` 字段与 `docs/reports/` 里的说明。
- **不主张 RAG 与图谱做了融合排序**。三条检索路径是并列的，由模型按问题类型选路，没有做统一重排。
