# 分层记忆走查：平台侧到哪一步（2026-11-22）

> **2026-11-22 22:41 状态更新：平台侧已补完 agent 层短期记忆的读写闭环，见文末 §七。§一「平台侧走不了」只对当天 15:59 那个时点成立，平台栈起不来时的判断仍可复用。**

> 本文是 §5 的底稿，从工作区临时目录搬进仓库，便于复核。走查脚本当时是临时文件，未随仓库分发；下面每条结论都能由 `samples/eval/decision_memory.json` 与 `src/decision_poc/experience.py` 直接核对。

## 结论先说

**平台侧走不了，离线侧走完了。** 限制条目「分层记忆未在平台侧验证」保持原样，不撤。

## 一、平台为什么走不了（15:59 更正）

15:45 那一版判断错了两处，这里更正：**Docker 引擎是在跑的**（`\\.\pipe\docker_engine` 存在）；`docker` 命令返回 SIGTERM 是宿主策略终止进程，不是引擎没起来。之前 curl 报 `000` 也是沙箱拦网络，不是端口没开。

真实状态：

| 检查 | 结果 |
|---|---|
| Docker 引擎管道 | 存在（运行中） |
| `docker ps` / `docker version` | 命令被宿主终止（关沙箱后同样被终止），拿不到容器列表 |
| TCP 5013（北向） | **在监听**，占用者是 `com.docker.backend`（端口映射代理） |
| HTTP 5013 `/nb/v1/health`、`/api/health`、`/docs` | 全部**无响应**，6 秒超时，一个字节都不回 |
| 5010（管理 API，记忆接口所在） | 未监听 |
| 3000 前端 / 5432 PG / 18000 MCP | 均未监听 |

**判断**：只有一个北向容器处于运行态并把 5013 映出来，但应用层不响应；数据库与管理 API 都没起。典型形态是后端卡在等依赖（DB 未启动），所以连接能建立、请求永远不回。平台栈没起来，不是端口没开。

要进入下一步，需要在本机终端里执行（本会话的 docker 命令会被宿主终止）：

```bash
cd D://华为ICT//Nexent//deploy//docker//compose
docker compose ps            # 看哪几个容器在跑、哪个在 restart
docker compose up -d         # 或按 ps 结果单独 start 数据库与 backend
```

## 二、离线侧：五步走完

走查脚本只读 `samples/eval/decision_memory.json`、不写仓库文件（临时文件，未分发）。

**① 写入**：同一场景签名、三种作用域各一条，效果不同。

| scope | 策略 | 偏差 |
|---|---|---:|
| tenant | flood-response-v1.0 | 0.3520 |
| user | flood-response-v1.1 | 0.1210 |
| agent | flood-response-v1.2 | 0.0000 |

**② 召回**（min_similarity 0.6）：三条相似度都是 1.000，排序 agent → user → tenant，符合分层优先级。`warm_start` 选中 agent 的 v1.2（偏差 0）。

**③ 逐层降级**：去掉 agent 层 → 命中 user（0.1210）；只剩 tenant → 命中（0.3520）；全空 → 无命中，从零起步（训练初始偏差 0.2264）。记忆确实起到"不从零调参"的作用。

**④ 持久化往返**：3479 字节，3 条写入再读回，召回顺序一致。

**⑤ 字段对照**（与 `sdk/nexent/memory/models.py`）：

| 我们 | 平台 MemoryRecord | 说明 |
|---|---|---|
| `entry_id` | `memory_id` | 平台 uuid4；我们由内容摘要派生，同内容不重复沉淀 |
| `scope` | `layer` | 值域一致：tenant / user / agent |
| — | `memory_type` | 平台分短/长期；我们只沉淀已验证结果，等价长期 |
| — | `tenant_id` / `user_id` | 平台按登录身份自动填；我们的库不带身份，靠文件路径区分 |
| — | `agent_id` / `conversation_id` | 平台用于建索引；我们只分别存在 `event_ids` / `review_ids` |
| `signature + params + outcome` | `content` | 平台是自由文本；我们是结构化 JSON，可比较、可回放 |
| `note` | `concept_tags` | 平台靠标签检索；我们靠签名相似度 |

## 三、顺手查出一处口径分歧

代码注释写「记忆分三层，越具体越优先（Agent > User > Tenant）」，但**只有 `recall()` 的排序遵守它，`warm_start()` 不遵守**：

实测——tenant 层偏差 0.0、agent 层偏差 0.30 时，`recall` 先列 agent，`warm_start` 却选了 tenant。

`warm_start` 的实现是按 `deviation` 取最小，不看 scope。这不算 bug（相似度门槛已保证场景相近，选效果最好的起步策略更合理），但**材料里不能写「越具体越优先」**——一查代码就对不上。现在的材料没这么写，不用改；要改的是 `src/decision_poc/experience.py` 顶部注释那一句。

## 四、平台起来后怎么补完（6 条命令）

前置：`NEXENT_API_KEY` 取自 `agent_poc/.env`。

```bash
K="<NEXENT_API_KEY>"; B="http://localhost:5013"

# 1 写一条 agent 层记忆（tenant/user 层走 /memory/long-term/{scope}，走这里会返回 410）
curl -s -X POST "$B/memory/records" -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"layer":"agent","memory_type":"long_term","agent_id":"urban_deduction_agent",
       "content":"场景 A-03+B-01 缺口 0.667 → 策略 v1.2 → 偏差 0.0",
       "concept_tags":["urban-drainage","allocation"],"idempotency_key":"mem-probe-01"}'

# 2 列出，确认落库
curl -s "$B/memory/records?limit=5" -H "Authorization: Bearer $K"

# 3 检索，确认能召回
curl -s -X POST "$B/memory/records/search" -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"query":"A-03 B-01 抽排缺口 分配策略","layers":["agent"],"top_k":3,"threshold":0.65}'

# 4 取上下文（平台实际注入提示词的那一段）
curl -s "$B/memory/context" -H "Authorization: Bearer $K"

# 5 跨会话验证：同一事件跑两次对话，比较第二次的初始偏差（应直接 0，而非 0.2264）

# 6 隔离验证：换一个 user 再检索，确认召回为空（对应离线侧"另一份库文件"）

# 7 清理探针
curl -s -X DELETE "$B/memory/records/<memory_id>" -H "Authorization: Bearer $K"
```

第 5、6 步做完，限制条目才能改写成「已接入平台分层记忆」。在那之前，唯一的合法表述是现在这句。

## 五、接上之后还有一个坑

平台有从最终答案自动抽取记忆的 `fa_memory_extractor`，输入是**自由文本答复**。我们的提示词把最终答案约束成 `<code>` 里的 `final_answer(json)`——自动抽取大概率抽不出可用记忆。所以要么在工具侧主动调 Create 接口写入，要么调整抽取输入。别指望自动抽取一条龙。

---

## 六、按推荐已处理（15:58）

**① 代码注释改准**（`src/decision_poc/experience.py`）：

- 原：「记忆分 Tenant / User / Agent 三层，越具体越优先（Agent > User > Tenant）」→ 改为「**召回排序**越具体越优先，但 **warm start 只挑偏差最低的一条、不看层级**——场景相似度已由门槛保证，起步策略认效果不认来源」。
- 诚实边界那段同步收紧：说明平台侧六个接口位置已核实，但没做过一次真实写入与召回。

自检仍 **108/108（`a63cbf4f9fc53c6e`）**，注释改动不触碰行为。

**② 提交件限制条目同步收紧**（第 18 页第 4 条）：

> 4．分层记忆未在平台侧验证：决策记忆（租户 / 用户 / 智能体三层）在离线同构实现中跑通，写入、召回、逐层降级、持久化四项都有实测；平台侧的记忆写入、检索、上下文取用与按作用域写长期记忆四个接口位置已核实，但没有做过一次真实写入与召回。材料中不宣称已接入。

页数仍 22，两份 sha `8dc4fc702168b53d…`，禁词 0。

这样改的用意：把「没验证」从一句自我否定变成「哪四项已测 + 哪四个位置已定位 + 缺的最后一步是什么」。评委要追问时，问的是最后一句话，而不是整条存不存在。

---

## 七、平台侧补完了（2026-11-22 22:41 实测）

平台栈起来后按 §四 跑，三处与当时规划不同，先说清：

1. **接口不在北向**。记忆路由挂在 5010 管理 API（`/memory/records`、`/memory/records/search`、`/memory/context`、`/memory/long-term/{scope}`）。5013 上实测 404，§四 那几条 curl 打 5013 是错的。
2. **`memory_records_t` 只收 agent 层短期记忆**。库上有 `ck_memory_records_agent_short_term`：
   `CHECK (layer = 'agent' AND memory_type = 'short_term')`。第一次按 `long_term` 写直接被
   406 `Failed to persist memory record` 打回，容器日志里的根因是 `CheckViolation`。这条表还要带
   `conversation_id` 才能按会话隔离。
3. **长期记忆是版本化 Markdown**，不是这张表：`/memory/long-term/{scope}/versions`，
   `scope` 只有 tenant / user，tenant 的写入要求 ADMIN 角色。

实测结果（`samples/nexent/live/memory_roundtrip.json`，脚本 `deploy/nexent/memory_roundtrip.py`）：

| 步骤 | 接口 | 结果 |
|---|---|---|
| 写 agent 层 | `POST /memory/records` | 200，`memory_id=456` |
| 落库确认 | `GET /memory/records` | 可见 |
| 召回 | `POST /memory/records/search` | 命中 1 条，相似度 `0.8448` |
| 上下文注入 | `GET /memory/context` | `agent_short_term` 1 块，prompt 161 字符，含探针内容 |
| 跨用户隔离 | 换 `sub` 再列 | 0 条，不可见 |
| 清理 | `DELETE /memory/records/456` | 200，再列已无 |

所以 §一 的结论要收窄成一句：**agent 层短期记忆的平台侧读写闭环已验证；租户/用户两层长期记忆没写**——
写它们会改变所有对话看到的上下文，属平台状态变更，没在无人值守脚本里做。
