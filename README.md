# Agent Framework PoC

这是一个通用、离线、非生产用途的可进化决策内核验证：

- **LangGraph**：唯一的决策流程编排器。
- **Pydantic AI**：图中的结构化决策节点，结果由 Pydantic 模型校验。
- **LangChain Core**：将图包装为 `RunnableLambda`，供演示或评测调用；不承担第二套编排。
- **确定性规则与版本注册表**：负责证据一致性、审批门禁、反馈候选版本、评测发布和回滚。

## 环境

- Python 3.12
- LangGraph 1.2.17（MIT）
- Pydantic AI Slim 2.61.0（MIT）
- LangChain Core 1.7.0（MIT）
- Pydantic 2.14.1（MIT）
- pytest 9.1.1（MIT）

版本已在 `pyproject.toml` 中固定，解析锁定后由 `uv.lock` 记录完整依赖图。
`pydantic-ai-slim` 只引入本 PoC 所需核心能力，避免安装未使用的模型提供商和工具集。

## 运行

```powershell
uv sync --locked
uv run pytest -q
```

测试使用 Pydantic AI 内置 `TestModel`，无需 API Key；不会连接真实模型、执行生产动作或调用 Nexent 服务。

## 当前验证范围

已覆盖：通用 Schema、结构化工具信封、有限重试、证据来源/版本校验、LangGraph 节点顺序、模拟审批、模拟执行、反馈候选版本、评测门禁、版本回滚、LangChain Runnable 调用，以及整个图状态的 JSON 序列化。

## 通用决策流程

```text
请求 → 模拟证据 → 结构化决策 → 证据/风险校验 → 模拟审批
     → 模拟执行 → 反馈 → 候选策略版本 → 评测通过后发布 / 失败保留旧版本
```

技术基线可由 `decision_poc.evaluation.evaluate_cases` 记录：完成数、阻断数、错误数、步骤数、重试次数、序列化结果和耗时；它不替代最终行业评价指标。
固定的通用基线入口和风险清单见 [`docs/technical-baseline.md`](docs/technical-baseline.md)。

未覆盖：真实 LLM 推理质量、真实工具副作用、人工审批持久化、LangGraph 检查点恢复、真实 Nexent 接入和生产级错误恢复。策略候选、评测门禁和回滚已在内存注册表中验证，但尚未接入持久化存储。该 PoC 不能作为生产实现或比赛完整方案。
