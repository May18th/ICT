# P0 接口冻结与骨架基线

状态：**已完成**（2026-10-29）。冻结后如需变更，必须提交变更说明并经评审，否则下游（P2–P8）不得改动。

## 1 八层架构与代码落点

| 层 | 职责 | 代码落点 | 阶段 |
| --- | --- | --- | --- |
| 数据资产层 | 预案/公告/工单/施工计划/气象/管线图/资源表 | `data_asset.py`（已存在）+ `governance.py`（新增治理字段） | P1 完成 |
| 认知加工层 | 解析、切片、表格结构化、图像标注、实体关系抽取 | `data_asset.py`（结构摘要）+ `ontology_bootstrap.py` | P2 |
| 知识资产层 | 本体、图谱、版本、冲突、证据 | `ontology.py` + 待建 `semantic.py` | P3–P4 |
| Nexent 编排层 | MCP Tools + Skills + Agent | `tools.py` + 待建 `mcp_server.py`；`nexent_*` 传输层已就绪 | P5–P6 |
| 推演层 | 影响分析、资源冲突、跨部门处置推演 | 待建 `deduction.py` | P6 |
| 溯源层 | 来源、版本、定位、处理器版本、变更记录 | 待建 `provenance.py` | P4 |
| Demo 层 | 事件输入、影响范围、推理路径、证据卡片 | 待建 `demo/` | P7 |
| 评测层 | 解析/检索/多跳/引用/时延/失败案例 | `evaluation.py` + `baseline.py` | P8 |

新模块一律放在 `src/decision_poc/` 下，不新开顶层包——避免 import 路径分裂与重复依赖。

## 2 冻结项

### 2.1 标识规则（`governance.stable_id`）

| 规则 | 取值 | 说明 |
| --- | --- | --- |
| 格式 | `<prefix>-<slug>-<hash8>` | 例 `req-R-101-6b91476e` |
| prefix | `req` / `trc` / `clm` / `dec` / `evt` | `ID_PREFIXES`，新增须评审 |
| 确定性 | 同输入同输出 | 跨进程、跨机器一致；保证 Demo 与评测可复现 |
| 校验 | `validate_id(value, kind=...)` | 前缀与字符集同时校验 |
| 知识版本 | `kb-v<major>.<minor>.<patch>` | 按段数值比较，禁止字典序 |

禁止：用 `uuid4()` / 时间戳 / 递增计数生成对外 id。

### 2.2 错误码（`governance.ErrorCode`，封闭枚举）

| 码 | 语义 | 可重试 |
| --- | --- | --- |
| `VALIDATION_FAILED` | 输入不合法 | 否 |
| `NOT_FOUND` | 实体/资产不存在 | 否 |
| `PERMISSION_DENIED` | 越权访问 | 否 |
| `EVIDENCE_INSUFFICIENT` | 证据不足，需转人工 | 否 |
| `EVIDENCE_CONFLICT` | 证据冲突，需人工裁定 | 否 |
| `KNOWLEDGE_EXPIRED` | 知识已过期 | 否 |
| `UPSTREAM_UNAVAILABLE` | 上游不可用 | 是 |
| `NOT_IMPLEMENTED` | 能力未实现 | 是 |
| `NO_SCRIPTED_RESULT` | 脚本化结果缺失（沿用） | 是 |
| `DECISION_INVALID` | 决策输出校验失败（沿用） | 是 |

新增错误码属于接口变更。`make_tool_error()` 是唯一构造入口，`retryable` 不得手写。

### 2.3 返回信封

全部 MCP 工具返回既有 `ToolResult`（`status` / `data` / `error` / `stats` / `context`），不新增信封类型。`status` 仅允许 `ok` / `partial` / `error`，且 `error` 与 `status` 强一致（`error` 必须非空当且仅当 `status=error`）。

### 2.4 资产治理字段映射

任务单要求的字段与 `DataAsset` 的对应关系（`governance.GOVERNANCE_FIELD_MAP`）：

| 任务单字段 | 落点 |
| --- | --- |
| `asset_id` | `DataAsset.asset_id`（`AS-<scope>-<type>-<hash8>`） |
| `asset_type` | `AssetGovernance.asset_type`；`DataAsset.doc_type` 为落地名 |
| `title` | `AssetGovernance.title` |
| `source_uri` / `source_org` | `AssetGovernance` 同名 |
| `version` | `DataAsset.version`；知识版本另用 `KnowledgeVersion` |
| `published_at` / `valid_from` / `valid_to` | `AssetGovernance` 同名，必须带时区 |
| `visibility` | `AssetGovernance.visibility`，空表示公开 |
| `content_hash` | `AssetGovernance.content_hash` ≡ `DataAsset.checksum_sha256` |
| `status` | `AssetGovernance.status`（draft/published/deprecated） |
| `locator` | `DataAsset.locator`，证据引用写作 `asset_id#locator` |

`DataAsset` 不改造（已有测试与下游消费），治理字段以组合方式附加，`manifest.json` 中与资产同层输出。

## 3 健康检查

```bash
$env:PYTHONPATH = "src"
python -m decision_poc.health                      # 无数据集也返回 ok
python -m decision_poc.health --dataset samples/assets/urban
```

检查项：Python 版本、决策契约往返、ToolResult 信封、治理 Schema、错误码词表、数据集（缺失仅告警）、离线声明。退出码：`fail` 时为 1，其余为 0。

## 4 P0 验收对照

| 验收项 | 结果 |
| --- | --- |
| 建立目录与运行环境 | `src/decision_poc/`、`.venv`（Python 3.12.14）就绪 |
| 固定公共 Schema 与错误码 | `governance.py` + 10 个封闭错误码，`tests/test_governance.py` 覆盖 |
| 固定 id 与知识版本规则 | `stable_id` / `validate_id` / `KnowledgeVersion`，含确定性断言 |
| 空数据可启动 | `health_check()` 无数据集返回 `ok`；指定不存在的目录降级为 `degraded` |
| Schema 校验可运行 | 12 项测试通过 |

## 5 下游约束

1. 新增模块必须通过 `governance` 的 id 与错误码，不得自造。
2. 任何返回给上层的结果必须能被 `ToolResult` 校验。
3. 时间字段一律带时区；区间判定统一半开区间 `[valid_from, valid_to)`。
4. 权限过滤必须在排序与截断之前执行。
