"""City-infrastructure domain profile: the only place domain knowledge lives.

Everything generic (freeze, confidence model, merge plan) stays in `ontology.py`;
this module supplies the seed schema, the table column aliases, the dtypes and
the asset-type -> entity-type routing for the waterlogging scenario.

Type names follow `docs/ICT-赛题3-...任务单.md` §4.2 exactly (12 types). Work
orders are modelled as `Incident` nodes — an order *is* an incident record, and
inventing a 13th type would drift from the brief.
"""

from __future__ import annotations

from .ontology import OntologySchema, OntologyType, RelationSchema

CITY_SCHEMA_VERSION = "city-1"

CITY_SCHEMA = OntologySchema(
    version=CITY_SCHEMA_VERSION,
    types=[
        OntologyType(
            name="Road",
            description="城市道路路段，带高程与排水分区",
            seeds=["路", "街", "大道", "road"],
        ),
        OntologyType(
            name="Area",
            description="行政或排水分区，道路与设施的归属范围",
            seeds=["片区", "分区", "district"],
        ),
        OntologyType(
            name="DrainageFacility",
            description="雨水泵站、雨水口等排水设施",
            seeds=["泵站", "雨水口", "pump", "intake"],
        ),
        OntologyType(
            name="Pipeline",
            description="地下管线：排水、燃气、电力、通信、供水",
            seeds=["管线", "管道", "pipeline", "gas", "drainage"],
        ),
        OntologyType(
            name="ConstructionProject",
            description="占道或开挖施工项目，含计划工期与开挖深度",
            seeds=["施工", "开挖", "construction"],
        ),
        OntologyType(
            name="WeatherEvent",
            description="气象预警事件，含等级、雨量与有效期",
            seeds=["预警", "暴雨", "weather", "alert"],
        ),
        OntologyType(
            name="Incident",
            description="工单记录：积水、泵站故障、基坑沉陷等",
            seeds=["工单", "积水", "故障", "incident", "order"],
        ),
        OntologyType(
            name="Department",
            description="责任部门及其职责范围",
            seeds=["公司", "中心", "支队", "局", "department"],
        ),
        OntologyType(
            name="Resource",
            description="应急资源：抽水泵、围挡、沙袋、发电车等",
            seeds=["抽水泵", "围挡", "沙袋", "resource"],
        ),
        OntologyType(
            name="EmergencyPlan",
            description="应急预案或作业规程，带版本与有效期",
            seeds=["预案", "规程", "指引", "plan", "procedure"],
        ),
        OntologyType(
            name="Action",
            description="处置动作，按顺序构成处置链",
            seeds=["先", "再", "后", "停止", "封闭", "抽排"],
        ),
        OntologyType(
            name="Risk",
            description="二次风险：管道悬空、泄漏、拥堵等",
            seeds=["风险", "悬空", "泄漏", "risk"],
        ),
    ],
    relations=[
        RelationSchema(
            name="located_in",
            domain=[
                "Road",
                "Pipeline",
                "DrainageFacility",
                "ConstructionProject",
                "Incident",
                "Resource",
                "WeatherEvent",
            ],
            range=["Area"],
            description="实体属于某个片区（预警也按片区发布）",
        ),
        RelationSchema(
            name="connected_to",
            domain=["Pipeline"],
            range=["Pipeline", "Road"],
            description="管线与管线/道路的空间邻接",
        ),
        RelationSchema(
            name="affected_by",
            domain=["Road", "Pipeline", "DrainageFacility", "ConstructionProject"],
            range=["WeatherEvent", "Incident"],
            description="设施受某事件影响",
        ),
        RelationSchema(
            name="under_construction",
            domain=["Pipeline", "Road"],
            range=["ConstructionProject"],
            description="管线或道路处于施工状态",
        ),
        RelationSchema(
            name="responsible_for",
            domain=["Department"],
            range=["Road", "Pipeline", "DrainageFacility", "ConstructionProject", "Resource"],
            description="部门对该对象负运维责任",
        ),
        RelationSchema(
            name="requires_resource",
            domain=["ConstructionProject", "Incident", "Road"],
            range=["Resource"],
            description="处置该对象需要某类资源",
        ),
        RelationSchema(
            name="covered_by_plan",
            domain=["Road", "Pipeline", "DrainageFacility", "ConstructionProject", "Incident", "Department", "Action"],
            range=["EmergencyPlan"],
            description="对象或动作有预案/规程依据",
        ),
        RelationSchema(
            name="conflicts_with",
            domain=["Department", "EmergencyPlan", "Pipeline"],
            range=["Department", "EmergencyPlan", "Pipeline"],
            description="显式冲突：职责重叠、版本矛盾、空间冲突",
        ),
        RelationSchema(
            name="precedes",
            domain=["Action"],
            range=["Action"],
            description="处置动作的先后顺序",
        ),
        RelationSchema(
            name="supported_by",
            domain=["Incident", "Action", "Risk"],
            range=["EmergencyPlan"],
            description="结论或动作的证据支撑来源",
        ),
    ],
)


# asset_type -> 实体类型；None 表示该资产只提供证据不产出实体（如示意图）
ASSET_ENTITY_TYPE: dict[str, str | None] = {
    "road_network": "Road",
    "pipeline_ledger": "Pipeline",
    "drainage_facility": "DrainageFacility",
    "construction_plan": "ConstructionProject",
    "work_order": "Incident",
    "weather_alert": "WeatherEvent",
    "duty_list": "Department",
    "resource_inventory": "Resource",
    "emergency_plan": "EmergencyPlan",
    "procedure": "EmergencyPlan",
    "public_notice": None,
    "schematic_map": None,
}

# 每类表的主键列，用作实体名称
ID_COLUMN: dict[str, str] = {
    "road_network": "road_id",
    "pipeline_ledger": "pipeline_id",
    "drainage_facility": "facility_id",
    "construction_plan": "plan_id",
    "work_order": "order_id",
    "weather_alert": "alert_id",
    "duty_list": "org_id",
    "resource_inventory": "resource_id",
}

# 规范列名 -> 可接受的中文/变体表头（真实市政数据一定是中文表头）
ALIASES: dict[str, tuple[str, ...]] = {
    "road_id": ("道路编号", "路段编号"),
    "name": ("名称", "道路名称", "设施名称", "资源名称"),
    "district": ("片区", "行政区", "所属片区"),
    "road_class": ("道路等级", "等级"),
    "lanes": ("车道数", "机动车道数"),
    "length_m": ("长度", "路段长度(m)"),
    "elevation_m": ("高程", "路面高程(m)"),
    "drainage_zone": ("排水分区", "雨水系统"),
    "pipeline_id": ("管线编号", "管段编号"),
    "kind": ("管线类型", "专业"),
    "depth_m": ("埋深", "埋深(m)"),
    "diameter_mm": ("管径", "管径(mm)"),
    "material": ("材质",),
    "install_year": ("敷设年份", "建设年份"),
    "owner_org": ("权属单位", "运维单位", "责任单位"),
    "facility_id": ("设施编号", "泵站编号"),
    "facility_type": ("设施类型",),
    "capacity_m3h": ("能力", "抽排能力(m3/h)", "额定流量"),
    "backup_power": ("备用电源",),
    "last_inspection": ("最近巡检", "上次巡检"),
    "state": ("状态", "运行状态"),
    "status": ("工单状态", "处置状态", "台账状态"),
    "plan_id": ("计划编号", "施工编号"),
    "project_name": ("项目名称", "工程名称"),
    "work_type": ("作业类型", "施工类型"),
    "excavation_depth_m": ("开挖深度", "开挖深度(m)"),
    "start_at": ("开始时间", "计划开始"),
    "end_at": ("结束时间", "计划结束"),
    "permit_org": ("审批单位", "许可单位"),
    "contractor": ("施工单位",),
    "closure_scope": ("占道范围", "封闭方式"),
    "order_id": ("工单编号",),
    "order_type": ("工单类型", "事件类型"),
    "reported_at": ("上报时间", "报告时间"),
    "reporter_org": ("上报单位",),
    "severity": ("严重程度", "等级"),
    "alert_id": ("预警编号",),
    "issued_at": ("发布时间",),
    "level": ("预警等级", "等级"),
    "rainfall_mm_3h": ("3小时雨量", "雨量(mm)"),
    "valid_from": ("生效时间", "起始时间"),
    "valid_to": ("失效时间", "结束时间"),
    "org_id": ("部门编号", "单位编号"),
    "org_name": ("部门名称", "单位名称"),
    "scope_type": ("职责类型", "范围类型"),
    "scope_ref": ("职责范围", "范围值"),
    "authority": ("权限", "职责内容"),
    "contact": ("联系电话",),
    "escalation_note": ("备注", "升级说明"),
    "resource_id": ("资源编号",),
    "category": ("类别", "资源类别"),
    "qty_available": ("可用数量", "库存数量"),
    "unit": ("单位",),
    "storage_site": ("存放地点", "仓库"),
    "capacity_note": ("能力说明", "规格"),
    "description": ("描述", "详情", "事件描述", "预警内容"),
    "source_org": ("来源单位", "发布单位", "数据来源"),
    "note": ("说明", "备注"),
}

FLOAT_COLUMNS = frozenset(
    {"length_m", "elevation_m", "depth_m", "excavation_depth_m", "capacity_m3h", "rainfall_mm_3h"}
)
INT_COLUMNS = frozenset({"lanes", "diameter_mm", "install_year", "qty_available"})
DATETIME_COLUMNS = frozenset(
    {"start_at", "end_at", "reported_at", "issued_at", "valid_from", "valid_to", "last_inspection"}
)
UNITS: dict[str, str] = {
    "length_m": "m",
    "elevation_m": "m",
    "depth_m": "m",
    "excavation_depth_m": "m",
    "diameter_mm": "mm",
    "capacity_m3h": "m3/h",
    "rainfall_mm_3h": "mm/3h",
    "qty_available": "unit",
}


def alias_index() -> dict[str, str]:
    """raw header (normalised) -> canonical column name."""
    index: dict[str, str] = {}
    for canonical, synonyms in ALIASES.items():
        index[canonical] = canonical
        for synonym in synonyms:
            index[synonym] = canonical
    return index
