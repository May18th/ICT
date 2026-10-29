# Nexent 集成边界

## 目的

将当前离线决策 PoC 与未来 Nexent 接入隔离，保留证据校验、审批门禁、评测发布和回滚能力。

## 当前边界

- `NexentAdapter` 只负责把 `DecisionRequest` 序列化为 JSON 负载。
- 传输行为通过 `invoke` 注入，默认不联网。
- 真实网络调用集中在 `NexentHttpTransport`（`/nb/v1/chat/run` SSE），只有显式构造才会发请求。
- LangGraph 流程不依赖 Nexent SDK、凭据、数据库或生产工具。
- 适配层不得绕过审批状态或直接执行副作用。

## 真实契约来源

Nexent 北向 API 的路径、鉴权头、请求体字段、SSE 事件、幂等与限流约定已在
[北向传输层设计](nexent-northbound-transport-design.md) 中按官方文档与源码逐项核对，
不需要靠猜测推进；该文档同时给出传输层实现和离线测试方案。

## 请求契约

```json
{
  "request_id": "req-1",
  "goal": "choose",
  "context": {"team": "ict"},
  "constraints": ["safe"]
}
```

真实传输层接入时，应在适配层外完成认证、超时、重试、幂等和审计；核心流程只接收结构化结果。

## 接入验收

1. 无凭据时，离线测试仍可完整运行。
2. 传输失败不会跳过审批或误标记为执行成功。
3. 相同 `request_id` 的重试不会产生重复副作用。
4. Nexent 返回结果必须能映射到现有 JSON 兼容状态。
5. 评测失败时 active 策略保持不变，并可回滚到已接受版本。

## 非目标

- 不在 PoC 中保存生产凭据（凭据只从环境变量读取，不落库、不打日志）。
- 不把 Nexent 的编排循环引入 LangGraph 之外。
- 传输层不实现审批、评测或回滚，这些仍由本地确定性规则负责。

