# P12 多智能体协同层落地报告

- 日期：2026-11-02
- 对应设计：[P12 多智能体协同 + 场景 v2 设计](../superpowers/specs/2026-11-02-p12-multi-agent-collaboration-and-scenario-v2-design.md) §2–§4、T4
- 本阶段范围：**只做协同内核（契约 / 角色 / 协同板 / 编排）**；不注册 MCP 工具、不接 Nexent 多 Agent、不改六跳数字
- 结论：1 总控 + 5 专业部门可确定性跑通，六角色覆盖 100%，联合发布与处置顺序由规则判定，内核指纹稳定

## 1 实现清单

| 模块 | 内容 |
| --- | --- |
| `src/decision_poc/collab/contracts.py` | `ActionCard`（动作卡）、`RoleHandoff`（结构化交接）、`CollabResult`（协同结果）、`kernel_fingerprint` |
| `src/decision_poc/collab/roles.py` | 6 个角色声明 + 工具组 + `RULE_OWNERS` 规则归属表（数据，非 if/else）+ 顺序硬约束表 |
| `src/decision_poc/collab/blackboard.py` | 协同板：只传卡片 id，不做自由对话 |
| `src/decision_poc/collab/orchestrator.py` | 出卡 → 联合发布判定 → 无职责角色补 `no_action` → 顺序编排 → 指纹 |

## 2 三条边界怎么被强制（不是口号）

| 边界 | 落点 |
| --- | --- |
| 断言权在内核 | `ActionCard` 构造期校验 `claim_ids` 与 `evidence_refs` 非空；测试断言所有卡的 `claim_ids ⊆ claims_from(deduction)` |
| 裁决权在规则 | `co_sign_required` 由规则归属表生成；缺部门即 `rejected + 理由`，没有"协商"入口 |
| 复现口径分轨 | `kernel_fingerprint` 只哈希推演的 `step_id/stage/status/conclusion/evidence`；测试模拟 Agent 输出漂移，指纹必须不变 |

## 3 实测（核心事件 WO-20261029-007 @ 2026-10-29T09:40+08:00）

| 项 | 结果 |
| --- | --- |
| 角色 | 6 个（EMERG-COORD / ORG-DRAIN / ORG-ROAD / ORG-TRAFFIC / ORG-GAS / ORG-POWER） |
| 覆盖度 | **6/6 = 100%**（无职责者发 `no_action` 并带理由） |
| 动作卡 | 24 张，状态分布 `accepted: 24` |
| 联合发布对 | 8 对，含 `(ORG-ROAD, ORG-TRAFFIC)` 联合封路 |
| 处置顺序 | 13 条动作，隔离风险源序位在抽排之前 |
| 顺序硬约束 | 违规 **0 条** |
| 冲突保留 | 内核 C1/C2/C3 原样进入协同结果，未被"综合" |
| 资源缺口卡 | `dispatch_pump` 带数量，取自内核 `ResourceGap.gap` |
| 内核指纹 | `fdbfa41399ab96b8`，两次运行逐位一致 |

## 4 本阶段明确没做

| 项 | 原因 |
| --- | --- |
| MCP `collab_run` 工具 + 角色工具组白名单（T5） | 会让 `mcp.tools_count` 从 12 变 13，属对外数字变更，与平台装配同一批做 |
| Nexent 多 Agent 真机装配 | 需先验证平台是否支持 Agent-as-Tool；不支持则走"总控 Agent 调 `collab_run`"保底 |
| LLM 出卡适配器 | 契约已留出替换点；但先要保证确定性内核可测，避免"多 Agent 一上来就不可复现" |
| 消融第 4 档（多 Agent vs 单 Agent） | 依赖 P11 消融框架接入，属 T8 之后 |

## 5 对外数字变化

测试数 639 → 671（新增 32 项协同层测试），自检指纹 `d7d55f5e800d5ff2` → `73b104597a9c2318`。
已按 `AGENTS.md`「单一 owner + 一次性同步」改全五处并重新生成自检报告与初赛内容稿 `.docx`；后续 v2 标定再增至 734 项（指纹随测试数变化，以实跑为准）；
六跳、缺口、冲突、P9 分配等其余 105 项口径未变。

## 6 验证

```powershell
uv run --locked pytest -q                                   # 671 passed
uv run --locked python -m decision_poc.evidence_check        # 106/106（指纹 73b104597a9c2318）
```
