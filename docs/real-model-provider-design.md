# 真实语言模型可选接入设计（DeepSeek）

## 1. 目标与非目标

**目标**：让 `decision_poc.agent` 的类型化决策节点在**显式配置**下调用真实 LLM
（DeepSeek，OpenAI 兼容接口），用于质量、时延和失败模式的实测与演示。

**非目标**：

- 不改变默认离线路径：不配置环境变量时仍是 Pydantic AI `TestModel`，不读凭据、不联网，
  `pytest` 与 `scripts/reproduce.py` 的可复现结论不变；
- 不把真实模型接进 LangGraph 主流程（`graph.py` 仍只调用确定性 `TestModel` 路径），
  避免"一次演示依赖外部模型可用性"；
- 不替代 Nexent 北向通道（那是平台侧 Agent 运行时，本设计只解决本地类型化节点）；
- 不引入向量库、数据库或新的推理编排。

## 2. 架构

```
graph.py ──generate_decision_async──> agent._build_agent().run(model=TestModel)   # 默认，离线
scripts/deepseek_smoke.py ──generate_decision_live──> model_provider.build_model
                                                        └─ pydantic_ai.models.openai.OpenAIChatModel
                                                           + pydantic_ai.providers.deepseek.DeepSeekProvider
```

- 复用 Pydantic AI 官方 `DeepSeekProvider`（`https://api.deepseek.com`）与
  `OpenAIChatModel`，不自写 HTTP 客户端、不维护第二套 wire 格式；
- `model_provider.py` 只做三件事：读环境、校验配置、构造模型；不含 prompt 与业务逻辑；
- 决策输出仍由 `DecisionOutput` Schema 约束；真实模型只影响这一节点的生成质量，
  **不参与六跳推演**（推演由确定性规则引擎产生，见 `docs/innovation-and-evidence.md`）。

## 3. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DECISION_LLM_PROVIDER` | `test` | `test`（离线 `TestModel`）/ `deepseek` |
| `DEEPSEEK_API_KEY` | 无 | `provider=deepseek` 时必填；只从环境变量读取 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 支持 `deepseek-chat`、`deepseek-reasoner`、`deepseek-v4-flash`、`deepseek-v4-pro` |

依赖：真实模型需要可选依赖 `openai`（`uv sync --group llm`），默认 `uv sync --locked`
不安装它，离线环境不受影响。

## 4. 失败语义

| 情况 | 行为 |
|---|---|
| 未配置 `DECISION_LLM_PROVIDER` | 默认走 `TestModel`；`generate_decision_live` 明确报 `ModelProviderError`，不静默降级 |
| `provider=deepseek` 但缺 `DEEPSEEK_API_KEY` | 构造阶段抛 `ModelProviderError`，**不发请求** |
| 缺 `openai` 依赖 | 抛 `ModelProviderError` 并提示 `uv sync --group llm` |
| 鉴权失败 / 限流 / 网络错误 | 不吞异常，由调用方（脚本/评测）记录并决策是否重试 |
| 模型输出不符合 `DecisionOutput` | Pydantic 校验失败照常抛错，不生成兜底结论 |

## 5. 安全

- 密钥**只从环境变量**读取，不写入 `.env` 文件、不写入 `samples/`、不写入日志；
  `.gitignore` 已排除 `.env` / `.env.*`；
- 异常消息、日志、测试断言都不回显密钥（有单测守护）；
- 只持久化结构化的 `DecisionOutput`，不落盘原始请求/响应全文；
- `sk-...` 类凭据一旦出现在对话、工单或截图里，应视为已泄露并立即吊销重发。

## 6. 测试与验收

- 离线单测（默认 `pytest`，不联网）覆盖：默认配置、非法 provider/model、
  缺 key、缺依赖提示、异常消息不含密钥、显式 `TestModel` 路径正常出 `DecisionOutput`；
- 真实调用**只发生**在 `scripts/deepseek_smoke.py` 且 `DECISION_LLM_PROVIDER=deepseek` 时；
  脚本在未配置时打印跳过说明并返回 0（可安全进 CI）；
- 验收：`uv run --locked pytest -q` 全绿（不触网）；设置 key 后
  `uv run --group llm python scripts/deepseek_smoke.py` 返回合法 `DecisionOutput`。

## 7. 边界与责任

- DeepSeek API Key 由使用者自行提供，调用计费、数据合规由使用者负责；
- 本设计不保证模型质量、不把模型输出当作推演依据；
- 与 Nexent 真机链路的取舍见 `docs/nexent-northbound-transport-design.md`。

## 8. A/B 评测（2026-10-30 实测）

评测在 P8 的 22 个事件用例上构造同一个类型化决策任务（`src/decision_poc/llm_ab.py`，
报告 `docs/reports/p8-llm-ab-report.md`），三方对照：

| provider | 选中参照项 | Schema 合法 | 选项合法 | 引用命中率 | p50 |
|---|---:|---:|---:|---:|---:|
| `rule`（确定性参照） | 22/22 | 22/22 | 22/22 | 97.2% | 0.0 ms |
| `test`（地板基线，固定选第一项） | 13/22 | 22/22 | 22/22 | 95.5% | 6.1 ms |
| `deepseek`（deepseek-chat） | 21/22 | 22/22 | 22/22 | 95.5% | 4125.9 ms |

两个可回答的问题：

1. **模型能不能自己推断"最优先"？** 不能。去掉证据里的 `action_priority:*` 后，
   DeepSeek 从 21/22 掉到 11/22；拒答案例（证据不足 / 无权限）11/11 全部正确拒答。
2. **给定结构化优先级后能不能按优先级选？** 能，21/22，唯一未命中（C03）是
   模型选了"清掏雨水口"而非推演排序第一的"联合封路"。

结论：生产链路保留**确定性推演负责排序、模型只做给定选项内的选择与解释**；
这与 `docs/innovation-and-evidence.md` 的"不让模型生成推演断言"一致。

