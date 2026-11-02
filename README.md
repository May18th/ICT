# ICT：可进化决策智能体 PoC

这是一个面向华为 ModelEngine Nexent 赛题方向的决策智能体工程：**离线内核可复现、平台上可运行**。默认离线（不联网、不带凭据），
只在内核验证完成后通过北向 API / MCP 接入真实 Nexent。城市内涝主链路已在本机 Nexent v2.7.0 真机跑通，
平台 Agent 调 MCP 工具产出的六跳结论与离线推演 8/8 一致（conversation_id=21，证据见 `samples/nexent/live/`）。

当前提供 `NexentAdapter` 作为离线适配边界：它只把 `DecisionRequest` 转成可传输的 JSON 负载，并调用注入的传输函数；默认不会联网。真实 Nexent 接入由 `NexentHttpTransport` 承担，走官方北向 `/nb/v1/chat/run` SSE 契约（Bearer API Key + `Idempotency-Key`），并且只在显式构造时才联网，现有审批、评测和回滚门禁保持不变。

策略演进可通过 `evaluate_and_apply` 执行离线评测门禁：候选策略只有在 `EvaluationReport.passed` 为真时才会升为 active 版本，评测失败则保留当前版本并记录拒绝历史。

运行 `run_evolution_showcase()` 可重复演示一次发布和一次拒绝路径，结果包含版本历史，便于后续接入 Nexent 或整理赛题材料。

## 项目目标

外部知识引用可通过 `external_items_to_tool_result` 转成现有证据信封后再进入决策校验；`equipment_decision_agent` 的四段式报告则通过 `parse_business_report` 独立解析，不与 JSON 决策契约混用。

- 用通用 Schema 表达决策请求、证据、选项、风险、审批和反馈。
- 用状态图串联“证据 → 决策 → 校验 → 审批 → 执行 → 反馈 → 进化”。
- 验证有限重试、证据一致性校验、结构化失败和 JSON 状态序列化。
- 验证候选策略经过评测后才能发布，失败时保留旧版本并支持回滚。

## 架构边界

| 组件 | 职责 | 当前边界 |
| --- | --- | --- |
| LangGraph | 唯一流程编排器、状态流和终止路由 | 不引入第二套 Agent Loop |
| Pydantic AI | 生成类型化决策输出 | 当前使用无凭据的 `TestModel` |
| 确定性 Python 规则 | Schema、证据校验、审批门禁、版本发布和回滚 | 不绑定行业规则 |
| LangChain Core | 同步/异步 `Runnable` 演示和评测适配 | 不负责流程编排 |
| httpx 传输层 | 调用 Nexent 北向 `/nb/v1/chat/run`、聚合 SSE、映射错误 | 不做编排，不影响离线路径 |
| Nexent SDK 传输层（可选） | 无平台环境时内嵌运行 Nexent Agent | 需手动装 `nexent`，不验证北向契约 |

## 当前范围

已完成：

- 通用决策 Schema 与 JSON 兼容状态；
- 模拟证据工具、模拟动作工具和模拟审批；
- 可重试/不可重试错误与有限重试；
- 关联 ID、证据来源、版本和内容一致性校验；
- 缺少审批或审批拒绝时阻断执行；
- 反馈记录、候选策略、评测门禁、发布和回滚；
- LangChain 同步/异步适配；
- Nexent 北向传输层（SSE 聚合、幂等键、401/429/冲突错误映射，默认不联网）；
- Nexent SDK 传输层（无平台环境时的内嵌路径，可选依赖）；
- 北向链路真机验证（本机 Nexent v2.7.0，SSE → 结构化决策）；
- 城市内涝主链路 Nexent 真机集成：MCP-over-SSE（12 工具）+ Agent 发布 + 8/8 结论一致性校验（conversation_id=21）；
- 多模态数据资产标识（表格 / 文本 / 图片统一登记、结构摘要、标准引用、可引用定位）；
- 本体半自动构建（种子本体 → 规则抽取 → 置信度分档 + 跨文档多跳证据 + 合并计划）；
- 城市内涝数字孪生语义层 + 六跳跨部门推演 + 三类职责冲突 + 反事实重放（P4–P6）；
- 五个 Skill 编排与 12 个 MCP 工具（9 个决策语义 + 3 个可视化，含权限收口、trace_id、失败语义、工具分组 allowlist）（P6/P9/P10）；
- P8 评测：22 个事件用例、七项指标口径与改进前后对比；
- P9 决策层：推演输出片区决策输入、稀缺资源分配策略、偏差归因 + 评测门禁 + 回滚的受控进化闭环；
- P9.1 持续进化：分层决策记忆（离线同构）+ 人工评审第二反馈通道 + 语义化策略版本标签；
- P9.2 浏览器进化对比面板：`/evolution` 一屏展示双会话、评审、版本标签与真实事件分配；
- P10 可视化工具层：决策快照（`snapshot_id` 内容哈希）→ UE 渲染作业 → 引擎 ack 的异步闭环，
  3 个 MCP 工具（`scene_render` / `scene_compare` / `asset_lookup`）+ 工具分组 allowlist，
  让"智能体决定调策略 → 自动调可视化工具 → 场景呈现变化"可核对而不是人工切镜头；
- P10.1 实体重投影与绑定表：把决策层 `TW-*` 投影成场景实体（`twin_id ↔ 布局几何 ↔ Actor tag`），
  回放高亮的 19 个对象里 16 个可画、3 个无几何显式记入 `unrepresented`（不静默丢），
  同一真源逐位一致、`projection_hash` 可当场核对；
- 平台技能仓库：2 个 `SKILL.md` 上传到 Nexent 技能仓库（`skill_id=2/3`，正文逐位一致，幂等脚本 `deploy/nexent/upload_skills.py`）；
- 平台界面取证：`deploy/nexent/capture_platform_ui.py` 一命令出 3 张可核对截图（技能仓库 / 智能体 / MCP 连通性校验），
  每页"必须出现的文字"写成断言，图被换过 `evidence_check` 立刻失败；
- 对外数字自检：一条命令核对 106 项宣称证据（含漂移检测，改坏即退出码 1）；
- P11 消融阶梯：五档配置 × 22 用例的同口径指标、leave-one-out（逐例剔除）稳健性与**精确符号翻转检验**（2²² 全枚举、零随机、无种子）（`decision_poc.ablation`，报告 `docs/reports/p11-ablation-report.md`）；
- P11 决策锚定包：`H(o),H(Z),H(a*),meta` 哈希链提交 + 链下原文复核，改一个字节即检出（`decision_poc.anchoring`，表述仍是哈希链审计日志，不宣称区块链）；
- 技术基线与风险记录。

暂不做：

- 固定行业业务流程和主题专属逻辑；
- 生产环境 Nexent / 数据库 / 生产工具接入（当前只在本地 Docker 演示环境做真机验证）；
- 完整业务 UI；
- 最终行业评价指标。

## 快速开始

要求：Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --locked
uv run --locked pytest -q
```

一条命令生成并核对全部对外材料（测试、数据集、布局、推演、反事实、Skill 编排、P8 评测、MCP 契约、P9 进化、P11 消融与锚定、106 项自检）：

```powershell
uv run --locked python scripts/reproduce.py            # 全量，约 30 秒（本机实测 17/17 步通过）
uv run --locked python scripts/reproduce.py --fast     # 跳过 pytest 与 P9 进化，只核对数字
uv run --locked python scripts/reproduce.py --list     # 只看会跑哪些步骤
```

期望末行 `结果：17/17 步通过`；明细写 `artifacts/reproduce/summary.json`（已被 .gitignore 忽略）。
脚本只比对、不改数字：任一期望不成立即退出码 1，并把失败步骤的原始输出打出来。

一条命令核对全部对外宣称数字（六跳、P8 评测、P9 双会话进化、MCP 工具数；任一项漂移即失败）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.evidence_check
# 期望：对外数字自检：106/106 项一致（期望指纹 df6e48a10232d54f）
```

期望值冻结在 `samples/eval/evidence_expectations.json`；改数字必须改期望文件并同步 README/报告/答辩口径，
不允许为过检放宽期望。报告见 [对外数字自检报告](docs/reports/evidence-check-report.md)。

运行技术基线：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -c "from decision_poc.baseline import run_technical_baseline; print(run_technical_baseline())"
```

生成资产清单与本体候选（都不联网）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.asset_manifest       # -> samples/assets/manifest.json
uv run --locked python -m decision_poc.ontology_bootstrap   # -> samples/assets/ontology_candidates.json
```

导入城市内涝最小数据集（15 个合成资产，含治理字段与版本有效期）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.urban_dataset        # -> samples/assets/urban/manifest.json
uv run --locked python -m decision_poc.health --dataset samples/assets/urban
```

预期 `assets=15 types=12 modalities={'image': 1, 'table': 8, 'text': 6}`；健康检查全绿。
数据集说明见 [samples/assets/urban/README.md](samples/assets/urban/README.md)，
推演标准答案见 [核心事件 Ground Truth](docs/urban-scenario-ground-truth.md)。

跑六跳跨部门推演（规则来自 `samples/rules/`，每条带原文行号并在加载时校验）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00
uv run --locked python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --record artifacts/ledger
uv run --locked python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --json
```

预期：三类冲突 C1/C2/C3、资源缺口 2 台、`隔离风险源` 序位在 `抽排` 之前；`--record` 把结论写成 claim 落进溯源账本并打印 `verify=True`。

跑五个 Skill 的编排（事件理解 → 影响分析 → 资源检查 → 跨部门推演 → 证据审计）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --caller-labels ORG-EMERG
uv run --locked python -m decision_poc.skills --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00 --caller-labels ORG-ROAD   # 越权身份：推演被拒
uv run --locked python -m decision_poc.skills --manifest > samples/nexent/skills.json                                          # 重新生成绑定表
```

预期：末行打印 `claim 14/14 通过  引用完整率 1.00`；换 `ORG-ROAD` 后状态变 `partial`、可发布为 `否`，终止原因写明依据不可见。
Nexent 侧只需消费 `samples/nexent/skills.json`（顺序、请求 Schema、工具绑定、失败语义），说明见 [samples/nexent/README.md](samples/nexent/README.md)。

跑反事实（CF1–CF3：改一个输入后重放同一套规则，取与基线的差异）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.counterfactual --case all     # CF1 / CF2 / CF3
uv run --locked python -m decision_poc.counterfactual --case CF2 --json
```

预期：CF1 封路使 J-01↔J-02 从 820 m/1.2 分钟变为 3860 m/6.3 分钟（绕行经 R-102/R-103/R-104）；CF2 库存 6→2 台、缺口 2→6 台、A-03 本片区需 6 台（缺 3）、B-01 需 2 台（缺 2），优先保障 A-03；CF3 开挖面暴露等级 orange→red、新增二次风险；越权身份退出码 2 且不返回空差异。

跑 MCP Server（stdio，JSON-RPC 2.0，不依赖任何 MCP SDK）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_server --list-tools     # 打印 12 个工具的参数结构
uv run --locked python -m decision_poc.mcp_server                  # 进入 stdio 服务循环
```

一行请求做自检（Windows 下用单引号包 JSON，管道进进程）：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | uv run --locked python -m decision_poc.mcp_server
```

十二个工具（9 个决策语义 + 3 个可视化）：`semantic_query`、`twin_neighbors`、`evidence_locate`、`claim_trace`、`provenance_verify`、
`event_deduce`、`counterfactual_run`、`conflict_check`、`allocation_plan`、`scene_render`、`scene_compare`、`asset_lookup`。
全部返回既有 `ToolResult` 信封；失败返回 `status="error"` 且不返回伪造数据。
可视化三个工具做工具分组隔离（`--groups decision|presentation|bootstrap`），推演循环里看不见渲染工具。
`scene_render` 只接受快照 id 与策略参数（不接受位姿/材质/镜头），作业落盘即返回——引擎没起只体现为 `delivery=pending`，不算失败。
推演、冲突与分配工具需要 `caller_labels`：结论引用了调用方不可见的依据时返回 `PERMISSION_DENIED`，而不是删掉依据照给结论。
调用日志用 `--log artifacts/mcp_calls.jsonl` 落盘（缺省写 `artifacts/visual/tool_log.jsonl`，与 Web 端工具日志面板同一份），
`trace_id` 随请求透传到 `context.trace_id` 并写进日志；每条带 `origin`（`mcp` / `manual`），演示时能分辨是谁触发的渲染。

测试使用 Pydantic AI 内置 `TestModel`，无需 API Key，不会连接真实模型、调用 Nexent 或执行生产动作。
Nexent 传输层单测全部使用 `httpx.MockTransport` 回放 SSE，同样不触网。

### 可选：真实语言模型（DeepSeek，默认关闭）

默认的测试与演示都走 `TestModel`，不联网。要用真实模型跑同一个类型化决策节点：

```powershell
uv sync --group llm                          # 可选依赖（openai SDK）；默认 locked sync 不装
$env:DECISION_LLM_PROVIDER = "deepseek"
$env:DEEPSEEK_API_KEY      = "<你的 key>"    # 只放环境变量，不要写进仓库
$env:DEEPSEEK_MODEL        = "deepseek-chat" # 可选：deepseek-reasoner / deepseek-v4-flash / deepseek-v4-pro
uv run --group llm python scripts/deepseek_smoke.py
```

未配置 provider 时脚本打印"跳过"并返回 0；缺 key 或缺依赖会给出明确错误且**不发请求**；
`.gitignore` 已排除 `.env` / `.env.*`，密钥不会入库。真实模型只替换 `DecisionOutput` 生成节点，
六跳推演仍由确定性规则引擎产生，不参与断言生成。设计、失败语义与安全约束见
[真实模型接入设计](docs/real-model-provider-design.md)。

加 `--out samples/eval/deepseek_smoke.json` 可把这次真实模型输出固化为证据（不含凭据）：

```powershell
uv run --group llm python scripts/deepseek_smoke.py --out samples/eval/deepseek_smoke.json
```

A/B 评测（在 P8 的 22 个事件用例上对比 rule / TestModel 地板 / 真实模型，报告见
[P8 之上的真实模型 A/B 报告](docs/reports/p8-llm-ab-report.md)）：

```powershell
uv run --locked python scripts/llm_ab_eval.py --provider rule --provider test  # 离线
$env:DECISION_LLM_PROVIDER = "deepseek"
uv run --group llm python scripts/llm_ab_eval.py --provider rule --provider test --provider deepseek
```

真机联调（可选，需自行提供凭据）：

```powershell
$env:NEXENT_BASE_URL   = "http://<nexent-host>:5013"
$env:NEXENT_API_KEY    = "<发布 Agent 后生成的 Access Key>"
$env:NEXENT_AGENT_NAME = "workbench_main"
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.nexent_smoke
```

缺凭据时脚本直接列出缺失项并返回退出码 2，不会发请求。

本机实测（Nexent v2.7.0）用 `workbench_main` 一次即返回可校验的 `DecisionOutput`；
`equipment_decision_agent` 的提示词强制「检索→推理→结论」四段中文报告，与 JSON 契约冲突，
因此对外统一走 `workbench_main`，业务报告仍由该 Agent 自己承担。原因见
[设计文档 8.2 节](docs/nexent-northbound-transport-design.md)。

没有可用的 Nexent 平台部署时（例如只有一台 ARM64 沙箱容器、起不了 Docker 全栈），改走 SDK 内嵌：

```powershell
uv pip install "nexent==1.8.1"   # 可选依赖，不入 pyproject
$env:PYTHONPATH = "src"
uv run --locked python -c "from decision_poc.nexent_sdk_transport import NexentSdkTransport; from decision_poc.nexent_adapter import NexentAdapter; from decision_poc.contracts import DecisionRequest; t = NexentSdkTransport(model_id='<模型名>', api_key='<模型 Key>', api_base='<OpenAI 兼容地址>'); print(NexentAdapter(invoke=t).run(DecisionRequest(request_id='req-sdk', goal='choose', context={}, constraints=['safe'])))"
```

这条路径用真实模型和真实 Nexent Agent 运行时，但不验证 `/nb/v1` 北向契约；两者取舍见
[北向传输层设计](docs/nexent-northbound-transport-design.md) 第 6 节。

## Nexent 真机接入（已验证：8/8）

赛题 3 的「基于 Nexent 可运行」不是"能连上"，而是"平台 Agent 调工具产出的结论与离线一致"。当前已在本机
Nexent **v2.7.0**（openPangu-2.0-Pro）跑通：

- MCP 服务 `urban-twin-mcp`（12 工具）以 SSE 方式跑在 `nexent_network` 容器里；
- Agent `urban_deduction_agent`（agent_id=3，已发布 v3）挂载全部 12 个工具；
- 一次真实对话（**conversation_id=21**）产出六跳 JSON，与离线 `DeductionEngine` **8/8 项一致**；
- 平台侧确实发生了工具调用：`event_deduce`（29ms）、`allocation_plan`（28ms）、`scene_render`（88ms），
  三条日志快照哈希一致。

```powershell
# 1) 构建并启动 MCP 容器（详细步骤见 deploy/nexent/README.md）
docker build -t urban-twin-mcp:dev -f deploy/mcp_sse/Dockerfile .
docker run -d --name urban-twin-mcp --network nexent_network --restart unless-stopped urban-twin-mcp:dev

# 2) 平台侧装配（注册 MCP → 同步工具 → 创建 Agent → 发布），在 nexent-config 容器内执行
docker cp deploy/nexent/setup_nexent.py nexent-config:/tmp/setup_nexent.py
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/setup_nexent.py all"

# 3) 结论级验证（宿主机）
$env:NEXENT_API_KEY = "<上一步生成的 Key>"
$env:NEXENT_AGENT_NAME = "urban_deduction_agent"
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.nexent_urban --json samples/eval/nexent_urban_smoke.json
# 期望：Nexent 城市内涝主链路：8/8 项一致；conversation_id=<n>
```

证据固化：`python deploy/nexent/capture_evidence.py` → `samples/nexent/live/`（Agent 列表、会话、MCP 调用日志、
汇总 JSON）。完整排障与边界见 [真机集成报告](docs/reports/nexent-live-integration-report.md)。

边界：只在小规模本机 Docker 演示环境验证；两个 SKILL.md 已上传平台技能仓库（skill_id=2/3）但未绑定到已发布 Agent；
平台界面截图 3 张已固化（`docs/reports/captures/nexent/`，含 sha256 自检），问答过程录屏与多事件真机回归仍待补；
不宣称"多 Agent 协同"。

## 决策进化闭环（P9 / P9.1）

六跳推演只回答「缺多少台」，不回答「这几台先给谁」。P9 把「分配」做成一层**有版本、可评测、可回滚的策略**，
并把它接进受控进化循环：偏差归因 → 候选策略 → 评测门禁（训练集提升 + 留出集不退化 + 不违反人工评审）→
采纳/拒绝 → 回滚验证。

P9.1 再把「一次演示」做成「持续机制」，三个新增证据：

- **分层记忆 warm start**：同类场景第二次遇到，从已验证有效的策略起步（离线同构契约：Tenant/User/Agent 三层）；
- **人工评审第二信号**：评审给出的修正转成评审用例进门禁，候选违反即拒（演示为角色扮演，非真实批复）；
- **语义化版本标签**：每次采纳产出 `flood-response-v1.N`，带触发反馈 ID、触发评审 ID 与预期改善，可回滚。

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.policy_evolution --report docs/reports/p9-policy-evolution-report.md
```

预期（第一次运行从零进化，第二次运行从记忆 warm start）：

```text
第一次运行：训练偏差 0.2264 → 0.0000；留出 0.2182 → 0.0000；标签 flood-response-v1.2
轮次 3（采纳 2 / 拒绝 1）；关键片区未满足 15 → 6 台；应调拨未调拨 4 → 0
人工评审 REV-001（水务调度负责人（角色扮演））：评审偏差 0.1171 → 0.0000
第二次运行（记忆 M-3979c3901de5，相似度 1.0000）：起点偏差 0.0000，轮次 1
真实事件：v1 {"A-03": 4, "B-01": 2} → {"A-03": 5, "B-01": 1}
```

关键片区未满足 `15 → 6` 的 6 是 ground truth 自身也解决不了、必须上报增援的下限；真实事件的
`A-03 6 台 / B-01 0 台` 对应 40 分钟跨区时延（台账字段，不是叙事）。记忆落盘在
`samples/eval/decision_memory.json`，加 `--reuse-memory` 可演示真正跨进程沉淀。

反馈信号来自 `samples/eval/allocation_cases.json`（8 训练 + 4 留出，逐例标注依据条款）的**冻结 ground truth**
与 `samples/eval/decision_reviews.json` 的**人工评审结论**，不是真实市政数据回流；报告口径统一写作
「受控评测环境下的反馈信号」。分层记忆为**离线同构实现，未在真实 Nexent 平台真机验证**。
策略本身可由 Nexent 通过 MCP 工具 `allocation_plan` 调用（`policy` 参数可传进化后的版本），
见 [P9 设计规格](docs/superpowers/specs/2026-10-30-p9-decision-evolution-design.md) 与
[P9 报告](docs/reports/p9-policy-evolution-report.md)。

## 真实用户评分采集（第二反馈信号的真实版）

P9.1 的评审是角色扮演；真实用户评分另走一条可采集通道：`/evolution` 页面上的表单，
或 CLI `python -m decision_poc.review_collect add`。四维 1–5 分只做汇总（样本/均分/前后差值），
**不参与门禁**；只有同时给出"建议分配"的评分才会转成评审用例进入 P9 门禁（与角色扮演评审同一条路）。

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.review_collect add --reviewer u1 --phase before --scores 4,3,5,4
uv run --locked python -m decision_poc.review_collect add --reviewer u2 --phase after --scores 5,4,5,5 --suggested "A-03=5,B-01=1"
uv run --locked python -m decision_poc.review_collect summary --markdown
uv run --locked python -m decision_poc.review_collect to-reviews   # 导出给 P9 门禁消费
```

在 `/evolution` 页面上提交「进化后」评分时，后端会自动重跑一次进化门禁，把候选采纳/拒绝、训练与留出偏差前后值回显在同一屏。

记录默认落 `samples/eval/review_submissions.jsonl`（追加式、幂等、默认不入 git，可用
`URBAN_TWIN_REVIEWS` 改路径）；口径固定写「真实用户评分（个人意见，非部门批复）」。
详见 [真实用户评分采集](docs/real-feedback-channel.md)。

## 多模态：图像语义抽取（像素级，无视觉模型）

`decision_poc.image_semantics` 把 PNG 资产从"只登记尺寸"变成**可复算的像素事实**：
检测坐标轴 → 抽取响应曲线峰值频率/相对幅值（频谱图）；按颜色聚类出色块 bbox 与走向、
识别浅蓝积水范围（管线示意图）。输出可直接转成 `RegionSpec` 区域候选，被现有溯源链引用。

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.image_semantics --image samples/assets/vibration_spectrum.png
# 期望：kind=spectrum，峰值 [120.5, 241.0, 360.3, 480.8] Hz（生成器 ground truth 120/240/360/480）
uv run --locked python -m decision_poc.image_semantics --image samples/assets/urban/pipeline_map.png --regions artifacts/image-regions.json
# 期望：kind=schematic，6 个色块（排涝横线/燃气横线/电力竖线/通信竖线/给水竖线）+ 积水范围 bbox
```

口径：**不是 OCR、不是视觉模型**；频率轴范围来自生成器契约，图例文字含义需调用方映射。
详见 [图像语义抽取](docs/image-semantics.md)。

## 评测（P8）

22 个事件用例覆盖正常、冲突、证据不足、过期数据、越权五类形态，逐例声明可核查的期望
（状态、六跳、冲突编号、缺口、必须出现的对象、必须能定位的证据、引用完整率、拒答理由）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.urban_eval                                      # 只打印汇总，退出码 0 表示全过
uv run --locked python -m decision_poc.urban_eval --json samples/eval/latest.json \
    --report docs/reports/p8-evaluation-report.md \
    --baseline samples/eval/baseline_before_temporal_guard.json                        # 含改进前后对比
```

预期：`22/22 通过`；资产解析 16/16、未识别列 0、检索命中 100%、六跳完整 100%、引用完整率 100%、
不可支撑结论 0，单用例 p50 约 10 ms 以内。改进前基线（`samples/eval/baseline_before_temporal_guard.json`）
是 17/22——差的 5 条全部是「as_of 早于上报时间 / 工单已闭环」被当成正常事件推演掉的，
生成方式见 `samples/eval/make_pre_guard_baseline.py` 顶部注释。

## 仓库结构

```text
agent_poc/
├─ src/decision_poc/       # 契约、治理、认知、语义层、推演、溯源、MCP、Skills
├─ tests/                  # 契约、工具、流程、评测、基线测试
├─ samples/assets/urban/   # 15 个合成资产（表格/文档/示意图）
├─ samples/rules/          # 推演规则（每条带原文行号）
├─ samples/nexent/         # Nexent 侧绑定表（由代码生成，不手改）
├─ samples/eval/           # P8 评测用例 + P9 分配用例/人工评审/分层记忆/进化快照
├─ docs/                   # 文档导航、技术基线、回传报告
├─ pyproject.toml          # Python 与依赖约束
├─ uv.lock                 # 锁定后的完整依赖图
├─ AGENTS.md               # 项目级协作与验证约束
└─ README.md               # 项目入口说明
```

## 文档入口

- [文档导航](docs/README.md)
- [赛题方案（三能力与证据口径）](docs/innovation-and-evidence.md)
- [核心事件 Ground Truth](docs/urban-scenario-ground-truth.md)
- [技术基线与风险](docs/technical-baseline.md)
- [Nexent 集成边界](docs/nexent-integration-boundary.md)
- [Nexent 北向传输层设计](docs/nexent-northbound-transport-design.md)
- [城市内涝主链路 Nexent 集成与真机报告](docs/reports/nexent-live-integration-report.md)（8/8 一致性，conversation_id=21）
- [Nexent 集成设计（SSE + SKILL.md）](docs/nexent-urban-integration-design.md)
- [平台侧证据报告（技能上传 + 界面截图）](docs/reports/platform-evidence-report.md)
- [Nexent 侧装配与复现手册](deploy/nexent/README.md)
- [赛题 3 差距审计](docs/competition-gap-audit.md)
- [演示运行手册](docs/demo-runbook.md)
- [数据资产标识规范](docs/data-asset-spec.md)
- [P8 评测报告](docs/reports/p8-evaluation-report.md)（由评测命令生成，勿手改）
- [P9 进化闭环报告](docs/reports/p9-policy-evolution-report.md)（由进化命令生成，勿手改）
- [对外数字自检报告](docs/reports/evidence-check-report.md)（由自检命令生成，勿手改）
- [P9 决策进化闭环设计](docs/superpowers/specs/2026-10-30-p9-decision-evolution-design.md)
- [本体半自动构建方案](docs/ontology-bootstrap.md)
- [通用内核设计](docs/superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)
- [实施计划](docs/superpowers/plans/2026-10-26-generic-evolution-kernel.md)

## 验证状态

当前基线：`uv run --locked pytest -q` 共 **734 项**测试通过；`python -m decision_poc.evidence_check` 106/106 项一致；
城市内涝主链路 Nexent 真机校验 8/8（`python -m decision_poc.nexent_urban`）；
同时要求锁文件、Python 编译和依赖一致性检查通过。
P0-P10 的验收口径与实测数字见各阶段回传报告（`docs/reports/`）。

## 后续演进

赛题主题确定后，仅替换领域 Schema、规则、工具和评测集；LangGraph 状态流、Pydantic AI 类型化输出、审批门禁、评测发布和回滚机制继续复用。真实 Nexent 接入应作为独立适配层，不直接破坏当前离线 PoC。

P9 的进化循环目前跑在**冻结的合成 ground truth + 人工评审结论** 上（受控、可重复、可回滚）。接真实数据时只替换反馈 provider：
把工单复盘/处置结果评测成同口径的 `AllocationCase`，门禁、偏差归因、版本与回滚逻辑不变；
在此之前不宣称"真实市政数据回流"。分层记忆同理：现为离线同构契约，接 Nexent 时换成平台记忆 API，**未真机验证前不宣称已接入**。

## 城市内涝 Web Demo 与三维场景桥接

启动无额外依赖的浏览器 Demo：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.web_demo --port 8787
# 推演面板   http://127.0.0.1:8787
# 进化对比面板 http://127.0.0.1:8787/evolution   （数据接口 /api/evolution）
```

进化对比面板把 P9.1 的双会话证据放在同一屏：第一会话进化前/后（训练 0.2264→0.0000、留出 0.2182→0.0000）、
第二会话记忆 warm start（起点偏差 0.0000、轮次 3→1）、人工评审（0.1171→0.0000）、版本标签
`flood-response-v1.0→v1.1→v1.2`、真实事件分配（`A-03 4→5 台 / B-01 2→1 台`）。
页面数据直接取 `run_showcase()`，展示层不重算任何指标；页脚固定印三条表述边界（受控反馈 / 评审角色扮演 / 记忆离线同构）。

生成可供 Blender/Unreal 导入的引擎无关场景数据：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.scene_export --out samples/assets/urban/scene.json
```

场景文件只负责稳定的 `twin_id`、类型、状态、`geometry_ref` 和溯源信息；Blender/Unreal 的模型资产和坐标解析属于展示层，不参与推理结论生成。
