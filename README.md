# Agent Framework PoC

这是一个一次性、离线、非生产用途的集成验证：

- **LangGraph**：唯一的决策流程编排器。
- **Pydantic AI**：图中的结构化决策节点，结果由 Pydantic 模型校验。
- **LangChain Core**：将图包装为 `RunnableLambda`，供演示或评测调用；不承担第二套编排。

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

测试使用 Pydantic AI 内置 `TestModel`，无需 API Key；不会连接真实模型、执行运维动作或调用 Nexent 服务。

## 当前验证范围

已覆盖：结构化决策输出、LangGraph 节点顺序、决策与请求 ID 一致性校验、LangChain Runnable 调用，以及整个图状态的 JSON 序列化。

未覆盖：真实 LLM 推理质量、真实工具副作用、人工审批持久化、检查点恢复、模型/策略进化、Nexent 接入和生产级错误恢复。该 PoC 不能作为生产实现或比赛完整方案。
