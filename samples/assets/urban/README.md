# 最小城市内涝数据集（P1）

**全部为合成数据，不含任何真实市政信息、真实单位或个人数据。** 事件与文件编号均为虚构。

## 文件清单

| 文件 | 类型 | 模态 | 用途 |
| --- | --- | --- | --- |
| `road_network.csv` | road_network | 表格 | 7 条道路（A-03 五条 + B-01 两条），含高程、排水分区；R-101 为片区最低点 |
| `pipeline_ledger.csv` | pipeline_ledger | 表格 | 8 条管线，含埋深/管径/权属；PL-DRN-101 管径不足、PL-GAS-101 为施工对象 |
| `drainage_facilities.csv` | drainage_facility | 表格 | 6 处泵站与雨水口；DF-PS-01 故障且 `owner_org` 为空（职责空白来源），DF-PS-03 为 B-01 正常泵站 |
| `construction_plans.csv` | construction_plan | 表格 | 3 个在建项目；CP-2026-041 开挖 2.5m，CP-2026-038 占用 R-103 |
| `work_orders.csv` | work_order | 表格 | 5 条工单：A-03 积水 30cm、B-01 积水 18cm、泵站故障、基坑沉陷、路面沉陷 |
| `weather_alerts.csv` | weather_alert | 表格 | A-03 橙色 + B-01 黄色两条现行预警，另有一条已解除黄色预警（过期证据） |
| `department_duties.csv` | duty_list | 表格 | 8 个部门职责；封路权限交叉、泵站职责缺失均在此 |
| `resource_inventory.csv` | resource_inventory | 表格 | 9 条资源（按片区存放）；抽水泵 A-03 4 台 + B-01 2 台，是资源缺口与跨区调配的关键数 |
| `plan_flood_emergency_v2.md` | emergency_plan | 文本 | 现行预案：封路 25cm、基坑停工、先隔离风险源 |
| `plan_flood_emergency_v1.md` | emergency_plan | 文本 | 已废止预案：封路 30cm、抽排优先（冲突/过期测试） |
| `procedure_waterlogging.md` | procedure | 文本 | 内部：移动泵折算 400 m³/h、泵站故障按 80% 折算 |
| `procedure_gas_protection.md` | procedure | 文本 | 内部：基坑浸泡检测频次、管道悬空处理 |
| `notice_road_closure.md` | public_notice | 文本 | 封路公告：绕行建议指向正在施工的 R-103 |
| `notice_gas_construction.md` | public_notice | 文本 | 燃气施工公告：开挖 2.5m、检测 2 小时/次 |
| `pipeline_map.png` | schematic_map | 图片 | 800×520 管线示意图，标注积水区与泵站位置 |
| `manifest.json` | — | — | 由导入脚本生成，勿手改 |

关于"3 张地图/管线示意图或结构化替代数据"：本数据集提供 1 张示意图 + 2 份结构化替代（`road_network.csv`、`pipeline_ledger.csv`），后两者是可查询、可引用的事实来源，示意图只用于展示。

## 治理字段

每个资产在 `manifest.json` 中带 `governance`：

`asset_type` / `title` / `source_uri` / `source_org` / `published_at` / `valid_from` / `valid_to` / `visibility` / `content_hash` / `status`

- `content_hash` 与 `DataAsset.checksum_sha256` 恒等，同一文件两次导入指纹一致。
- `visibility` 为空表示公开；`procedure_*.md` 限定为内部（按 `org_id` 授权）。
- `status=deprecated` 的 v1 预案 `valid_to=2025-06-01`，任何时点查询都应被过滤。

## 复现

```bash
# 重新生成示意图（纯标准库，字节级可复现）
python samples/generate_urban_map.py

# 重新登记资产并生成 manifest.json
$env:PYTHONPATH = "src"
python -m decision_poc.urban_dataset

# 核对
python -m decision_poc.health --dataset samples/assets/urban
```

预期：`assets=15 types=12 modalities={'image': 1, 'table': 8, 'text': 6}`。

## 设计说明

数据集刻意埋了四类"不完美"，因为演示价值来自系统能否正确处理它们：

1. **职责空白**：泵站无人认领 —— 推演必须标记冲突，而不是随机指派一个部门。
2. **权限交叉**：道路中心与交警都能封路 —— 推演必须给出"联合发布"而非单一主责。
3. **资源不足**：抽水泵需求 5 台、库存 4 台 —— 必须量化缺口并给优先级。
4. **信息过期与误导**：废止预案阈值更高、公告绕行路线本身在施工 —— 引用旧版或照抄公告都会得出错误结论。
5. **跨区取舍**：A-03 与 B-01 同时受灾但严重程度不同，抽水泵库存分处两地且跨区调用需 40 分钟 —— 这是反事实「资源不足时优先保哪个片区」的物质基础。
