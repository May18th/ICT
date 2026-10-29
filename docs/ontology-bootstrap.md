# 本体半自动构建方案（低资源场景）

状态：**已实现并跑通**。日期：2026-10-29。对应赛题「设计低资源场景下领域知识图谱本体半自动化构建方案」。

## 1. 为什么是"半自动 + 低资源"

空手起步建本体（人工定类型、人工标实体）成本高且不可复现；全自动抽取（纯 LLM）在没有金标准时不可验证。本方案取中间态：

- **人工只写种子**：10 个类型 + 11 个谓词 + 每类的少量中文词例，约 120 行声明式配置；
- **其余全部由数据诱导**：列名别名表 + 正则模板 + 数值区间匹配，全部确定性、可复现；
- **机器给置信度，人只审"机器拿不准的"**：分档阈值把候选切成自动通过 / 人工复核 / 丢弃，人工工作量被压到量化指标（本样本集 **0.5 条/资产**）。

这正好对上赛题的三个关键词：低资源（种子极小）、半自动（人审增量）、标准对齐（阈值规则绑定到 `ISO 10816-3:2009 §7.2` 这类条款锚点）。

## 2. 三段式管线

```
资产清单 manifest.json
      │
      ├─ ① 种子 SEED_ONTOLOGY      10 类型 / 11 谓词，人工维护
      │
      ├─ ② 诱导 extract_*()        表格列 → 实体；规程文本 → 规则/步骤/条款
      │                            每条候选强制带 Evidence(asset_id, locator, quote)
      │
      └─ ③ 对齐 _freeze()          同名合并 → 谓词域/值域校验 → 置信度分档
                                   → ontology_candidates.json
                                        │
                                        └─ plan_merge(snapshot) → add / merge / conflict
```

入口：`python -m decision_poc.ontology_bootstrap`。

## 3. 种子本体

| 类型 | 父类 | 说明 |
|---|---|---|
| `Equipment` | Asset | 有台账编号、可独立检修的设备 |
| `EquipmentClass` | Concept | 设备类型（分类节点，供同类故障复用） |
| `Line` | Location | 产线/机组，停机决策的最小影响域 |
| `Metric` | Concept | 可测指标及单位 |
| `FaultMode` | Concept | 故障模式，由指标越限或人工判定触发 |
| `ThresholdRule` | Rule | 判定区间 + 处置要求，可跨文档匹配观测值 |
| `Process` | Concept | 一份可执行处置流程 |
| `ProcedureStep` | Concept | 流程中的单步动作 |
| `Standard` | Reference | 外部/厂内标准 |
| `StandardClause` | Reference | 条款，溯源最小锚点 |

谓词（含域/值域，构造时强校验，未声明的谓词直接判 `drop`）：

| 谓词 | 域 | 值域 |
|---|---|---|
| `located_in` | Equipment | Line |
| `classified_as` | Equipment | EquipmentClass |
| `measured_by` | Equipment | Metric |
| `exhibits` | Equipment | FaultMode |
| `triggered_by` | FaultMode | ThresholdRule |
| `defined_in` | ThresholdRule | StandardClause |
| `part_of` | StandardClause / ProcedureStep | Standard / Process |
| `applies_to` | Process | Equipment |
| `has_step` | Process | ProcedureStep |
| `cites` | ProcedureStep | StandardClause |
| `precedes` | ProcedureStep | ProcedureStep |

种子只描述"这个行业长什么样"，不含任何具体设备数据；换行业只需改这一段。

## 4. 抽取器与基础置信度

| 抽取器 | 输入特征 | 产出 | 基础置信度 |
|---|---|---|---|
| `declared_standard_ref` | 资产记录里声明的 `standard_refs` | Standard / StandardClause / `part_of` | 0.95 |
| `ledger_table` | 表头含 `设备编号` + `设备名称` | Equipment（+ 属性）、Line、EquipmentClass | 0.92 / 关系 0.86 |
| `observation_table` | 表头含 `巡检时间` + `设备编号` | Metric、`measured_by`、数值观测 | 0.92 / 关系 0.86 |
| `rule_table` | Markdown 表含 `判定`/`处置` 且某列可识别为指标 | ThresholdRule（区间/判定/处置） | 0.90 |
| `procedure_step` | 「步骤」小节下的编号条目 | ProcedureStep、`cites`、`precedes` | 0.88 |
| `procedure_prose` | 正文中的标准号 + 条款号 | Standard、StandardClause、Process | 0.84 |
| `threshold_prose` | `85°C 为报警值` 这类句式 | ThresholdRule（阈值） | 0.84 |
| `cross_document_rule_match` | 观测值 × 另一文档的区间规则 | FaultMode、`exhibits`、`triggered_by` | 0.80 |

**分档**：`≥ 0.80` 自动通过；`0.55 ~ 0.80` 人工复核；`< 0.55` 丢弃。

未识别表头不会被静默丢弃：任何"非标识列且有数值"的列都会成为 `Metric`，单位从列名后缀切分（`振动mm/s` → 振动 + `mm/s`），所以新增一张表不需要改代码。

## 5. 跨文档多跳（赛题「决策链路可溯源」的实证）

规程里解析区间规则，巡检记录里解析数值观测，两者在 `cross_document_rule_match` 里相遇：

```
观测  巡检记录 #row:6   2026-10-29 06:00 A-101 振动=7.8mm/s
规则  检修规程 #line:19  D | > 7.1 | 危险 | 立即降负荷并安排停机检查
  ↓
FaultMode「振动D区超限」
  attributes: observed_value=7.8, zone=D, verdict=危险,
              recommended_action=立即降负荷并安排停机检查
  evidence:   [巡检记录#row:6, 检修规程#line:19]      ← 两个 asset_id
  ↓
Equipment「主传动减速机」-exhibits-> FaultMode -triggered_by-> ThresholdRule
                                        ThresholdRule -defined_in-> ISO 10816-3:2009 §7.2
```

一条结论可以顺锚点回读到**两份不同资产的具体行号**，且这条链是规则算出来的，不是模型编的。当前样本集共产生 13 条跨资产证据候选。

注意 A 区（"新机正常"）与 B 区（"可长期运行"）**不会**被误判成故障模式——判定词先经过 `NORMAL_HINTS` 过滤，这是"不把正常当异常"的硬闸。

## 6. 动态更新与合并计划

`plan_merge(bundle, snapshot)` 把候选与现有图快照对比，输出四类操作：

| op | 含义 | 动作 |
|---|---|---|
| `add` | 快照中无同名同类型节点 | 直接新增 |
| `merge` | 同名同类型、属性不冲突 | 补属性、补证据 |
| `conflict` | 同名同类型但属性值不同 | **不改图**，人工裁决 |
| `unchanged` | 完全一致 | 跳过 |

快照的生成方式（平台在线时执行）：

```sql
select e.name, t.name as type, e.properties as attributes
from kg.kg_entity e join kg.kg_ontology_type t on t.id = e.type_id;
```

把结果保存为 `samples/assets/kg_snapshot.json`（形如 `{"entities": [...]}`），再跑一次引导命令即会打印合并计划。**平台不在线时该文件不存在，命令只提示"全部按新增处理"，不会伪造快照。**

## 7. 实测指标（本样本集：4 份资产）

| 指标 | 值 | 口径 |
|---|---|---|
| 实体 / 关系 | 43 / 53 | 去重合并后 |
| 自动通过 | 94 | `confidence ≥ 0.80` |
| 人工复核 | 2 | 同名多类型歧义（干燥机） |
| 丢弃 | 0 | — |
| 溯源覆盖率 | 100% | 每条候选都带 `asset_id + locator + quote` |
| 跨资产证据候选 | 13 | 证据涉及 ≥2 个 `asset_id` |
| 类型覆盖 | 100%（10/10） | 种子类型被实际用到的比例 |
| 人工复核量 | 0.5 条/资产 | 复核数 ÷ 资产数 |

判读方式：**溯源覆盖率必须是 100%**，否则等于产出了不可引用的断言；**复核量越低越好**，但不能为 0 而牺牲歧义处理——本样本里 `干燥机` 同时被识别为 `Equipment`（台账行）和 `EquipmentClass`（另一行的设备类型），这类同名多类型一律降级为人工复核，不允许自动入库；它产生的那条自指关系（`干燥机 -classified_as-> 干燥机`）也会被跳过并记录告警，因为自环不带来任何图信息。

## 8. 与 Nexent 的衔接

- 候选 JSON 可直接作为 MCP 工具 `kg_propose` 的返回体（字段已是 `candidate_id / type / attributes / evidence`）；
- `plan_merge` 的四类操作对应 Skill 工作流的四个分支：`add` 走批量入库、`merge` 走属性补全、`conflict` 生成人工工单、`unchanged` 空转；
- 每条候选的 `evidence.locator` 与数据资产规范里的定位符同构（`设备/文档类型/版本#锚点`），因此 Agent 回答可以一路引到原始行。

## 9. 已知边界（答辩时主动说明）

1. **列名别名是人工写的**：`LEDGER_ALIASES` / `OBSERVATION_ALIASES` 只覆盖中文字段名，遇到英文或异体列名需要加映射；未知数值列仍会成为 Metric，不会丢数据。
2. **未接入 LLM**：当前全程确定性规则，好处是可复现、可单测；代价是同义表述（"轴温""轴承发热"）覆盖不到。接 LLM 时应把置信度定在 0.70（落在复核档），而不是直接采信。
3. **图片资产不参与抽取**：PNG 只贡献 `standard_refs` 里的标准证据，没有做 OCR/图像理解。多模态理解的缺口在这，不在本体。
4. **同名多类型只降级不解决**：系统把歧义暴露给人，不做自动消歧（消歧需要上下文，硬猜比暴露更危险）。
5. **没有金标准评测**：`precision / recall` 需要人工标注对照集，当前只报了可自动计算的过程指标（溯源率、类型覆盖、复核量）。

## 10. 复现

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.asset_manifest
uv run --locked python -m decision_poc.ontology_bootstrap
uv run --locked pytest tests/test_ontology.py -q     # 19 项
```

输出：`samples/assets/ontology_candidates.json`（含 `seed` / `stats` / `entities` / `relations` / `warnings`）。
测试覆盖：种子自洽、非法谓词降级、跨文档双资产证据、正常观测不误报、区间解析、步骤顺序、确定性、合并计划四分类。
