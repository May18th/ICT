"""微调度台取数层：把"一条演示链路"需要的五份数据一次算清。

为什么要单独一层
----------------
页面需要的东西已经散在四处：推演在 `/api/demo`、复核在 `/api/collab-review`、
缺口测算在 `deduction.resource_gaps`、台账在 CSV 里。各自都能取，但谁也回答不了
"这次演示到底走了哪几步"。

本模块只做三件事，边界写死：

1. **只派生**——链路每一环的 `source` 都写明读的是哪个字段，不新算数字；
2. **读同一份台账**——工单与资源直接读 `samples/assets/urban/*.csv`，
   与推演引擎同一份文件、同一行号证据，不另建一份"展示用数据"；
3. **算得出来的才写**——封路只影响路网到场时间，不影响移动泵缺口，
   这一点在数据里就是事实，页面必须原样说，不许包装成"封路后缺口变化"。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .allocation import AllocationPolicy, AllocationUnavailable, decide
from .deduction import DistrictSignal
from .timeline import SKILL_TOOL
from .urban_dataset import DEFAULT_DATASET_ROOT

#: 内部 skill → 对外 MCP 工具名。chain 与 timeline 各维护一份必然会漂，直接复用后者。
SKILL_TOOL_MAP = {skill: tool for skill, (tool, _actor) in SKILL_TOOL.items()}

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"

#: 工单类型 → 会被这条工单消耗的主资源。用于把"工单列表"和"资源状态"对上，
#: 不让页面出现两张互不相干的表。
ORDER_RESOURCE = {
    "waterlogging": "RES-PUMP-M",
    "pump_fault": "RES-PUMP-M",
    "drainage_inspection": "RES-PUMP-M",
    "ground_settlement": "RES-SAND",
    "gas_protection": "RES-GAS-DETECT",
    "road_subsidence": "RES-BARRIER",
    "power_coordination": "RES-SIGN",
}

ORDER_TYPE_LABEL = {
    "waterlogging": "道路积水",
    "pump_fault": "泵组故障",
    "drainage_inspection": "排水巡检",
    "ground_settlement": "基坑沉陷",
    "gas_protection": "燃气保护",
    "road_subsidence": "路面沉陷",
    "power_coordination": "电力协调",
}

#: 演示用的"备选方案"策略参数：P9 报告里进化后的最终策略（weight_closure=1 /
#: weight_hazard=0.25 / transfer_penalty=0.1）。固化成常量是为了让"策略变了
#: → 分配变了"这一条可复现，不是现场手调。
ALTERNATIVE_POLICY: dict[str, Any] = {
    "policy_version": "v4",
    "weight_closure": 1.0,
    "weight_hazard": 0.25,
    "transfer_penalty": 0.1,
}


def _root(root: Path | str | None = None) -> Path:
    return Path(root) if root else Path(DEFAULT_DATASET_ROOT)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    # utf-8-sig：台账首列带 BOM，用 utf-8 读会把第一列名读成 "\ufeffresource_id"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


# --- 工单 -----------------------------------------------------------------


def load_work_orders(
    root: Path | str | None = None,
    *,
    event_id: str = EVENT_ID,
    only_open: bool = False,
) -> list[dict[str, Any]]:
    """读工单台账。`is_event` 标记演示事件，页面据此把它排在第一位并高亮。"""
    rows = _read_rows(_root(root) / "work_orders.csv")
    orders: list[dict[str, Any]] = []
    for row in rows:
        status = (row.get("status") or "").strip()
        if only_open and status != "open":
            continue
        order_type = (row.get("order_type") or "").strip()
        orders.append(
            {
                "order_id": row.get("order_id", ""),
                "order_type": order_type,
                "order_type_label": ORDER_TYPE_LABEL.get(order_type, order_type),
                "road_id": row.get("road_id", ""),
                "facility_id": row.get("facility_id", ""),
                "pipeline_id": row.get("pipeline_id", ""),
                "reported_at": row.get("reported_at", ""),
                "reporter_org": row.get("reporter_org", ""),
                "severity": row.get("severity", ""),
                "status": status,
                "description": row.get("description", ""),
                "resource_id": ORDER_RESOURCE.get(order_type, ""),
                "is_event": row.get("order_id") == event_id,
            }
        )
    # 演示事件置顶，其余按上报时间倒序——评委第一眼看到的就是本次事件
    orders.sort(key=lambda item: (not item["is_event"], item["reported_at"]), reverse=False)
    return orders


# --- 资源 -----------------------------------------------------------------


def build_resource_board(
    payload: Mapping[str, Any] | None = None,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """资源状态：台账可用量 + 本次推演的缺口测算，两列并排放。

    只列被本次推演引用到的资源在前，其余台账资源在后——评委先看"缺什么"，
    再看"还有什么可用"，避免一屏 7 行台账里找不到那 2 台缺口。
    """
    rows = _read_rows(_root(root) / "resource_inventory.csv")
    lines = ((payload or {}).get("resources") or {}).get("lines") or []
    by_id = {str(item.get("resource_id")): item for item in lines}

    items: list[dict[str, Any]] = []
    for row in rows:
        resource_id = row.get("resource_id", "")
        gap_line = by_id.get(resource_id)
        items.append(
            {
                "resource_id": resource_id,
                "name": row.get("name", ""),
                "category": row.get("category", ""),
                "unit": row.get("unit", ""),
                "qty_available": int(row.get("qty_available") or 0),
                "owner_org": row.get("owner_org", ""),
                "district": row.get("district", ""),
                "storage_site": row.get("storage_site", ""),
                "capacity_note": row.get("capacity_note", ""),
                "used_in_event": gap_line is not None,
                "required": (gap_line or {}).get("required"),
                "committed": (gap_line or {}).get("available"),
                "gap": (gap_line or {}).get("gap"),
                "basis": list((gap_line or {}).get("basis") or []),
                "evidence_refs": list((gap_line or {}).get("evidence_refs") or []),
            }
        )

    items.sort(key=lambda item: (not item["used_in_event"], item["resource_id"]))
    used = [item for item in items if item["used_in_event"]]
    return {
        "event_id": (payload or {}).get("event_id") or EVENT_ID,
        "items": items,
        "summary": {
            "total_rows": len(items),
            "used_in_event": len(used),
            "required": sum(int(item.get("required") or 0) for item in used),
            "committed": sum(int(item.get("committed") or 0) for item in used),
            "gap": sum(int(item.get("gap") or 0) for item in used),
            "unit": (used[0].get("unit") if used else "") or "",
        },
        "source": "samples/assets/urban/resource_inventory.csv + deduction.resource_gaps",
    }


# --- 方案对比 -------------------------------------------------------------


def _plan_view(policy: AllocationPolicy, inputs: Sequence[Mapping[str, Any]], snapshot_hash: str) -> dict[str, Any]:
    # `decision_inputs` 是 dict，但 `decide()` 要的是 DistrictSignal：先把键收敛成契约字段，
    # 多的键丢掉而不是硬透传，免得推演层以后加字段时这里静默变成"多一个参数"。
    keys = ("district", "depth_cm", "closure_required", "has_hazard", "demand", "local_available", "world_required")
    signals = [DistrictSignal(**{key: item[key] for key in keys if key in item}) for item in inputs]
    decision = decide(signals, policy, snapshot_hash=snapshot_hash)
    return {
        "policy_version": decision.policy_version,
        "params": policy.model_dump(mode="json"),
        "per_district": [
            {
                "district": line.district,
                "demand": line.demand,
                "local_available": line.local_available,
                "allocated": line.allocated,
                "unmet": line.unmet,
                "critical": line.critical,
            }
            for line in decision.lines
        ],
        "transfers": [item.model_dump(mode="json") for item in decision.transfers],
        "priority_order": list(decision.priority_order),
        "total_demand": decision.total_demand,
        "total_allocated": decision.total_allocated,
        "total_unmet": decision.total_unmet,
        "escalation": list(decision.escalation),
    }


def build_plan_alternatives(
    payload: Mapping[str, Any],
    *,
    alternative: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """当前方案（v1：各片区先用本区库存）与备选方案（v4 进化策略）并排。

    两套方案都由同一份 `deduction.decision_inputs` 现算，差额来自**策略参数**而不是
    换了数据——所以"为什么换了方案"只有一个原因，答辩时可以指着参数说清楚。
    """
    deduction = payload.get("deduction") or {}
    inputs = deduction.get("decision_inputs") or {}
    if not inputs:
        raise AllocationUnavailable("本次推演没有产出 decision_inputs，不编一份空方案")
    ordered = [inputs[key] for key in sorted(inputs)]
    snapshot_hash = str(payload.get("snapshot_hash") or "")

    current = _plan_view(AllocationPolicy(policy_version="v1"), ordered, snapshot_hash)
    alt = _plan_view(
        AllocationPolicy(**dict(alternative or ALTERNATIVE_POLICY)), ordered, snapshot_hash
    )

    current_by = {item["district"]: item for item in current["per_district"]}
    diff: list[dict[str, Any]] = []
    for item in alt["per_district"]:
        before = current_by.get(item["district"]) or {}
        delta = int(item["allocated"]) - int(before.get("allocated") or 0)
        if delta:
            diff.append(
                {
                    "district": item["district"],
                    "current": before.get("allocated"),
                    "alternative": item["allocated"],
                    "delta": delta,
                    "current_unmet": before.get("unmet"),
                    "alternative_unmet": item["unmet"],
                }
            )

    statements: list[str] = []
    if diff:
        for item in diff:
            statements.append(
                f"{item['district']} 分配 {item['current']} → {item['alternative']} 台"
                f"（{item['delta']:+d}），未满足 {item['current_unmet']} → {item['alternative_unmet']} 台"
            )
    else:
        statements.append("两套策略下分配结果一致，本事件不产生方案差异")
    if current["total_unmet"] == alt["total_unmet"]:
        statements.append(f"总缺口不变，仍为 {current['total_unmet']} 台")
    else:
        statements.append(
            f"总缺口 {current['total_unmet']} → {alt['total_unmet']} 台"
        )
    if current["priority_order"] != alt["priority_order"]:
        statements.append(
            f"优先序 {' > '.join(current['priority_order'])} → {' > '.join(alt['priority_order'])}"
        )
    if alt["transfers"]:
        statements.append(
            "备选方案发生跨区调拨："
            + "、".join(f"{t['from_district']}→{t['to_district']} {t['qty']} 台" for t in alt["transfers"])
        )

    return {
        "event_id": payload.get("event_id") or EVENT_ID,
        "current": current,
        "alternative": alt,
        "diff": diff,
        "statements": statements,
        "source": "两份方案都由 deduction.decision_inputs 现算，仅策略参数不同",
    }


# --- 场景实体 -------------------------------------------------------------


def _layout(root: Path | str | None = None) -> dict[str, Any]:
    path = _root(root) / "scene_layout.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _project(bounds: Mapping[str, Any], width: int, height: int):
    """把布局坐标（米）线性投影到 SVG 画布，y 轴翻转（SVG 向下为正）。"""
    low = bounds.get("min") or [0, 0, 0]
    high = bounds.get("max") or [1, 1, 0]
    min_x, min_y = float(low[0]), float(low[1])
    max_x, max_y = float(high[0]), float(high[1])
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    def point(x: float, y: float) -> tuple[float, float]:
        return (
            round((x - min_x) / span_x * width, 1),
            round((max_y - y) / span_y * height, 1),
        )

    return point


def build_scene_entities(
    payload: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    width: int = 700,
    height: int = 330,
) -> dict[str, Any]:
    """把场景布局转成可点实体，每个实体挂上它对应的工单、资源与证据。

    `links` 是"点了以后看什么"的唯一来源：页面不再自己猜哪张工单对应哪个积水点。
    """
    layout = _layout(root)
    bounds = layout.get("bounds") or {}
    point = _project(bounds, width, height)
    orders = load_work_orders(root, event_id=str(payload.get("event_id") or EVENT_ID))
    resources = build_resource_board(payload, root=root)
    resource_by_id = {item["resource_id"]: item for item in resources["items"]}

    def orders_for(**criteria: str) -> list[str]:
        hits = []
        for order in orders:
            for key, value in criteria.items():
                if value and order.get(key) == value:
                    hits.append(order["order_id"])
                    break
        # 去重但保序
        seen: set[str] = set()
        return [item for item in hits if not (item in seen or seen.add(item))]

    def resource_links(order_ids: Iterable[str]) -> list[str]:
        ids: list[str] = []
        for order in orders:
            if order["order_id"] not in order_ids:
                continue
            rid = order.get("resource_id")
            if rid and rid not in ids:
                ids.append(rid)
        return [rid for rid in ids if rid in resource_by_id]

    entities: list[dict[str, Any]] = []

    for district in layout.get("districts") or []:
        cx, cy = point(float(district["center"]["x"]), float(district["center"]["y"]))
        entities.append(
            {
                "id": district.get("district_id", ""),
                "kind": "district",
                "label": district.get("district_id", ""),
                "shape": "circle",
                "cx": cx,
                "cy": cy,
                "r": round(float(district.get("radius_m") or 0) / float(bounds.get("max", [1])[0] or 1) * width, 1),
                "state": "area",
                "links": {"work_orders": [], "resources": [], "evidence": []},
                "detail": f"片区 {district.get('district_id', '')}",
            }
        )

    for road in layout.get("roads") or []:
        polyline = road.get("polyline") or []
        if len(polyline) < 2:
            continue
        points = " ".join(
            f"{point(float(item['x']), float(item['y']))[0]},{point(float(item['x']), float(item['y']))[1]}"
            for item in polyline
        )
        linked = orders_for(road_id=str(road.get("road_id") or ""))
        state = str(road.get("state") or "normal")
        entities.append(
            {
                "id": road.get("road_id", ""),
                "kind": "road",
                "label": f"{road.get('name', '')}（{'已封' if state != 'normal' else '通行'}）",
                "shape": "line",
                "points": points,
                "state": state,
                "links": {
                    "work_orders": linked,
                    "resources": resource_links(linked),
                    "evidence": [str(road.get("evidence") or "")] if road.get("evidence") else [],
                },
                "detail": (
                    f"{road.get('name', '')} · {road.get('road_class', '')}"
                    f" · {road.get('length_m', '-')} m · 状态 {state}"
                ),
            }
        )

    for facility in layout.get("facilities") or []:
        xy = facility.get("xy") or {}
        if "x" not in xy:
            continue
        cx, cy = point(float(xy["x"]), float(xy["y"]))
        linked = orders_for(facility_id=str(facility.get("facility_id") or ""))
        entities.append(
            {
                "id": facility.get("facility_id", ""),
                "kind": "facility",
                "label": facility.get("name", ""),
                "shape": "circle",
                "cx": cx,
                "cy": cy,
                "r": 6,
                "state": str(facility.get("state") or ""),
                "links": {
                    "work_orders": linked,
                    "resources": resource_links(linked),
                    "evidence": [str(facility.get("evidence") or "")] if facility.get("evidence") else [],
                },
                "detail": (
                    f"{facility.get('name', '')} · {facility.get('facility_type', '')}"
                    f" · 设计能力 {facility.get('capacity_m3h', '-')} m³/h · 状态 {facility.get('state', '')}"
                ),
            }
        )

    for water in layout.get("water") or []:
        polygon = water.get("polygon") or []
        if len(polygon) < 3:
            continue
        points = " ".join(
            f"{point(float(item['x']), float(item['y']))[0]},{point(float(item['x']), float(item['y']))[1]}"
            for item in polygon
        )
        incident = str(water.get("incident_id") or "")
        linked = orders_for(order_id=incident)
        entities.append(
            {
                "id": incident,
                "kind": "water",
                "label": f"{water.get('district', '')} 积水 {water.get('depth_cm', '-')} cm",
                "shape": "polygon",
                "points": points,
                "state": "incident" if incident == payload.get("event_id") else "water",
                "links": {
                    "work_orders": linked,
                    "resources": resource_links(linked),
                    "evidence": [str(water.get("evidence") or "")] if water.get("evidence") else [],
                },
                "detail": (
                    f"积水点 {incident} · 深度 {water.get('depth_cm', '-')} cm"
                    f" · 所在道路 {water.get('road_id', '-')}"
                ),
            }
        )

    return {
        "view": {"width": width, "height": height, "bounds": bounds, "units": layout.get("units", "m")},
        "entities": entities,
        "counts": {
            kind: len([item for item in entities if item["kind"] == kind])
            for kind in ("district", "road", "facility", "water")
        },
        "source": "samples/assets/urban/scene_layout.json（引擎无关布局，非测绘坐标）",
    }


# --- 演示链路 -------------------------------------------------------------


def build_chain(
    payload: Mapping[str, Any],
    *,
    collab: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    orders: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """七环一条链：事件输入 → 主 Agent → MCP 调用 → 协同复核 → 冲突 → 方案 → 复算。

    每一环都带 `actor` 与 `tool`，页面据此画出"谁做的、用什么做的"；
    没有 `tool` 的环节（事件输入、复算）不硬填一个，避免把所有步骤都说成工具调用。
    """
    facts = payload.get("facts") or {}
    deduction = payload.get("deduction") or {}
    audit = payload.get("audit") or {}
    gaps = deduction.get("resource_gaps") or []
    gap_total = sum(int(item.get("gap") or 0) for item in gaps)
    unit = str((gaps[0].get("unit") if gaps else "") or "")

    event_order = next(
        (item for item in (orders or []) if item.get("is_event")), None
    )
    event_text = (
        f"{facts.get('event_id', '-')} {facts.get('district', '-')} 积水 {facts.get('depth_cm', '-')} cm"
        f"，响应等级 {facts.get('response_level', '-')}"
    )
    if event_order:
        event_text += f"；工单来源 {event_order.get('reporter_org', '-')}「{event_order.get('description', '')}」"

    tools = []
    for step in payload.get("steps") or []:
        tool = SKILL_TOOL_MAP.get(str(step.get("skill") or ""), str(step.get("skill") or ""))
        if tool and tool not in tools:
            tools.append(tool)
    if collab and collab.get("status") == "ok" and "collab_run" not in tools:
        tools.append("collab_run")

    stages = [
        {
            "id": "event_input",
            "title": "事件输入",
            "actor": "值班室",
            "tool": None,
            "summary": event_text,
            "facts": [
                f"观测时间 {facts.get('observed_at', '-')}",
                f"数据缺口 {len(facts.get('data_gaps') or [])} 条",
                f"过期证据 {facts.get('expired_evidence', 0)} 条",
            ],
            "refs": [f"A-03/work_order/v1#row:2"],
            "source": "facts / work_orders.csv",
        },
        {
            "id": "main_agent",
            "title": "主 Agent 分析",
            "actor": "urban_deduction_agent",
            "tool": "event_deduce",
            "summary": (
                f"六跳推演完成，跳状态："
                + "、".join(
                    f"{item.get('stage')}={item.get('status')}" for item in deduction.get("stages") or []
                )
            ),
            "facts": [
                f"受影响对象 {len((payload.get('impact') or {}).get('affected') or [])} 个",
                f"命中规则覆盖 {sum((facts.get('rule_coverage') or {}).values())} 条",
                f"门到门到场时间不计算（缺坐标），只算路口到路口",
            ],
            "refs": [str(payload.get("snapshot_hash") or "")[:16]],
            "source": "deduction.stages / facts",
        },
        {
            "id": "mcp_call",
            "title": "MCP 工具调用",
            "actor": "主 Agent",
            "tool": "、".join(tools) if tools else None,
            "summary": f"{len(payload.get('steps') or [])} 次 skill 执行，落到 {len(tools)} 个工具",
            "facts": [
                f"{item.get('skill')} → {SKILL_TOOL_MAP.get(str(item.get('skill') or ''), '-')}"
                f"（{item.get('elapsed_ms', '-')} ms）"
                for item in payload.get("steps") or []
            ],
            "refs": [],
            "source": "steps",
        },
        {
            "id": "collab_review",
            "title": "协同 Agent 复核",
            "actor": "urban_collab_agent",
            "tool": "collab_run",
            "summary": (
                "未执行复核" if not (collab and collab.get("status") == "ok")
                else (
                    f"动作卡 {(collab.get('counts') or {}).get('cards', 0)} 张，"
                    f"覆盖 {(collab.get('counts') or {}).get('agents', 0)} 个部门，"
                    f"交接 {(collab.get('counts') or {}).get('handoffs', 0)} 次，"
                    f"需会签 {(collab.get('counts') or {}).get('co_sign_pairs', 0)} 对"
                )
            ),
            "facts": (
                []
                if not (collab and collab.get("status") == "ok")
                else [
                    f"内核指纹 {str(collab.get('kernel_fingerprint') or '')[:12]}",
                    f"规则版本 {collab.get('rules_version', '-')}",
                    f"规则冲突 {(collab.get('counts') or {}).get('violations', 0)} 处（复核不改六跳与缺口）",
                ]
            ),
            "refs": [],
            "source": "collab.orchestrate",
        },
        {
            "id": "conflict",
            "title": "资源冲突识别",
            "actor": "推演引擎",
            "tool": "conflict_check",
            "summary": (
                f"冲突 {len(deduction.get('conflicts') or [])} 类"
                f"，缺口 {gap_total} {unit}"
            ),
            "facts": [
                f"{item.get('conflict_id')} {item.get('kind')}：{item.get('subject')}"
                for item in deduction.get("conflicts") or []
            ],
            "refs": [],
            "source": "deduction.conflicts / resource_gaps",
        },
        {
            "id": "plan",
            "title": "调度方案生成",
            "actor": "推演引擎",
            "tool": "allocation_plan",
            "summary": (
                "当前方案 "
                + "、".join(
                    f"{item['district']} {item['allocated']}/{item['demand']} 台"
                    for item in (plan or {}).get("current", {}).get("per_district", [])
                )
                if plan
                else f"处置动作 {len(deduction.get('action_order') or [])} 条"
            ),
            "facts": [
                f"处置顺序第 {index + 1}：{text}"
                for index, text in enumerate((deduction.get("action_order") or [])[:3])
            ],
            "refs": [],
            "source": "allocation.decide / deduction.action_order",
        },
        {
            "id": "recalc",
            "title": "结果复算",
            "actor": "证据审计",
            "tool": "evidence_locate",
            "summary": (
                f"引用完整率 {audit.get('citation_completeness', '-')}"
                f"，账本链 {'完整' if audit.get('chain_ok') else '不可信'}"
                f"，{'可发布' if payload.get('publishable') else '需人工复核'}"
            ),
            "facts": [
                f"快照哈希 {str(payload.get('snapshot_hash') or '')[:16]}",
                f"知识版本 {payload.get('knowledge_version', '-')}",
                f"缺口只由库存与折算决定，封路不改缺口（见场景页说明）",
            ],
            "refs": [],
            "source": "audit / publishable",
        },
    ]

    for index, stage in enumerate(stages):
        stage["seq"] = index + 1
        stage["status"] = (
            "ok"
            if stage["id"] != "recalc" or payload.get("publishable")
            else "needs_review"
        )

    return {
        "event_id": payload.get("event_id") or EVENT_ID,
        "as_of": payload.get("as_of") or AS_OF,
        "stages": stages,
        "counts": {"stages": len(stages), "tools": len(tools)},
        "tools": tools,
        "source": "七环全部派生自同一份 payload + collab-review，展示层不新算数字",
    }


__all__ = [
    "ALTERNATIVE_POLICY",
    "AS_OF",
    "EVENT_ID",
    "ORDER_RESOURCE",
    "ORDER_TYPE_LABEL",
    "build_chain",
    "build_plan_alternatives",
    "build_resource_board",
    "build_scene_entities",
    "load_work_orders",
]
