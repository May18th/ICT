# P2 回传报告：数据资产认知加工层

- 交付日期：2026-10-29
- 对应任务单：[p2-cognition-task-order.md](../tasks/p2-cognition-task-order.md)
- 结论：**T1–T5 全部完成，153 项测试通过（既有 117 + 新增 36）**

## 1 代码变更说明

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `ontology.py` | **改动** | 仅参数化：`build_bundle(assets_dir=..., schema=...)`；`_freeze` 改为公开 `freeze_candidates` 并保留 `_freeze` 别名。设备运维域行为未变 |
| `city_profile.py` | 新增 | 12 类种子本体、10 条关系、表头别名表、dtype 与单位映射。领域知识只在此处 |
| `chunking.py` | 新增 | 标题感知切片，`locators: list[str]`（Q3 决策），同文合并保留多处定位 |
| `table_normalizer.py` | 新增 | 表头规范化、类型转换、源行号定位、未识别列显式上报 |
| `region_annotation.py` | 新增 | 图片区域标注，bbox 越界即拒绝 |
| `adapters.py` | 新增 | 按后缀路由；PDF/DOCX/XLSX 返回 `metadata_only`（Q1 决策） |
| `city_extract.py` | 新增 | 城市域候选抽取基线 + 已废止资产降权 |
| `samples/assets/urban/annotations.json` | 新增 | 5 个区域人工标注（Q2 决策） |

未改动：`data_asset.py`、`contracts.py`、`tool` 信封。

## 2 测试结果

```
154 passed in 2.85s
```

新增 37 项：`test_chunking.py` 7、`test_table_normalizer.py` 9、`test_region_annotation.py` 8、`test_city_extract.py 12`，另有 1 项并入既有回归。既有 117 项全绿，`ontology.py` 设备域输出未变。

## 3 资产解析统计

| 文件 | 类型 | 状态 | chunk | 表行 | 区域 |
| --- | --- | --- | --- | --- | --- |
| road_network.csv | road_network | full | 0 | 7 | 0 |
| pipeline_ledger.csv | pipeline_ledger | full | 0 | 8 | 0 |
| drainage_facilities.csv | drainage_facility | full | 0 | 6 | 0 |
| construction_plans.csv | construction_plan | full | 0 | 3 | 0 |
| work_orders.csv | work_order | full | 0 | 5 | 0 |
| weather_alerts.csv | weather_alert | full | 0 | 3 | 0 |
| department_duties.csv | duty_list | full | 0 | 8 | 0 |
| resource_inventory.csv | resource_inventory | full | 0 | 9 | 0 |
| plan_flood_emergency_v2.md | emergency_plan | full | 8 | 0 | 0 |
| plan_flood_emergency_v1.md | emergency_plan | full | 6 | 0 | 0 |
| procedure_waterlogging.md | procedure | full | 5 | 0 | 0 |
| procedure_gas_protection.md | procedure | full | 5 | 0 | 0 |
| notice_road_closure.md | public_notice | full | 5 | 0 | 0 |
| notice_gas_construction.md | public_notice | full | 5 | 0 | 0 |
| pipeline_map.png | schematic_map | partial | 0 | 0 | 5 |

合计：15 资产、8 表 49 行、34 chunk、5 区域；`metadata_only` 为 0。

## 4 候选抽取统计

| 指标 | 值 |
| --- | --- |
| 实体 / 关系 | 62 / 100 |
| auto_accept / review / drop | 58 / 4 / 0 |
| 实体类型覆盖 | 11 / 12（Risk 未抽取，属 P6） |
| 证据覆盖率 | 100%（模型层强制 `min_length=1`） |

关系分布：`located_in` 41、`responsible_for` 25、`connected_to` 8、`affected_by` 8、`covered_by_plan` 7、`under_construction` 5、`precedes` 5、`conflicts_with` 1。

## 5 抽查样例（可回源核对）

**切片**（预案 v2 §5）
- locator：`A-03/emergency_plan/v2.0#line:44-47`
- 回源第 44–47 行：`5.1 先隔离风险源…` 至 `5.4 全程同步交通管制与信息发布。` —— 完全一致
- heading_path：`["城区防汛排涝应急预案（2025 年修订）", "5 处置顺序"]`，未跨标题

**表行**（管线台账）
- locator：`A-03/pipeline_ledger/v1#row:5`
- 回源第 5 行：`PL-PWR-101,power,R-101,1.2,150,PVC,1998,ORG-POWER,in_service,为 DF-PS-01 供电`
- 类型化结果：`pipeline_id=PL-PWR-101`、`depth_m=1.2`（float）

**图片区域**（管线示意图）
- locator：`A-03/schematic_map/v1#region:250,245,220,30`
- label `积水区`，bbox 落在 800×520 内，标注者 `manual:v1`

## 6 未识别列与告警

- 未识别列：**0**（`description`、`source_org` 已补别名）。机制本身由单测 `test_unmapped_columns_are_reported_not_dropped` 覆盖。
- 告警：1 条 —— `存在已废止资产：其候选已降权至人工复核区间，不得自动采纳`。
- schema 校验告警：0。

## 7 硬断言验证（任务单 §5）

| 断言 | 结果 |
| --- | --- |
| 定位可解析，覆盖率 100% | 通过（chunk/row/region 三类均回源核对） |
| DF-PS-01 无 `responsible_for` | 通过，职责空白保持 |
| 废止预案不得 auto_accept | 通过：预案 v1 置信度 0.64 → `review`，其 3 个处置动作 0.62 → `review` |
| 封路权限交叉被检出 | 通过：`ORG-ROAD conflicts_with ORG-TRAFFIC`，置信度 0.84 |
| 处置顺序成链 | 通过：`先隔离风险源 → 再控制水势 → 后续动作`，5 条 `precedes` |
| 确定性 | 通过：两次运行统计一致，chunk 哈希序列一致 |

## 8 待确认问题

1. **Action 命名截断**：为可读性取前 24 字，跨预案重名时补步骤号。若 P6 需要完整语义作推演节点名，建议改为 `步骤号 + 全文首句`。
2. **Risk 实体暂缺**：现由 `docs/urban-scenario-ground-truth.md` 的二次风险表定义，计划 P6 随推演规则一并产出，不单独造实体。
3. **`Area` 仅 1 个**：数据集只有 A-03 一个片区。若 P6 反事实要演示"跨区调配资源"，需在 P1 数据集补第二片区（含独立泵站与资源库存）。
4. **PDF/XLSX**：仍为 `metadata_only`。若评委要求上传真实预案 PDF，需在 P7 前引入 `pypdf`，并同步更新 `pyproject.toml` 与锁文件。
