# RAG 检索层接入 + 平台清理（2026-11-06）

## 1 一句话

把 `rag/` 从「能跑但零引用」接成 MCP 工具 `doc_search`，索引改为**按台账登记建**（`asset_id` 与 `evidence_locate` 对齐），并在平台侧清掉 7 个孤儿工具。工具数 14 → 15 已同步到平台、容器与材料。

## 2 接入前挡着的两个硬问题

### 2.1 索引的是代码仓，不是业务资产

原始 `rag build --root .` 扫的是整个仓库，出来的 `asset_id` 是 `AS-UNK-tech_doc-0a87beb3`（未识别），而台账里是 `AS-A-03-emergency_plan-cbaa17ca`。两边带的是**两批文件**，ref 拼出来 `evidence_locate` 一个都不认。

原因在 `corpus._text_units` 调 `identify_asset(path, doc_type=..., version="v1")` 时**没传片区**，于是 asset_id 里的片区段落成 `UNK`。

修法不是改 id 规则，而是加一个入口：`load_registered_corpus()` 读台账清单，把每份业务资产的 `asset_id`/`doc_type`/`version`/`片区` 带进切分，CLI 对应 `rag build --registered`。

验证：`identify_asset(p, doc_type='duty_list', version='v1', equipment_id='A-03')` → `AS-A-03-duty_list-07d920f0`，与台账逐位一致。

### 2.2 检索不认治理状态

第一轮检索「暴雨积水 封路 阈值」，命中里同时有 2025 版预案和 **2021 版已废止预案**（`valid_to=2025-06-01`），封路阈值一个 25cm 一个 30cm。模型挑错一条就是业务事故。

`doc_search` 现在按 `governance` 的 `status` 过滤，默认只放行 `published`：

| 调用 | 命中 | 剔除 |
| --- | --- | --- |
| 默认 | 3 | 2 |
| `include_deprecated=true` | 5 | 0 |

被剔除的两条是 `AS-A-03-emergency_plan-cbaa17ca`（`deprecated`，`valid_to=2025-06-01`）。

## 3 闭环验证

`doc_search` 的返回里，每条命中直接给出 `evidence_ref = asset_id#locator`，与 `evidence_locate` 要的格式完全一致：

```
doc_search("基坑开挖 暴雨 职责冲突")
  → AS-A-03-duty_list-... #A-03/duty_list/v1#line:... 
  → evidence_locate(evidence_ref) → status=ok，取回「部门职责以《部门职责清单》为准…」
```

容器内（`urban-twin-mcp`）与宿主机两次独立验证均通过。

## 4 平台侧同步

| 项 | 结果 |
| --- | --- |
| 容器 `/health` | `tools_count=15`，`status=ok` |
| 平台 `/api/tool/list` 我方 15 个 | 15/15 命中，全部 `source=mcp`、`is_available=true` |
| `doc_search` | `usage=urban-twin-mcp`，已可用 |
| MCP 记录 `registry_json._toolNames` | 原缓存 14 项且缺 `doc_search`，已刷新为 15 |
| MCP 描述 | 原写「14 个工具」，已改为 15 |
| `setup_nexent.py::OUR_TOOLS` | 硬编码 14 项且缺 `doc_search`，已补（这是 `sync-tools` 的比对基准，不补会一直报缺失） |

## 5 平台清理

### 5.1 删掉 7 个孤儿工具

来源是「删了 MCP 服务，工具记录留在库里」：

| tool_id | 名称 | 来源 | 建表时间 | 状态 |
| --- | --- | --- | --- | --- |
| 34 | `local_test_tool_name` | outer-apis | 2026-10-22 | 平台自测残留 |
| 35–40 | `kg_get_ontology` / `kg_query_entity` / `kg_search_path` / `kg_search_entities` / `kg_add_triple` / `kg_stats` | kg-mcp-server | 2026-10-23 | `is_available=false`（所属 MCP 已删） |

`capture_platform_ui.py:70` 里那句「无关的 kg-mcp-server 已清理」说的是 MCP 记录被删了，但工具没跟着走——这次补上。

清理前后：平台工具 **50 → 43**，`is_available=false` 的条目 **6 → 0**。

备份：`artifacts/platform_tool_tables_backup_20261106.sql`（`pg_dump --data-only`，两张工具表全量）。

### 5.2 查过但不需要清的

| 对象 | 实际情况 |
| --- | --- |
| 智能体 | 列表 5 个：`workbench_main`（平台内建）+ `urban_deduction_agent`(3) / `urban_collab_agent`(4) / `urban_llm_decision_agent`(37) / `urban_control_no_tools`(38)，无重复。软删除的 `equipment_decision_agent`(2)、`urban_setup_smoke`(39) 不在任何列表里 |
| Agent 版本 | 每个 Agent 只有 1 个 `RELEASED` 版本，草稿历史没落库 |
| 技能 | 2 个（skill_id 2/3），都是我们的，无重复上传 |
| 草稿「重复绑定」 | 看着像 27 条，实际 14 条有效 + 13 条 `delete_flag='Y'` 的历史，**不是真重复**（我先前的判断有误，已核实） |
| 悬空绑定 | 指向已删工具的 `enabled` 绑定：**0 条** |

### 5.3 两处仍与实情不符（未动，怕连带破坏证据）

1. **`urban_control_no_tools`(38) 的描述**仍写「仅解绑 14 个 MCP 工具」。改描述要重新发布 → v1→v2，而 A/B 对照证据 JSON 绑的是那次运行，不值得为一句话换版本号。
2. **主 Agent 已发布 v19 早于 `doc_search`**：平台上看到的主 Agent 只有 2 个启用工具（这正是设计），但 v19 的发布快照里没有检索工具。要让评委在 Agent 配置页看到检索工具，得重新发布成 v20，会连带 42 处 `v19` 与 27 处 `conversation_id=120` 的引用，且必须重跑真机 8/8 才敢说没坏。

## 6 主 Agent 为什么不挂 `doc_search`

`MAIN_TOOLS` 收窄到 2 个是有原因的（`setup_nexent.py:83-89` 注释）：工具列表才是模型真正读的提示词，挂 5 个以上会绕过 `event_deduce` 去乱调，随后被 `parallel_executor` 截断在 `<|tool_call_start|>`。

`doc_search` 的适用场景是「**手上没有事件号**，只有自然语言问题」，与主 Agent「给事件号 → 两步式推演」的流程正交。挂上去会提高绕过推演的概率，而 8/8 一致性是现在最硬的证据。

所以：**检索能力以独立入口提供，不进主 Agent 的编排**。这一点在 `competition-gap-audit.md` 考点③ 里已写明，不藏。

## 7 复现命令

```bash
# 1) 建业务资产索引（台账驱动，约 19 份资产）
PYTHONPATH=src uv run --locked python -m decision_poc.rag build --registered --embed hash --out artifacts/rag_index_assets

# 2) 直接用工具验闭环
PYTHONPATH=src uv run --locked python -c "
from decision_poc.mcp_server import UrbanToolkit
t = UrbanToolkit()
r = t._doc_search({'query': '基坑开挖 暴雨 职责冲突', 'top_k': 3})
print(r.data['hit_count'], [h['evidence_ref'] for h in r.data['hits']])
"

# 3) 容器重建后确认平台同步
docker exec urban-twin-mcp python -c "import json,urllib.request;print(json.loads(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).read())['tools_count'])"
```
