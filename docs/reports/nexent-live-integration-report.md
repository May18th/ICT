# Nexent 真机集成报告：城市内涝主链路上平台

- 日期：2026-11-03（首版 2026-10-30，本轮全量复跑并重写结论段）
- 环境：本机 Docker Desktop + Nexent **v2.7.0**（openPangu-2.0-Pro 模型，`nexent-config` / `nexent-runtime` / `nexent-northbound` 全栈）
- 结论：**城市内涝主链路已在 Nexent 上真机跑通**——平台 Agent 调用 MCP 工具后产出六跳 JSON，
  与离线推演 **8/8 项一致**（`conversation_id=78`）。
- 证据：`samples/nexent/live/`（Agent 列表、会话记录、MCP 调用日志、技能绑定、协同实测、汇总 JSON）。
- 复现：`deploy/nexent/README.md`。

## 1 为什么这一步是必须的

赛题 3 的五个考点里，「基于 Nexent 可运行」是唯一无法用离线测试替代的。此前仓库的证据是：
北向传输层 + 绑定表 + 设备振动场景 smoke——**主演示链路（内涝六跳/冲突/分配）从未在平台上运行**。
本轮补的就是这一格，并且不是"能答一句话"，而是"结论与离线逐项一致"。

## 2 装配结果（平台内实际对象）

| 对象 | 值 |
| --- | --- |
| MCP 服务 | `urban-twin-mcp`（mcp_id=2），URL `http://urban-twin-mcp:8000/sse`，status=True，enabled=True |
| MCP 工具 | **13 个**（平台 tool_id 41–53）：semantic_query / twin_neighbors / evidence_locate / claim_trace / provenance_verify / event_deduce / counterfactual_run / conflict_check / allocation_plan / scene_render / scene_compare / asset_lookup / collab_run |
| 工具库命中 | `scan_tool` **13/13**，缺失 0 |
| 主 Agent | `urban_deduction_agent`（agent_id=3），模型 openPangu-2.0-Pro，已发布 **v10** |
| 主 Agent 工具 | **只挂 2 个**：`event_deduce` + `evidence_locate`（收窄原因见 §6） |
| 协同 Agent | `urban_collab_agent`（agent_id=4），已发布 **v4**，挂 6 个工具（含 `collab_run`） |
| 技能绑定 | 2 个 SKILL.md 在草稿与 v10 发布版本均 `present=True enabled=True` |
| 协议修复 | `enable_protocol_repair_retry=true`（openPangu 工具调用文本需要修复重试） |
| 北向 Key | 平台函数生成（token_id=38，密钥不落仓） |

## 3 真机对话结果（conversation 78）

用户问题：`WO-20261029-007 @ 2026-10-29T09:40:00+08:00`（城市内涝跨部门六跳推演）。

Agent 回答是**纯 JSON**（3596 字符，无 `<|tool_call_start|>` 残留、无 Markdown 围栏），
节选如下，完整见 `samples/nexent/live/conversation_78.json`：

```json
{
  "event_id": "WO-20261029-007",
  "as_of": "2026-10-29T09:40:00+08:00",
  "summary": "A-03 兴华路积水 30cm 触发橙色响应，下穿隧道倒灌、基坑沉陷与泵站故障叠加，移动泵缺口 2 台需跨区增援",
  "six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"],
  "conflicts": [
    {"conflict_id": "C1", "kind": "overlap", "subject": "封路权限"},
    {"conflict_id": "C2", "kind": "gap", "subject": "DF-PS-01"},
    {"conflict_id": "C3", "kind": "contention", "subject": "RES-PUMP-M"}
  ],
  "resource_gap": 2,
  "action_order": ["停止基坑开挖并覆盖或围堰…", "封闭下穿隧道洞口全幅…", "隔离风险源…", "…共 10 条…"],
  "residual_risks": ["…共 8 条…"],
  "evidence_refs": ["AS-A-03-work_order-…#row:2", "AS-A-03-emergency_plan-…#line:31", "…共 35 条…"],
  "handoff_required": true
}
```

平台确实调用了 MCP 工具（`samples/nexent/live/mcp_tool_log.jsonl`，来自 MCP 容器自身的调用日志）：

| 工具 | 状态 | 耗时 | 快照哈希前 12 位 |
| --- | --- | --- | --- |
| `event_deduce` | ok | 18 ms | `2085018615b3` |
| `event_deduce`（协议修复重试） | ok | 6 ms | `2085018615b3` |

两条记录快照哈希一致 → 平台看到的语义世界与离线是同一版。
日志归属：本轮验证前已清空容器日志，`live_evidence.json` 的 `tool_log` 记了
`container_started_at` 与 `file_mtime`，所以这份日志只覆盖这一轮。

## 4 一致性校验（8/8）

`python -m decision_poc.nexent_urban` 把平台回答与离线 `DeductionEngine` 结果逐项比对：

| 检查项 | 期望（离线） | 平台实际 | 结果 |
| --- | --- | --- | --- |
| 六跳齐全 | observed/affected/owner/conflict/resource/action | 同 | ✅ |
| 冲突编号 | C1/C2/C3 | 同 | ✅ |
| 冲突类型 | overlap/gap/contention | 同 | ✅ |
| 资源缺口 | 2 台 | 2 台 | ✅ |
| 隔离先于抽排 | true | true | ✅ |
| 关键处置动作 | 隔离风险源/抽排/封闭积水路段 | 均出现 | ✅ |
| 证据非空 | ≥1 条 `asset_id#locator` | 35 条 | ✅ |
| 事件编号 | WO-20261029-007 | 同 | ✅ |

校验结果落盘：`samples/eval/nexent_urban_smoke.json`（`checks` 8 条全 passed，`conversation_id=78`）。

## 5 装配方式（可复现，非手工点选）

| 步骤 | 实现 | 产物 |
| --- | --- | --- |
| MCP 服务容器化 | `deploy/docker-compose.mcp.yml`（join 外部网络 `nexent_network`，healthcheck 校验 `tools_count==13`） | `urban-twin-mcp` 容器 |
| 平台注册 | `deploy/nexent/setup_nexent.py register-mcp`（`/api/mcp/add`，JWT 由容器环境签） | `mcp_record_t`（mcp_id=2） |
| 工具同步 | `sync-tools`（`/api/tool/scan_tool`）+ 逐个 `/api/tool/update` 挂到 Agent 草稿 | tool_id 41–53 |
| Agent 创建/发布 | `create-agent`（**upsert**：`/api/agent/update` 带 agent_id → `/api/agent/{id}/publish`） | agent_id=3，v10 |
| 技能上传 | `upload_skills.py all`（平台自己解析 frontmatter、存正文，脚本只传输与复核） | skill_id=2/3，正文逐位一致 |
| 技能绑定 | `bind_skills.py all`（绑草稿 v0）→ 重新发布 → `verify --require-published` | 草稿与 v10 均 enabled |
| 北向 Key | `mint-key`（平台 `database.token_db.create_token`，不手写 SQL） | token_id=38 |
| 验证 | `python -m decision_poc.nexent_urban` | 8/8 + conversation_id |
| 协同验证 | `artifacts/nexent_collab_live.py --attempts 3` | 指纹比对 + 成功率 |
| 取证 | `deploy/nexent/capture_evidence.py` | `samples/nexent/live/` |

## 6 真实踩坑与修复（答辩可讲）

1. **Nexent 不认 stdio**：v2.7.0 的 MCP 客户端按 URL 后缀选传输（`/sse` → SSE，`/mcp` → Streamable HTTP），
   原有 stdio 服务接不进去 → 新增 `mcp_sse.py`（标准库手写 SSE，10 项测试）。
2. **Docker→宿主机被防火墙拦**：`host.docker.internal:8000` 连接被拒，且加防火墙规则需管理员权限
   → 把 MCP 服务做成容器跑在 `nexent_network` 里，用容器名互访。
3. **系统代理把 httpx 打挂**：Windows `NO_PROXY` 含 `10.*;192.168.*` 等通配，httpx 构造代理规则时
   抛 `Invalid port`，表现为"服务活着却连不上" → 传输层 `trust_env=False`，代理改为显式传入。
4. **技能绑定把主链路从 8/8 打到 0/8**（本轮最关键的一条）：原 SKILL.md 写"必须调
   `twin_neighbors` / `conflict_check` / `allocation_plan`"，模型照做后走 `parallel_executor`，
   回答截断在 `<|tool_call_start|>`，一个 JSON 都出不来。**绑定技能不是"加了份文档"，是改变了
   模型的任务理解**。
5. **提示词拦不住工具选择**：DUTY_PROMPT 明令"禁止 python_interpreter / read_skill_md /
   parallel_executor、只调 event_deduce"，实测 conv 74 仍开 `parallel_executor`，conv 75 把调用
   写成 `event_deduce(event_id=...)` 函数语法。**模型真正读的"提示词"是挂载的工具列表**——
   把主 Agent 收窄到 2 个工具后，v10 一次跑通 8/8。
6. **装配脚本两个隐性坑**：`create_agent` 只在 Agent 不存在时创建，复用已有 Agent 时**不回写
   prompt**（改了提示词却发布旧提示词，行为"没变化"却查不出原因）；`mint_api_key` 从 `/tmp`
   执行会 `ModuleNotFoundError: database`（缺 `sys.path`）。两处已修。
7. **`temperature=0` 是反效果**：设 0 后模型在工具调用之后返回**空回答**（conversation 76/77
   的 assistant `content` 长度为 0），已回退默认值。
8. **协同 Agent 的两种合法外壳**：它会用 `final_answer` 工具包一层
   （`{"name":"final_answer","arguments":{"content":"<json>"}}`），解析器必须认，否则把
   "成功"误判成"失败"。修正解析口径后，3 次调用从 1/3 变 **2/3** 通过。

## 7 遗留与下一步

| 项 | 状态 |
| --- | --- |
| Skills 上传 + 绑定到已发布 Agent | **已完成**（skill_id=2/3；草稿与 v10 均 enabled）。但实测模型不主动读技能文档，口径只能说"技能已绑定、可在平台浏览与复用"，不能说"Agent 按技能文档执行流程" |
| 平台截图与录屏 | **未完成**：`capture_platform_ui.py` 需要租户账号密码（`NEXENT_ADMIN_PASSWORD`），本会话未提供 |
| 公网 Demo 地址 | **未做**：当前是本机 Docker + 本机面板；方案见 §8 |
| 多事件真机回归 | 未做：当前只验证核心事件；扩事件需同步扩 `nexent_urban` 的 baseline 与用例 |
| 主 Agent 工具收窄的代价 | 主 Agent 只挂 2 个工具，13 个工具在"工具库"里可见但不由主 Agent 直接调；若要演示"多工具编排"，走协同 Agent（6 个工具） |

## 8 公网化的可行路径（未实施，先给方案）

本机 Nexent 是 `3000`（前端）与 `5013`（北向）两个入口，直接映射到公网会有三个硬问题：
管理后台弱口令、北向 API Key 明文、以及没有 TLS。可行顺序：

1. **先加反向代理 + 基本认证**（Caddy 或 Nginx），只暴露 `/nb/v1/*` 与前端只读页；
2. **隧道优先于端口映射**：Cloudflare Tunnel / frp 出站连接，不开放入站端口，省掉防火墙与证书；
3. **Key 与口令外置**：`NEXENT_API_KEY`、管理员口令走环境变量/密钥管理，不进镜像、不进仓；
4. 演示期给北向接口加**限流 + 只读白名单**（只放 `chat/run` 与 `agents`、`conversations/{id}`）。

## 9 一句话答辩口径

「同一套推理内核，离线可复算、平台上可运行：Nexent 的 Agent 调我们 13 个 MCP 工具做六跳推演，
结论与本仓库离线引擎 8/8 一致（conversation_id 可查）；策略进化与回滚在面板上可当场演示。」

---

## 附：2026-11-02 复跑记录（历史，口径已被 11-03 取代）

| 项 | 结果 |
| --- | --- |
| MCP 容器 | `urban-twin-mcp` 重建后 `/health` 返回 13 个工具（含 `collab_run`） |
| 平台工具库 | `scan_tool` 命中 13/13，缺失 0 |
| 主 Agent | `urban_deduction_agent` 发布 v5，挂 12 个结论类工具 |
| 协同 Agent | `urban_collab_agent` 发布 v1，挂 6 个工具 |
| 主链路复核 | 8/8 项一致，conversation_id=59 |
| 协同复核 | `kernel_fingerprint=d7c16d572ca7a33e` 与离线逐字一致，conversation_id=60 |

当时的已知限制：协同 Agent 2 次调用 1 次返回可解析 JSON；本机内存 15.3 GB，
`nexent-data-process` 占 4.5 GB 时 northbound 请求会被掐断（`docker restart nexent-northbound` 恢复）；
Agent-as-Tool（路径 A）未验证，无证据不宣称。

## 2026-11-06 复测：作答协议对齐后 8/8（Agent v18）

平台运行时升级后，Agent 的"作答"必须由 `<code>` 代码块里的 `final_answer(...)` 给出；而当时的提示词
与查询文案都写着"只输出 JSON、不要 Markdown 代码块"，两者直接冲突。表现不是"答案错"，而是
**每一轮都被判 `no_action_no_final` 并整轮重跑**（单次请求 7 分钟以上仍无终态，客户端只能超时），
平台侧还会把上一条挂住的会话继续跑，把运行时单 worker 占满，后续请求排队。

两处根因与修法：

| 现象（运行时日志） | 根因 | 修法 |
| --- | --- | --- |
| `agent.no_action_no_final` 反复出现，模型输出是**正确的 JSON 正文** | 提示词禁止代码块，模型照做；但平台只认 `final_answer` | `DUTY_PROMPT`/`CONSTRAINT_PROMPT` 与 `build_urban_query` 都改成"用 `<code>` 块内 `final_answer(json.dumps(...))` 交卷"，并明令禁止 ``` 围栏（围栏不执行） |
| `reason=truncated_generation`（step 2） | `requested_output_tokens=4096` 被深度思考吃掉，answer 在 `evidence_refs` 中途被截断 | 主 Agent 提到 16384（模型上限 32768），并把 `evidence_refs` 限制在 3–5 条 |

复测结果（`uv run python -m decision_poc.nexent_urban --attempts 2`）：

| 项 | 值 |
| --- | --- |
| 结论一致性 | **8/8**，`attempts=1`（未用重试） |
| conversation_id | **119** |
| Agent 版本 | v18（主 Agent），v6（协同），v2（LLM 决策） |
| 工具调用 | 2 次 `event_deduce`，均 `status=ok`，快照 `d102aa80…` |
| 工具日志 | `samples/nexent/live/mcp_tool_log.jsonl` 仅 2 行，与本轮一一对应（跑前清空过，避免历史累积冒充本轮） |

遗留问题（下一轮）：

1. **稳定性**：同一提示词下仍有偶发 ``` 围栏与超长思考，靠 `--attempts 2` 兜底；根因是模型侧格式服从性，
   平台层无法根治，只能在验收口径里显式允许"协议层重试"，并区分"结论不一致不重试"。
2. **并发**：运行时按会话串行，客户端超时后服务端不会自动停，必须 `GET /nb/v1/chat/stop/{id}` 显式停，
   否则新请求排队——这是本次排查里最长的一段弯路。
3. **LLM 决策 Agent（DeepSeek）**：3 次实测均 `selected_match=false`（期望 opt-3，实际 opt-2×2、opt-1×1）。
   证据池 8 个槽位被 `action_priority:1..7` 占满，模型看得到排序却看不到支撑证据，于是按"积水 30cm 已过 25cm 阈值"
   自行选了 opt-2。属证据池设计问题，不是模型随机性，下一轮单独修。
