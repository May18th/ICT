# ICT 创新赛赛题 3：城市内涝跨部门事件推演

## 1. 项目目标

构建一个可在华为 ModelEngine Nexent 上运行的纯软件智能体，针对“暴雨内涝 + 道路封闭 + 地下管线施工冲突”事件，完成多模态数据资产认知、低资源知识图谱本体半自动构建、MCP + Skills 编排、跨文档多跳推理、跨部门应急处置推演和 AI 数据可信溯源。

## 2. 核心演示问题

> 未来两小时出现 80 mm 暴雨，A 区道路正在进行燃气管线施工，哪些设施和道路会受到影响？哪些部门需要协同？应急资源是否足够？请给出处置顺序和证据链。

系统至少输出：

- 受影响道路、区域、排水设施和地下管线；
- 事件涉及的部门、职责和协同顺序；
- 应急资源需求、库存和缺口；
- 预案冲突、缺失或过期信息；
- 处置优先级和后续风险；
- 每个结论对应的来源文件、版本、证据片段和推理路径。

## 3. 总体架构

```text
数据资产层
  ├─ 政策/预案 PDF、公告、维修工单、施工计划
  ├─ CSV/Excel 资源清单、设施台账、事件记录
  ├─ 地图/管线示意图、现场图片
  └─ 统一元数据、版本、权限、时间有效期、来源
        ↓
认知加工层
  ├─ 文档解析与切片
  ├─ 表格结构化
  ├─ 图片/地图对象标注
  ├─ 实体关系候选抽取
  └─ 本体候选生成与人工确认
        ↓
知识资产层
  ├─ 城市基础设施事件本体
  ├─ 实体、关系、属性和空间关联
  ├─ 证据片段与来源版本
  ├─ 冲突、过期和变更记录
  └─ 图谱查询与版本发布
        ↓
Nexent 编排层
  ├─ MCP：资产检索、图谱查询、证据详情、冲突检测、资源核查
  ├─ Skills：事件理解、影响分析、资源检查、跨部门推演、证据审计
  └─ Agent：按 Skill 顺序调用工具并生成结构化结果
        ↓
展示与评测层
  ├─ 事件输入
  ├─ 影响范围地图/关系图
  ├─ 推理链和证据卡片
  ├─ 部门协同与资源处置清单
  └─ 离线评测、运行日志和回放
```

## 4. 模块边界

### 4.1 data_assets：数据资产管理

职责：导入、校验、解析、切片和登记数据。

统一字段至少包括：

- `asset_id`
- `asset_type`
- `title`
- `source_uri`
- `source_org`
- `version`
- `published_at`
- `valid_from`
- `valid_to`
- `visibility`
- `content_hash`
- `status`

禁止：把未经登记的文本直接送入推理；禁止丢失来源和版本。

### 4.2 ontology：本体与候选知识

核心概念：

- `Road`
- `Area`
- `DrainageFacility`
- `Pipeline`
- `ConstructionProject`
- `WeatherEvent`
- `Incident`
- `Department`
- `Resource`
- `EmergencyPlan`
- `Action`
- `Risk`

核心关系：

- `located_in`
- `connected_to`
- `affected_by`
- `under_construction`
- `responsible_for`
- `requires_resource`
- `covered_by_plan`
- `conflicts_with`
- `precedes`
- `supported_by`

本体构建必须支持：候选生成、人工确认、拒绝、合并、版本发布和回滚。

### 4.3 reasoning：事件推演

输入：事件描述、当前时间、设施/施工/资源状态。

输出 `Event推演结果`，至少包括：

- `incident_id`
- `summary`
- `affected_assets`
- `departments`
- `resource_gaps`
- `action_sequence`
- `risks`
- `evidence_refs`
- `reasoning_paths`
- `knowledge_version`
- `needs_human_review`

安全规则：证据不足、证据冲突、知识过期时，不得伪造确定结论，必须标注不确定性或转人工。

### 4.4 provenance：可信溯源

每个实体、关系、推理结论和 Skill 输出都必须能够回指：

- 来源资产 ID；
- 文档版本；
- 页码、行号、表格单元格或图片区域；
- 抽取时间；
- 处理器/模型版本；
- 变更前后差异；
- 操作和审核记录。

### 4.5 mcp_server：MCP 工具

首批工具接口：

- `search_assets(query, filters, top_k)`
- `get_asset_evidence(asset_id, locator)`
- `query_graph(subject, relation, object, version)`
- `detect_conflicts(scope, version)`
- `check_resources(resource_types, area, time_window)`
- `trace_claim(claim_id)`
- `get_knowledge_diff(from_version, to_version)`

工具必须返回结构化 JSON，并包含 `request_id`、`source_refs`、`knowledge_version` 和错误状态。

### 4.6 skills：Nexent Skills

首批 Skill：

1. `incident-understanding`
2. `impact-analysis`
3. `resource-conflict-check`
4. `cross-department-response`
5. `evidence-audit`

每个 Skill 必须定义：输入 Schema、输出 Schema、调用的 MCP 工具、失败处理、人工介入条件和可复用参数。

### 4.7 demo：可视化展示

至少提供：

- 事件输入页；
- 资产和事件概览；
- 影响范围图；
- 图谱/推理链路；
- 证据卡片；
- 部门协同和资源清单；
- 冲突、过期和人工复核提示；
- 运行记录回放。

## 5. 推荐目录

```text
project/
├─ data/
│  ├─ raw/                 # 原始公开或构造数据
│  ├─ normalized/          # 统一结构数据
│  ├─ annotations/         # 标注和本体确认结果
│  └─ evaluation/          # 评测案例和标准答案
├─ ontology/
│  ├─ schema.yaml
│  ├─ entities.jsonl
│  ├─ relations.jsonl
│  └─ versions/
├─ services/
│  ├─ asset_service/
│  ├─ ontology_service/
│  ├─ reasoning_service/
│  ├─ provenance_service/
│  └─ mcp_server/
├─ nexent/
│  ├─ skills/
│  ├─ prompts/
│  └─ config/
├─ demo/
├─ scripts/
│  ├─ ingest_demo_data.py
│  ├─ build_ontology.py
│  ├─ run_scenario.py
│  ├─ evaluate.py
│  └─ verify.sh
├─ tests/
├─ README.md
└─ docs/
```

## 6. WorkBuddy Todo（执行顺序）

### P0：项目骨架和接口冻结

- [x] 建立目录和 Python/TypeScript 运行环境。
- [x] 固定所有公共 JSON Schema 和错误码。
- [x] 固定 `request_id`、`trace_id`、`knowledge_version` 规则。
- [x] 写最小健康检查和本地启动说明。
- [x] 验收：空数据可以启动；Schema 校验可运行；README 可按步骤启动。

### P1：最小数据集

- [x] 准备一个虚拟城区的道路、区域、管线、排水设施和资源点数据。
- [x] 加入至少 6 份公开来源或构造的预案/公告文档。
- [x] 加入施工计划、维修工单、气象事件和资源库存表。
- [x] 加入 3 张地图/管线示意图或结构化替代数据。
- [x] 为每条数据生成来源、版本、有效期和哈希。
- [x] 验收：一键导入后资产数量、类型和来源可核对。

### P2：数据资产认知

- [x] 实现 PDF/Markdown/CSV/XLSX/图片的导入适配器。
- [x] 实现文本切片、表格标准化和图片区域标注记录。
- [x] 实现实体/关系候选抽取的离线基线。
- [x] 验收：每个候选均能回指原始资产和定位信息。

### P3：本体半自动构建

- [x] 实现本体 Schema 和实体/关系 JSONL 存储。
- [x] 实现候选生成、人工确认、拒绝、合并和版本发布。
- [x] 实现显式冲突关系和过期知识过滤。
- [x] 验收：可从少量样本生成候选本体，并能发布 v1、修改为 v2、回滚到 v1。

### P4：图谱与溯源服务

- [x] 实现实体、关系、空间关联和证据引用查询。
- [x] 实现 `trace_claim` 和 `get_knowledge_diff`。
- [x] 实现数据、知识、模型/处理器和操作日志的关联。
- [x] 验收：任意推理结论可追溯到来源资产和版本。

### P5：MCP Server

- [x] 实现首批 7 个 MCP 工具。
- [x] 为每个工具补充输入输出 Schema、错误状态和权限边界。
- [x] 实现工具调用日志和 `trace_id` 传播。
- [x] 验收：工具可独立调用、返回结构化结果，失败时不返回伪造数据。

### P6：Nexent Skills 与推演链

- [x] 实现 5 个 Skill 的输入输出和工具编排。
- [x] 完成“事件理解 → 影响分析 → 资源检查 → 跨部门推演 → 证据审计”主链路。
- [x] 对证据不足、冲突和过期知识转人工或安全终止。
- [x] 验收：核心事件端到端运行，结果包含影响范围、部门、资源、动作、风险和证据链。

### P7：Demo 与可视化

- [ ] 实现事件输入、结果摘要、影响范围、推理路径、证据卡片和资源清单页面。
- [ ] 增加冲突/过期/人工复核状态展示。
- [ ] 增加一次运行记录的回放功能。
- [ ] 验收：评委无需查看代码即可看懂事件、推理、证据和处置建议。

### P8：评测与材料

- [ ] 建立至少 20 个事件案例，包含正常、证据不足、冲突和过期数据。
- [ ] 评测资产解析、实体关系、检索命中、多跳路径、引用完整率和响应时间。
- [ ] 输出失败案例和改进前后对比。
- [ ] 完成 README、部署说明、数据说明、开源截图和演示脚本。
- [ ] 验收：从干净环境按 README 可复现；评测结果可重复生成。

## 7. WorkBuddy 禁止事项

- 不得把项目改成通用聊天机器人或普通 RAG 问答。
- 不得删除来源、版本、定位和证据字段。
- 不得让前端直接调用 Nexent 或内部模型服务。
- 不得把真实密钥、私有数据或未授权数据提交到仓库。
- 不得把不确定推理包装成确定事实。
- 不得新增行业范围、改变核心事件或替换 Nexent 编排方式而不提交变更说明。
- 不得在没有测试和运行日志的情况下声称任务完成。

## 8. Codex 验收清单

- [ ] 核心事件能从干净环境复现。
- [ ] 五个赛题考点均有可运行证据。
- [ ] 所有推理结论都能回指来源和版本。
- [ ] 本体候选、人工确认、版本发布和回滚可演示。
- [ ] MCP 工具和 Skill 可独立调用并可组合。
- [ ] 冲突、过期、证据不足会安全处理。
- [ ] Demo 展示完整，非代码人员可理解。
- [ ] 测试、评测、README、数据说明和开源材料齐全。
