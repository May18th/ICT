# 本体半自动构建方案（低资源场景）

状态：**已实现并跑通**。对应赛题「设计低资源场景下领域知识图谱本体半自动化构建方案」。

本文只讲一条管线：`src/decision_poc/ontology.py` 里的通用三段式流程，以及它在**城市排水防涝**这一个域上的实例化。同一条管线还挂着一个设备维护域的样例（§9），它只服务于单元测试的通用性验证，**不属于本作品的领域口径**。

## 1. 为什么是"半自动 + 低资源"

空手起步建本体（人工定类型、人工标实体）成本高且不可复现；全自动抽取（纯 LLM）在没有金标准时不可验证。本方案取中间态：

- **人工只写种子**：一份声明式配置，写清"这个域有哪些类型、类型之间允许哪些谓词、域和值域各是什么"；
- **其余全部由数据诱导**：中文表头别名表 + 主键列识别 + 外键连接 + 编号步骤正则，全部确定性、可复现，不依赖模型；
- **机器给置信度，人只审"机器拿不准的"**：分档阈值把候选切成自动通过 / 人工复核 / 丢弃，人工工作量因此可以被量化（本样本集 0.47 条/资产）。

换城市不改代码：种子是配置，别名表是配置，资产类型到实体类型的路由也是配置。

## 2. 三段式管线

```
资产清单 manifest.json（含 asset_type / governance.status）
      │
      ├─ ① 种子   CITY_SCHEMA        13 类型 / 10 谓词，人工维护（city_profile.py）
      │
      ├─ ② 诱导   extract_*()        表格主键 → 实体；外键与职责范围 → 关系；
      │                              预案编号步骤 → 动作与顺序
      │                              每条候选强制带 Evidence(asset_id, locator, quote)
      │
      └─ ③ 对齐   freeze_candidates() 同名合并 → 谓词域/值域校验 → 置信度分档
                                    → candidates.json（entities / relations / stats / warnings）
                                         │
                                         └─ plan_merge(snapshot) → add / merge / conflict / unchanged
```

入口：`python -m decision_poc.city_extract`（城市域）；`python -m decision_poc.ontology_bootstrap`（通用性样例）。

## 3. 城市排水域种子本体

`src/decision_poc/city_profile.py` 的 `CITY_SCHEMA`（版本 `city-1`）。评委关心的"路段 → 雨水口/泵站 → 工单 → 部门职责 → 预案条款"这条链，在类型上落在下面加粗的五个节点。

| 类型 | 说明 | 六层映射 |
|---|---|---|
| **`Road`** | 城市道路路段，带高程、路幅、限速与排水分区 | 路段 |
| `Area` | 行政或排水分区，道路与设施的归属范围 | 空间归属 |
| **`DrainageFacility`** | 雨水泵站、雨水口等排水设施，带抽排能力、泵组台数与运行状态 | 雨水口 / 泵站 |
| `Underpass` | 下穿隧道：入口挡水槛、最低点标高、封控水深阈值 | 易涝点 |
| `Pipeline` | 地下管线：排水、燃气、电力、通信、供水 | 次生风险源 |
| `ConstructionProject` | 占道或开挖施工，含计划工期与开挖深度 | 诱因 |
| `WeatherEvent` | 气象预警事件，含等级、雨量与有效期 | 输入 |
| **`Incident`** | 工单记录：积水、泵站故障、基坑沉陷。工单即事件记录，不另设节点 | 工单 |
| **`Department`** | 责任部门及其职责范围、权限与升级路径 | 部门职责 |
| `Resource` | 应急资源：抽水泵、围挡、沙袋、发电车 | 处置手段 |
| **`EmergencyPlan`** | 应急预案或作业规程，带版本与有效期 | 预案（条款级挂 `supported_by`） |
| `Action` | 处置动作，按顺序构成处置链 | 处置流程 |
| `Risk` | 二次风险：管道悬空、泄漏、拥堵。只在推演阶段表达，**不建图实例** | 风险 |

谓词共声明 10 类，本样本实际用到 8 类（下表按实例化与否标注）：

| 谓词 | 域 | 值域 | 本样本 |
|---|---|---|---|
| `located_in` | 道路/管线/下穿/设施/施工/工单/资源/预警 | Area | 已实例化 |
| `connected_to` | Pipeline | Pipeline / Road | 已实例化 |
| `affected_by` | 道路/管线/下穿/设施/施工 | WeatherEvent / Incident | 已实例化 |
| `under_construction` | Pipeline / Road | ConstructionProject | 已实例化 |
| `responsible_for` | Department | 道路/管线/设施/施工/资源 | 已实例化 |
| `covered_by_plan` | 道路/管线/设施/施工/工单/部门/动作 | EmergencyPlan | 已实例化 |
| `conflicts_with` | Department / EmergencyPlan / Pipeline | 同上 | 已实例化 |
| `precedes` | Action | Action | 已实例化 |
| `requires_resource` | 施工/工单/道路 | Resource | 声明未实例化（资源需求写在属性里，尚未拉成边） |
| `supported_by` | 工单/动作/风险 | EmergencyPlan | 声明未实例化 |

后两条是**已知缺口**，不掩饰：声明了但本样本没有产出边，等有新数据再补，不为了"类型全覆盖"造边。

## 4. 抽取器与基础置信度

全部确定性规则，没有一次模型调用。

| 抽取器 | 输入特征 | 产出 | 基础置信度 |
|---|---|---|---|
| 主键列 | 表头命中 `ID_COLUMN`（如 `泵站编号` / `工单编号`） | 对应类型实体 + 全部属性 | 0.92 |
| 外键连接 | 行内出现另一张表的主键值（`org_id` / `road_id`） | `responsible_for` / `located_in` / `connected_to` | 0.86 |
| 职责范围 | `duty_list` 的 `scope_type` + `scope_ref` | Department -`responsible_for`-> 对象 | 0.92 |
| 事件关联 | 工单/预警的时间与片区重叠 | `affected_by` | 0.86 |
| 预案编号步骤 | `^\d+\.\d+` 形式的条目 | Action 实体 + `precedes` | 0.88 |
| 预案正文 | 正文里点名的部门与设施 | `covered_by_plan` | 0.84 |
| 废止降权 | 资产 `governance.status = deprecated` | 候选置信度 ×0.7，压入人工复核档 | — |

**分档**：`≥ 0.80` 自动通过；`0.55 ~ 0.80` 人工复核；`< 0.55` 丢弃。

中文表头别名表 `ALIASES` 覆盖了 19 份台账里出现过的全部变体（"片区 / 行政区 / 所属片区"、"抽排能力(m3/h) / 额定流量"这类）。遇到没登记的表头不静默丢弃：只要该列是数值列，就成为实体属性；只要该列值命中另一张表的主键，就尝试建边，建不成记进 `warnings`。

## 5. 跨文档与跨版本：一条结论回读两份原文

两条证据链是本方案唯一"不看模型"的硬证据：

**跨资产**——泵站台账给出 `DF-PS-01` 的抽排能力与泵组台数，工单台账给出同编号设施的故障记录，两条证据的 `asset_id` 不同，`located_in` 与 `affected_by` 因此可以在同一次回答里被引到两行原文。

**跨版本**——预案目录里同时存在 `城区防汛排涝应急预案 v1.0（已废止）` 与 `v2.0`。废止件不删：它的候选按 ×0.7 降权后照样入图，于是同一个对象身上挂着两条互相矛盾的 `covered_by_plan`，`conflicts_with` 把矛盾显式表达出来。这正是演示里"同一场雨、两家引不同版本、结论打架"的来源，**不是人为编的桥段，是抽取结果的自然产物**。

```
工单   work_orders #row:12   DF-PS-01 泵站停运 12:40
设施   drainage_facilities #row:5  DF-PS-01 2 台泵组 / 1 台故障 / 能力 1.08 万 m3/h
  ↓
Incident「DF-PS-01 泵站停运」 -affected_by-> WeatherEvent -located_in-> Area
                            -covered_by_plan-> EmergencyPlan(v2.0)
  evidence: [work_orders#row:12, drainage_facilities#row:5,
             emergency_plan_v2#line:88]
```

## 6. 低资源冷启动：手上只有几份市级预案时怎么起步

这是赛题最关心的场景——没有历史台账、没有标注集、只有几份公开发布的预案。按下面四步走，每一步都不需要模型：

| 步 | 动作 | 产出 | 人工量 |
|---|---|---|---|
| 1 | **只读预案立骨架**：抽"组织机构与职责""响应分级""处置流程"三节，人工写 13 个类型名与 10 条谓词（约 120 行配置） | 类型层 + 部门节点 + 预案节点 | 半天 |
| 2 | **条款挂动作**：预案里 `3.2.1` 这类编号条目按 `^\d+\.\d+` 切成 Action，顺序靠编号本身给出 `precedes`；条款里点名的部门与设施自动挂 `covered_by_plan` | 处置链 + 预案依据 | 0（跑脚本） |
| 3 | **来表补别名**：台账到手后只往 `ALIASES` 加中文表头映射，不改抽取逻辑；先接"谁管哪条路"（`duty_list`）与"哪台泵什么状态"（`drainage_facilities`），这两张表就能让 `responsible_for` 与 `affected_by` 活起来 | 实体 + 责任边 | 每张表 10 分钟 |
| 4 | **拿矛盾当验收**：接完必须能在图上搜出至少一处"职责重叠"或"预案版本冲突"。搜不出来说明关系没建对，不是数据太干净 | 可演示的冲突 | 半天 |

冷启动的判据不是"实体抽得多"，而是**第一条跨部门冲突能不能在图上指出来**——能指出，说明类型、谓词、证据三样都对了。

## 7. 动态更新与合并计划

`plan_merge(bundle, snapshot)` 把候选与现有图状态记录对比，输出四类操作：

| op | 含义 | 动作 |
|---|---|---|
| `add` | 状态记录中无同名同类型节点 | 直接新增 |
| `merge` | 同名同类型、属性不冲突 | 补属性、补证据 |
| `conflict` | 同名同类型但属性值不同 | **不改图**，转人工裁决 |
| `unchanged` | 完全一致 | 跳过 |

状态记录的生成方式（平台在线时执行）：

```sql
select e.name, t.name as type, e.properties as attributes
from kg.kg_entity e join kg.kg_ontology_type t on t.id = e.type_id;
```

把结果保存为 `samples/assets/kg_snapshot.json`（形如 `{"entities": [...]}`），再跑一次引导命令即会打印合并计划。**平台不在线时该文件不存在，命令只提示"全部按新增处理"，不伪造状态记录。**

## 8. 实测指标（城市排水域，19 份资产）

| 指标 | 值 | 口径 |
|---|---|---|
| 实体 / 关系 | 74 / 123 | 去重合并后 |
| 候选合计 | 197 | 实体 + 关系 |
| 自动通过 | 188 | `confidence ≥ 0.80` |
| 人工复核 | 9 | 含废止预案降权后的候选 |
| 丢弃 | 0 | — |
| 候选采纳率 | 95.43% | 188 ÷ 197 |
| 溯源覆盖率 | 100% | 每条候选都带 `asset_id + locator + quote` |
| 跨资产证据候选 | 2 | 证据涉及 ≥2 个 `asset_id` |
| 类型覆盖 | 12 / 13 | `Risk` 按设计不建实例 |
| 人工复核量 | 0.47 条/资产 | 复核数 ÷ 资产数 |

判读方式：**溯源覆盖率必须是 100%**，否则等于产出了不可引用的断言；**复核量越低越好，但不能为 0**——本样本里的 9 条复核全部来自"废止预案降权"与"同名多类型歧义"，宁可让人看一眼，也不许自动入库。

注意这两个数不能互相替代：候选采纳率讲的是**质量**（机器判得准不准），人工复核量讲的是**效率**（人要看多少）。赛题问的是效率，答案应该是后者。

## 9. 同一条管线的第二个域实例（通用性验证，非项目领域）

`src/decision_poc/ontology_bootstrap.py` 的 `SEED_ONTOLOGY` 是一份设备维护域的种子（设备 / 产线 / 指标 / 故障模式 / 阈值规则 / 标准条款），配套 `samples/assets/equipment_ledger.csv` 等样例资产，用来验证两件事：

1. 三段式管线不依赖具体领域——换一份种子配置就是另一个域，`freeze_candidates` / `plan_merge` / `bundle_stats` 一行未改；
2. 跨文档多跳（巡检记录 × 检修规程）在另一个域里同样成立，说明"证据回读两行原文"是管线能力，不是城市域的特例。

**边界**：这套样例只跑在单测里（`tests/test_ontology.py`），不参与城市域口径、不进对外材料、不作为本作品的行业归属。本作品是**政务·城市防汛应急**。

## 10. 已知边界（演示时主动说明）

1. **列名别名是人工写的**：`ALIASES` 覆盖中文表头，遇到英文或异体列名要加映射；未知数值列仍会成为属性，不丢数据。
2. **未接入 LLM**：全程确定性规则，好处是可复现、可单测；代价是同义表述（"轴温""轴承发热"这类）覆盖不到。接 LLM 时应把置信度定在复核档，而不是直接采信。
3. **图片资产不参与字段抽取**：示意图只贡献像素级区域候选，不做 OCR、不识别图例文字。
4. **同名多类型只降级不解决**：系统把歧义暴露给人，不自动消歧——消歧需要上下文，硬猜比暴露更危险。
5. **没有金标准**：`precision / recall` 需要人工标注对照集，当前只报可自动计算的过程指标（采纳率、溯源率、类型覆盖、复核量）。实体识别与关系抽取的 F1 **未建基线**，不拿采纳率顶替。
6. **两条谓词声明未实例化**（`requires_resource` / `supported_by`），如实列出，不造边凑覆盖率。

## 11. 复现

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.city_extract            # 城市域：打印候选统计
uv run --locked python -m decision_poc.city_extract --write    # 另存 samples/assets/urban/ontology_candidates.json
uv run --locked python -m decision_poc.ontology_bootstrap      # 通用性样例
uv run --locked pytest tests/test_ontology.py -q               # 19 项
```

候选 JSON 含 `seed` / `stats` / `entities` / `relations` / `warnings` 五段，默认只打印不落盘——写入的产物不入库，需要时现生成。
