# 事件实体重投影与绑定表

由 `python -m decision_poc.scene_binding --markdown <path>` 生成，**不要手改**。

- 事件：`WO-20261029-007` @ `2026-10-29T09:40:00+08:00`（片区 A-03）
- 投影指纹：`proj-de40f14cbc813e83`
- 真源哈希：scene `d102aa807648` / layout `438d4cc786ba` / replay `d102aa807648`
- 实体 26 个（全部可画）/ 未落到几何 3 个 / 本事件会被高亮 17 个
- 回放高亮 id 20 个：可画 17 + 不可画 3（不允许静默丢）

## 1 主跳分布（primary_hop = 回放里出现过的最后一跳）

| hop | 对象数 |
| --- | --- |
| `observed` | 1 |
| `conflict` | 2 |
| `resource` | 10 |
| `action` | 4 |

## 2 实体 ↔ 几何 ↔ Actor tag

| twin_id | kind | primary_hop | 几何 | Actor tag（`twin:`/`kind:`/`state:`） | 证据行 |
| --- | --- | --- | --- | --- | --- |
| `TW-drainage-DF-IN-101` | drainage | `resource` | facilities:DF-IN-101 | twin:TW-drainage-DF-IN-101 / kind:intake / state:clogged / evidence:A-03/drainage_facility/v1#row:3 | A-03/drainage_facility/v1#row:3 |
| `TW-drainage-DF-IN-103` | drainage | `resource` | facilities:DF-IN-103 | twin:TW-drainage-DF-IN-103 / kind:intake / state:in_service / evidence:A-03/drainage_facility/v1#row:5 | A-03/drainage_facility/v1#row:5 |
| `TW-drainage-DF-IN-201` | drainage | `resource` | facilities:DF-IN-201 | twin:TW-drainage-DF-IN-201 / kind:intake / state:in_service / evidence:A-03/drainage_facility/v1#row:7 | A-03/drainage_facility/v1#row:7 |
| `TW-drainage-DF-IN-202` | drainage | `resource` | facilities:DF-IN-202 | twin:TW-drainage-DF-IN-202 / kind: intake / state:in_service / evidence:A-03/drainage_facility/v1#row:8 | A-03/drainage_facility/v1#row:8 |
| `TW-drainage-PL-DRN-101` | drainage | `resource` | pipelines:PL-DRN-101 | twin:TW-drainage-PL-DRN-101 / kind:drainage / evidence:A-03/pipeline_ledger/v1#row:2 | A-03/pipeline_ledger/v1#row:2 |
| `TW-drainage-PL-DRN-102` | drainage | `resource` | pipelines:PL-DRN-102 | twin:TW-drainage-PL-DRN-102 / kind:drainage / evidence:A-03/pipeline_ledger/v1#row:3 | A-03/pipeline_ledger/v1#row:3 |
| `TW-drainage-PL-DRN-201` | drainage | `resource` | pipelines:PL-DRN-201 | twin:TW-drainage-PL-DRN-201 / kind:drainage / evidence:A-03/pipeline_ledger/v1#row:8 | A-03/pipeline_ledger/v1#row:8 |
| `TW-gas-PL-GAS-101` | gas | `conflict` | pipelines:PL-GAS-101 | twin:TW-gas-PL-GAS-101 / kind:gas / evidence:A-03/pipeline_ledger/v1#row:4 | A-03/pipeline_ledger/v1#row:4 |
| `TW-gas-PL-GAS-201` | gas | `-` | pipelines:PL-GAS-201 | twin:TW-gas-PL-GAS-201 / kind:gas / evidence:A-03/pipeline_ledger/v1#row:10 | A-03/pipeline_ledger/v1#row:10 |
| `TW-incident-WO-20261029-007` | incident | `observed` | water:WO-20261029-007 | twin:TW-incident-WO-20261029-007 / kind:water / state:30cm / evidence:A-03/work_order/v1#row:2 | A-03/work_order/v1#row:2 |
| `TW-incident-WO-20261029-011` | incident | `-` | water:WO-20261029-011 | twin:TW-incident-WO-20261029-011 / kind:water / state:18cm / evidence:A-03/work_order/v1#row:6 | A-03/work_order/v1#row:6 |
| `TW-power-PL-PWR-101` | power | `conflict` | pipelines:PL-PWR-101 | twin:TW-power-PL-PWR-101 / kind:power / evidence:A-03/pipeline_ledger/v1#row:5 | A-03/pipeline_ledger/v1#row:5 |
| `TW-power-PL-PWR-201` | power | `-` | pipelines:PL-PWR-201 | twin:TW-power-PL-PWR-201 / kind:power / evidence:A-03/pipeline_ledger/v1#row:11 | A-03/pipeline_ledger/v1#row:11 |
| `TW-pump_station-DF-PS-01` | pump_station | `resource` | facilities:DF-PS-01 | twin:TW-pump_station-DF-PS-01 / kind:pump_station / state:fault_pending_repair / evidence:A-03/drainage_facility/v1#row:2 | A-03/drainage_facility/v1#row:2 |
| `TW-pump_station-DF-PS-02` | pump_station | `resource` | facilities:DF-PS-02 | twin:TW-pump_station-DF-PS-02 / kind:pump_station / state:in_service / evidence:A-03/drainage_facility/v1#row:4 | A-03/drainage_facility/v1#row:4 |
| `TW-pump_station-DF-PS-03` | pump_station | `resource` | facilities:DF-PS-03 | twin:TW-pump_station-DF-PS-03 / kind:pump_station / state:in_service / evidence:A-03/drainage_facility/v1#row:6 | A-03/drainage_facility/v1#row:6 |
| `TW-road-R-101` | road | `action` | roads:R-101 | twin:TW-road-R-101 / kind:road / state:closed / evidence:A-03/road_network/v1.1#row:2 | A-03/road_network/v1.1#row:2 |
| `TW-road-R-102` | road | `-` | roads:R-102 | twin:TW-road-R-102 / kind:road / state:normal / evidence:A-03/road_network/v1.1#row:3 | A-03/road_network/v1.1#row:3 |
| `TW-road-R-103` | road | `action` | roads:R-103 | twin:TW-road-R-103 / kind:road / state:under_construction / evidence:A-03/road_network/v1.1#row:4 | A-03/road_network/v1.1#row:4 |
| `TW-road-R-104` | road | `-` | roads:R-104 | twin:TW-road-R-104 / kind:road / state:normal / evidence:A-03/road_network/v1.1#row:5 | A-03/road_network/v1.1#row:5 |
| `TW-road-R-105` | road | `action` | roads:R-105 | twin:TW-road-R-105 / kind:road / state:under_construction / evidence:A-03/road_network/v1.1#row:6 | A-03/road_network/v1.1#row:6 |
| `TW-road-R-201` | road | `action` | roads:R-201 | twin:TW-road-R-201 / kind:road / state:under_construction / evidence:A-03/road_network/v1.1#row:7 | A-03/road_network/v1.1#row:7 |
| `TW-road-R-202` | road | `-` | roads:R-202 | twin:TW-road-R-202 / kind:road / state:normal / evidence:A-03/road_network/v1.1#row:8 | A-03/road_network/v1.1#row:8 |
| `TW-telecom-PL-TEL-103` | telecom | `-` | pipelines:PL-TEL-103 | twin:TW-telecom-PL-TEL-103 / kind:telecom / evidence:A-03/pipeline_ledger/v1#row:6 | A-03/pipeline_ledger/v1#row:6 |
| `TW-water-PL-WTR-103` | water | `-` | pipelines:PL-WTR-103 | twin:TW-water-PL-WTR-103 / kind:water / evidence:A-03/pipeline_ledger/v1#row:7 | A-03/pipeline_ledger/v1#row:7 |
| `TW-water-PL-WTR-202` | water | `-` | pipelines:PL-WTR-202 | twin:TW-water-PL-WTR-202 / kind:water / evidence:A-03/pipeline_ledger/v1#row:9 | A-03/pipeline_ledger/v1#row:9 |

## 3 高亮了但场景里没有几何（`unrepresented`）

| twin_id | kind | 出现步 | 原因 |
| --- | --- | --- | --- |
| `TW-incident-WO-20261026-013` | incident | 1,2,3 | scene_layout.json 的 water 段只导出本事件水面，历史事件无几何可绑 |
| `TW-incident-WO-20261029-009` | incident | 1,2,3 | scene_layout.json 的 water 段只导出本事件水面，历史事件无几何可绑 |
| `TW-work_site-CP-2026-041` | work_site | 1,2,3 | scene_layout.json 没有 work_sites 段，施工面没有几何可绑 |

## 4 旧 3D 命名空间处置（24 个）

旧实体一律 `twin_id: null`（不伪造映射）；`osm_ref` 只记录它当初绑定的真实 OSM 元素。

| legacy_id | kind | osm_ref | 处置 |
| --- | --- | --- | --- |
| `ACT-CLOSE-ROAD` | action | - | retired |
| `AREA-DONGTUO` | heritage | osm:way/1204383950 | retired |
| `AREA-HOUZIMEN` | park | osm:way/239424751 | retired |
| `AREA-MOHECHI` | park | osm:way/993208555 | retired |
| `AREA-RENMIN` | park | osm:way/103295076 | retired |
| `BLD-A03` | district | - | retired |
| `BLD-ART` | building | osm:way/430059676 | retired |
| `BLD-MUSEUM` | building | osm:way/929666443 | retired |
| `BLD-THEATRE` | building | osm:way/815834575 | retired |
| `BLK-PORTAL` | obstruction | - | retired |
| `CF-ROADCTRL` | conflict_point | - | retired |
| `DEPT-EMERG` | department | - | retired |
| `FAC-PS-01` | pump_station | - | retired |
| `OBS-FLOOD` | observation | - | retired |
| `RD-DONGHUAMEN` | road | osm:way/115625961 | retired |
| `RD-DONGYU` | road | osm:way/837186660 | retired |
| `RD-RENMINZHONG` | road | osm:way/459057751 | retired |
| `RD-SHUNCHENG` | road | osm:way/1369175166 | retired |
| `RD-XIYU` | road | osm:way/672331734 | retired |
| `RES-AMB` | resource | - | retired |
| `RES-FIRE` | resource | - | retired |
| `RES-PUMP-TEAM` | resource | - | retired |
| `TN-TIANFU` | tunnel | osm:way/211820797 | retired |
| `WTR-FLOOD` | flood_water | - | retired |

## 5 口径声明

- 底图为真实 OSM 几何，只作背景；事件层（积水、封路、施工、设施状态）为合成内容。
- 场景布局是示意图坐标，不是测绘坐标：实体位置只能用于本场景的可视化，不能用于距离测量。
- 被高亮但无几何的对象列在 unrepresented 里，渲染时应当显示为'画面上没有'，而不是假装亮过。
- 片区（region）在关卡里是地面 Actor，只有 district: 标签、没有 twin: 标签，不参与高亮。
- 本表不含时间戳：同一份 scene/layout/replay 重跑逐位一致，projection_hash 可当场核对。
