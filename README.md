# ICT：可进化决策智能体 PoC

这是一个面向华为 ModelEngine Nexent 赛题方向的通用、离线、非生产决策智能体内核验证项目。当前不绑定具体行业，不接入真实 Nexent、真实模型或生产系统，先验证可复用的决策流程与受控进化机制。

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
- 多模态数据资产标识（表格 / 文本 / 图片统一登记、结构摘要、标准引用、可引用定位）；
- 本体半自动构建（种子本体 → 规则抽取 → 置信度分档 + 跨文档多跳证据 + 合并计划）；
- 技术基线与风险记录。

暂不做：

- 固定行业业务流程和主题专属逻辑；
- 真实 Nexent、真实模型、数据库或生产工具接入；
- 完整业务 UI；
- 最终行业评价指标。

## 快速开始

要求：Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --locked
uv run --locked pytest -q
```

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

预期：三类冲突 C1/C2/C3、资源缺口 1 台、`隔离风险源` 序位在 `抽排` 之前；`--record` 把结论写成 claim 落进溯源账本并打印 `verify=True`。

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

预期：CF2 库存 6→2 台、缺口 1→5 台、优先保障 A-03；CF3 **无差异**（规则不含工期字段，该问题当前不可判定）；越权身份退出码 2 且不返回空差异。

跑 MCP Server（stdio，JSON-RPC 2.0，不依赖任何 MCP SDK）：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.mcp_server --list-tools     # 打印 8 个工具的参数结构
uv run --locked python -m decision_poc.mcp_server                  # 进入 stdio 服务循环
```

一行请求做自检（Windows 下用单引号包 JSON，管道进进程）：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | uv run --locked python -m decision_poc.mcp_server
```

八个工具：`semantic_query`、`twin_neighbors`、`evidence_locate`、`claim_trace`、`provenance_verify`、
`event_deduce`、`counterfactual_run`、`conflict_check`。全部返回既有 `ToolResult` 信封；失败返回 `status="error"` 且不返回伪造数据。
推演与冲突工具需要 `caller_labels`：结论引用了调用方不可见的依据时返回 `PERMISSION_DENIED`，而不是删掉依据照给结论。
调用日志用 `--log artifacts/mcp_calls.jsonl` 落盘，`trace_id` 随请求透传到 `context.trace_id` 并写进日志。

测试使用 Pydantic AI 内置 `TestModel`，无需 API Key，不会连接真实模型、调用 Nexent 或执行生产动作。
Nexent 传输层单测全部使用 `httpx.MockTransport` 回放 SSE，同样不触网。

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

## 仓库结构

```text
agent_poc/
├─ src/decision_poc/       # 契约、治理、认知、语义层、推演、溯源、MCP、Skills
├─ tests/                  # 契约、工具、流程、评测、基线测试
├─ samples/assets/urban/   # 15 个合成资产（表格/文档/示意图）
├─ samples/rules/          # 推演规则（每条带原文行号）
├─ samples/nexent/         # Nexent 侧绑定表（由代码生成，不手改）
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
- [演示运行手册](docs/demo-runbook.md)
- [数据资产标识规范](docs/data-asset-spec.md)
- [本体半自动构建方案](docs/ontology-bootstrap.md)
- [通用内核设计](docs/superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)
- [实施计划](docs/superpowers/plans/2026-10-26-generic-evolution-kernel.md)

## 验证状态

当前基线：`uv run --locked pytest -q` 共 **338 项**测试通过；同时要求锁文件、Python 编译和依赖一致性检查通过。
P0-P6 的验收口径与实测数字见各阶段回传报告（`docs/reports/`）。

## 后续演进

赛题主题确定后，仅替换领域 Schema、规则、工具和评测集；LangGraph 状态流、Pydantic AI 类型化输出、审批门禁、评测发布和回滚机制继续复用。真实 Nexent 接入应作为独立适配层，不直接破坏当前离线 PoC。
