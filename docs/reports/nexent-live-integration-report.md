# Nexent 真机集成报告：城市内涝主链路上平台

- 日期：2026-10-30
- 环境：本机 Docker Desktop + Nexent **v2.7.0**（openPangu-2.0-Pro 模型，`nexent-config` / `nexent-runtime` / `nexent-northbound` 全栈）
- 结论：**城市内涝主链路已在 Nexent 上真机跑通**——平台 Agent 调用 MCP 工具后产出六跳 JSON，
  与离线推演 **8/8 项一致**（`conversation_id=21`）。
- 证据：`samples/nexent/live/`（Agent 列表、会话记录、MCP 调用日志、汇总 JSON）。
- 复现：`deploy/nexent/README.md`。

## 1 为什么这一步是必须的

赛题 3 的五个考点里，「基于 Nexent 可运行」是唯一无法用离线测试替代的。此前仓库的证据是：
北向传输层 + 绑定表 + 设备振动场景 smoke——**主演示链路（内涝六跳/冲突/分配）从未在平台上运行**。
本轮补的就是这一格，并且不是"能答一句话"，而是"结论与离线逐项一致"。

## 2 装配结果（平台内实际对象）

| 对象 | 值 |
| --- | --- |
| MCP 服务 | `urban-twin-mcp`（mcp_id=2），URL `http://urban-twin-mcp:8000/sse`，status=True，enabled=True |
| MCP 工具 | 12 个（平台 tool_id 41–52）：semantic_query / twin_neighbors / evidence_locate / claim_trace / provenance_verify / event_deduce / counterfactual_run / conflict_check / allocation_plan / scene_render / scene_compare / asset_lookup |
| Agent | `urban_deduction_agent`（agent_id=3），模型 openPangu-2.0-Pro，已发布 **v3（urban-mcp-v3）** |
| Agent 工具 | 13 个 MCP 工具全部挂在草稿与已发布版本上（`ag_tool_instance_t` v0/v1 各 12 条） |
| 协议修复 | `enable_protocol_repair_retry=true`（openPangu 工具调用文本需要修复重试） |
| 北向 Key | 平台函数生成（`token_id=3`，密钥不落仓） |

## 3 真机对话结果（conversation 21）

用户问题：`WO-20261029-007 @ 2026-10-29T09:40:00+08:00`（城市内涝跨部门六跳推演）。

Agent 回答（节选，完整见 `samples/nexent/live/conversation_21.json`）：

```json
{
  "event_id": "WO-20261029-007",
  "as_of": "2026-10-29T09:40:00+08:00",
  "summary": "A-03 片区积水深度达 30cm 触发橙色响应，移动泵需求 7 台、可用 6 台，缺口 1 台需跨区调配。",
  "six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"],
  "conflicts": [
    {"conflict_id": "C1", "kind": "overlap", "subject": "封路权限"},
    {"conflict_id": "C2", "kind": "gap", "subject": "DF-PS-01"},
    {"conflict_id": "C3", "kind": "contention", "subject": "RES-PUMP-M"}
  ],
  "resource_gap": 1,
  "action_order": ["停止基坑开挖并覆盖或围堰…", "隔离风险源…", "封闭积水路段全幅…", "…"],
  "residual_risks": ["…5 条…"],
  "evidence_refs": ["AS-A-03-work_order-…#row:2", "AS-A-03-emergency_plan-…#line:31", "…共 6 条…"],
  "handoff_required": true
}
```

平台确实调用了 MCP 工具（`samples/nexent/live/mcp_tool_log.jsonl`，来自 MCP 容器自身的调用日志）：

| 工具 | 状态 | 耗时 | 快照哈希前 12 位 |
| --- | --- | --- | --- |
| `event_deduce` | ok | 29 ms | `9b60debff3c9` |
| `allocation_plan` | ok | 28 ms | `9b60debff3c9` |
| `scene_render` | ok | 88 ms | `9b60debff3c9` |

三条记录的快照哈希一致 → 平台看到的语义世界与离线是同一版。

## 4 一致性校验（8/8）

`python -m decision_poc.nexent_urban` 把平台回答与离线 `DeductionEngine` 结果逐项比对：

| 检查项 | 期望（离线） | 平台实际 | 结果 |
| --- | --- | --- | --- |
| 六跳齐全 | observed/affected/owner/conflict/resource/action | 同 | ✅ |
| 冲突编号 | C1/C2/C3 | 同 | ✅ |
| 冲突类型 | overlap/gap/contention | 同 | ✅ |
| 资源缺口 | 1 台 | 1 台 | ✅ |
| 隔离先于抽排 | true | true | ✅ |
| 关键处置动作 | 隔离风险源/抽排/封闭积水路段 | 均出现 | ✅ |
| 证据非空 | ≥1 条 `asset_id#locator` | 6 条 | ✅ |
| 事件编号 | WO-20261029-007 | 同 | ✅ |

校验结果落盘：`samples/eval/nexent_urban_smoke.json`（`checks` 8 条全 passed，`conversation_id=21`）。

## 5 装配方式（可复现，非手工点选）

| 步骤 | 实现 | 产物 |
| --- | --- | --- |
| MCP 服务容器化 | `deploy/mcp_sse/Dockerfile` + `docker run --network nexent_network` | `urban-twin-mcp` 容器，`/health` 返回 13 工具 |
| 平台注册 | `deploy/nexent/setup_nexent.py register-mcp`（`/api/mcp/add`，JWT 由容器环境签） | `mcp_record_t` 记录（mcp_id=2） |
| 工具同步 | `sync-tools`（`/api/tool/scan_tool`）+ 逐个 `/api/tool/update` 挂到 Agent 草稿 | tool_id 41–52 |
| Agent 创建/发布 | `create-agent`（`/api/agent/update` → `/api/agent/{id}/publish`） | agent_id=3，v3 |
| 北向 Key | `mint-key`（平台 `database.token_db.create_token`，不手写 SQL） | token_id=3 |
| 验证 | `python -m decision_poc.nexent_urban` | 8/8 + conversation_id |
| 取证 | `deploy/nexent/capture_evidence.py` | `samples/nexent/live/` |

## 6 真实踩坑与修复（答辩可讲）

1. **Nexent 不认 stdio**：v2.7.0 的 MCP 客户端按 URL 后缀选传输（`/sse` → SSE，`/mcp` → Streamable HTTP），
   原有 stdio 服务接不进去 → 新增 `mcp_sse.py`（标准库手写 SSE，10 项测试）。
2. **Docker→宿主机被防火墙拦**：`host.docker.internal:8000` 连接被拒，且加防火墙规则需管理员权限
   → 改为把 MCP 服务做成容器跑在 `nexent_network` 里，用容器名互访。
3. **系统代理把 httpx 打挂**：Windows `NO_PROXY` 含 `10.*;192.168.*` 等通配，httpx 构造代理规则时
   抛 `Invalid port`，表现为"服务活着却连不上" → 传输层 `trust_env=False`，代理改为显式传入。
4. **openPangu 工具调用以文本输出**：最终答案一度是 `<|tool_call_start|>[...]` 文本 → 开
   `enable_protocol_repair_retry`，并把提示词收敛为"先调 `event_deduce`、再只输出 JSON"两步式。
5. **模型爱用 `parallel_executor`**：会连发多个子任务并卡在中间态 → 提示词明确禁止，改直连工具。
6. **`twin_neighbors` 传参错**：模型把工单号当 `twin_id`，返回 NOT_FOUND（不影响 8/8；后续提示词补格式）。

## 7 遗留与下一步

| 项 | 状态 |
| --- | --- |
| Skills 上传到 Nexent 技能仓库 | **未做**：`samples/nexent/skill/*/SKILL.md` 已就绪，平台「技能仓库 → 上传 SKILL.md」即可；不影响当前 8/8 |
| `twin_neighbors` / `conflict_check` 也纳入强校验 | 本次只强校验 8 项（含 event_deduce/allocation_plan 产物）；模型少调工具时会缺项，可扩为"必须调用 N 个工具"检查 |
| 平台截图与录屏 | 未做：需要 GUI 操作（本会话无浏览器），建议现场补 |
| 多事件真机回归 | 未做：当前只验证核心事件；扩事件需同步扩 `nexent_urban` 的 baseline 与用例 |
| 公网 Demo 地址 | 未做：当前是本机 Docker + 本机面板 |

## 8 一句话答辩口径

「同一套推理内核，离线可复算、平台上可运行：Nexent 的 Agent 调我们 13 个 MCP 工具做六跳推演，
结论与本仓库离线引擎 8/8 一致（conversation_id 可查）；策略进化与回滚在面板上可当场演示。」
