# ICT：可进化决策智能体 PoC

这是一个面向华为 ModelEngine Nexent 赛题方向的通用、离线、非生产决策智能体内核验证项目。当前不绑定具体行业，不接入真实 Nexent、真实模型或生产系统，先验证可复用的决策流程与受控进化机制。

当前提供 `NexentAdapter` 作为离线适配边界：它只把 `DecisionRequest` 转成可传输的 JSON 负载，并调用注入的传输函数；默认不会联网。真实 Nexent 接入应在独立传输层中实现，并继续保留现有审批、评测和回滚门禁。

## 项目目标

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

## 当前范围

已完成：

- 通用决策 Schema 与 JSON 兼容状态；
- 模拟证据工具、模拟动作工具和模拟审批；
- 可重试/不可重试错误与有限重试；
- 关联 ID、证据来源、版本和内容一致性校验；
- 缺少审批或审批拒绝时阻断执行；
- 反馈记录、候选策略、评测门禁、发布和回滚；
- LangChain 同步/异步适配；
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

测试使用 Pydantic AI 内置 `TestModel`，无需 API Key，不会连接真实模型、调用 Nexent 或执行生产动作。

## 仓库结构

```text
agent_poc/
├─ src/decision_poc/       # 决策契约、图编排、工具、评测和版本注册表
├─ tests/                  # 契约、工具、流程、评测、基线测试
├─ docs/                   # 文档导航、技术基线、设计和实施计划
├─ pyproject.toml          # Python 与依赖约束
├─ uv.lock                 # 锁定后的完整依赖图
├─ AGENTS.md               # 项目级协作与验证约束
└─ README.md               # 项目入口说明
```

## 文档入口

- [文档导航](docs/README.md)
- [技术基线与风险](docs/technical-baseline.md)
- [通用内核设计](docs/superpowers/specs/2026-10-26-generic-evolution-kernel-design.md)
- [实施计划](docs/superpowers/plans/2026-10-26-generic-evolution-kernel.md)

## 验证状态

当前基线：`uv run --locked pytest -q` 共 28 项测试通过；同时要求锁文件、Python 编译和依赖一致性检查通过。技术基线只衡量通用流程的技术行为，不替代赛题确定后的行业指标。

## 后续演进

赛题主题确定后，仅替换领域 Schema、规则、工具和评测集；LangGraph 状态流、Pydantic AI 类型化输出、审批门禁、评测发布和回滚机制继续复用。真实 Nexent 接入应作为独立适配层，不直接破坏当前离线 PoC。
