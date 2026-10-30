"""把台账变成可渲染的几何：一份 Blender 与 Unreal 共用的布局文件。

三维演示最容易走偏的一步是"两个引擎各算一套坐标"：Blender 里路在这儿，UE 里路
在那儿，对不上之后就只能手工调，调完又和数据脱钩。所以这里只做一件事——把
`road_network.csv` / `pipeline_ledger.csv` / `drainage_facilities.csv` /
`construction_plans.csv` / `work_orders.csv` 的**平面几何**算出来，写成一个
引擎无关的 `scene_layout.json`，两边都只消费它。

坐标系约定（写死，避免各自发挥）：

- 单位米，右手系，Z 向上；`z = 0` 是地平面，路面高度取该路段 `elevation_m`；
- 管线画在 `z = -depth_m`，与地面平行，沿所属道路中线偏移排布；
- 建筑只做"简化体块"：给定底面矩形与高度，不做立面细节；
- 水面是贴在路面上方的半透明多边形，带 `depth_cm`，不做流体。

节点坐标不是测绘数据，是**示意图布局**：从拓扑出发做一次确定性的力导向松弛，边长
目标取真实 `length_m`，再整体归一化。同一份输入必定得到同一份输出（有测试守着），
但它回答的是"这条路在片区的哪个方位、多长"，不是"它在哪条经纬度上"。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from .contracts import StrictModel
from .urban_dataset import DEFAULT_DATASET_ROOT

LAYOUT_VERSION = "urban-twin-layout-v1"
OUTPUT_NAME = "scene_layout.json"

# 车道宽按 3.5m 折算：示范区的路宽不来自台账，这里是显式假设
LANE_WIDTH_M = 3.5
CURB_MARGIN_M = 2.0
BUILDING_GAP_M = 30.0
BUILDING_MIN_CLEARANCE_M = 14.0
MAX_BUILDINGS_PER_DISTRICT = 60
CLOSURE_THRESHOLD_CM = 25.0  # 与 RULE-ACT-CLOSE-ROAD 的 or_min_depth_cm 一致


class LayoutPoint(StrictModel):
    x: float
    y: float


class LayoutRoad(StrictModel):
    road_id: str
    name: str
    district: str
    road_class: str
    state: str  # normal / closed / under_construction
    width_m: float
    z: float
    length_m: float
    polyline: list[LayoutPoint]
    from_node: str
    to_node: str
    evidence: str = ""


class LayoutPipeline(StrictModel):
    pipeline_id: str
    kind: str
    road_id: str
    diameter_mm: int
    depth_m: float
    z: float
    polyline: list[LayoutPoint]
    evidence: str = ""


class LayoutFacility(StrictModel):
    facility_id: str
    name: str
    facility_type: str
    road_id: str
    state: str
    capacity_m3h: int
    footprint_m: float
    xy: LayoutPoint
    z: float
    evidence: str = ""


class LayoutBuilding(StrictModel):
    building_id: str
    district: str
    footprint: list[LayoutPoint]
    height_m: float


class LayoutWater(StrictModel):
    incident_id: str
    district: str
    road_id: str
    depth_cm: float
    z: float
    polygon: list[LayoutPoint]
    evidence: str = ""


class LayoutDistrict(StrictModel):
    district_id: str
    center: LayoutPoint
    radius_m: float


class SceneLayout(StrictModel):
    schema_version: str = LAYOUT_VERSION
    snapshot_hash: str = ""
    units: str = "m"
    notes: str = "示意图布局，非测绘坐标；z=0 为地平面，路面取 elevation_m，管线取 -depth_m"
    bounds: dict[str, list[float]] = Field(default_factory=dict)
    nodes: dict[str, LayoutPoint] = Field(default_factory=dict)
    roads: list[LayoutRoad] = Field(default_factory=list)
    pipelines: list[LayoutPipeline] = Field(default_factory=list)
    facilities: list[LayoutFacility] = Field(default_factory=list)
    buildings: list[LayoutBuilding] = Field(default_factory=list)
    water: list[LayoutWater] = Field(default_factory=list)
    districts: list[LayoutDistrict] = Field(default_factory=list)
    states: dict[str, list[str]] = Field(default_factory=dict)


# --- 读表 ---------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _state_of(row: dict[str, str]) -> str:
    status = (row.get("status") or "normal").strip()
    return "under_construction" if status == "under_construction" else status or "normal"


# --- 节点布局 -----------------------------------------------------------


def _components(nodes: list[str], edges: list[tuple[str, str, float]]) -> list[list[str]]:
    """连通分量：A-03 与 B-01 之间没有路段连接，必须分开摆，否则力导向会把它们推开几十公里。"""
    neighbors: dict[str, list[str]] = {node: [] for node in nodes}
    for a, b, _ in edges:
        neighbors[a].append(b)
        neighbors[b].append(a)
    seen: set[str] = set()
    groups: list[list[str]] = []
    for node in nodes:
        if node in seen:
            continue
        stack, group = [node], []
        seen.add(node)
        while stack:
            current = stack.pop()
            group.append(current)
            for nxt in neighbors[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        groups.append(sorted(group))
    return groups


def _spring_layout(
    nodes: list[str],
    edges: list[tuple[str, str, float]],
    *,
    iterations: int = 240,
) -> dict[str, tuple[float, float]]:
    """确定性力导向 + 分量拼版：拓扑位置可读，整体尺寸按真实路长压缩到演示尺度。

    不用第三方库（也不引 networkx）。初值按节点名字母序排在圆上、迭代顺序固定，
    所以同一份输入必定同一份输出；这是这个函数唯一需要的"随机性"。

    单位不是米：先按力导向拿到相对形状，再把每个分量的包围盒缩放成
    `0.35 × 该分量路长之和`，分量之间沿 X 轴排开。要的是"这条路相对那条在哪个方位、
    多长"，不是测绘坐标。
    """
    components = _components(nodes, edges)
    placed: dict[str, tuple[float, float]] = {}
    cursor_x = 0.0
    gap = 220.0
    for group in components:
        members = set(group)
        local_edges = [edge for edge in edges if edge[0] in members and edge[1] in members]
        radius = max(60.0, sum(length for _, _, length in local_edges) / max(1, len(local_edges)))
        pos: dict[str, list[float]] = {}
        for index, node in enumerate(group):
            angle = 2 * math.pi * index / max(1, len(group))
            pos[node] = [radius * math.cos(angle), radius * math.sin(angle)]

        pairs = [(a, b) for i, a in enumerate(group) for b in group[i + 1 :]]
        for step in range(iterations):
            temp = radius * (1 - step / iterations) * 0.25
            disp = {node: [0.0, 0.0] for node in group}
            for a, b in pairs:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                dist = math.hypot(dx, dy) or 0.01
                force = radius * radius / dist
                ux, uy = dx / dist, dy / dist
                disp[a][0] += ux * force
                disp[a][1] += uy * force
                disp[b][0] -= ux * force
                disp[b][1] -= uy * force
            for a, b, length in local_edges:
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                dist = math.hypot(dx, dy) or 0.01
                force = dist * dist / max(length, 1.0)
                ux, uy = dx / dist, dy / dist
                disp[a][0] -= ux * force
                disp[a][1] -= uy * force
                disp[b][0] += ux * force
                disp[b][1] += uy * force
            for node in group:
                dx, dy = disp[node]
                norm = math.hypot(dx, dy) or 1.0
                move = min(norm, temp)
                pos[node][0] += dx / norm * move
                pos[node][1] += dy / norm * move

        xs = [value[0] for value in pos.values()]
        ys = [value[1] for value in pos.values()]
        width = max(xs) - min(xs) or 1.0
        height = max(ys) - min(ys) or 1.0
        total_length = sum(length for _, _, length in local_edges) or 1.0
        target_width = max(360.0, 0.35 * total_length)
        scale = target_width / width
        for node in group:
            placed[node] = (
                round(cursor_x + (pos[node][0] - min(xs)) * scale, 1),
                round((pos[node][1] - min(ys)) * scale - height * scale / 2, 1),
            )
        cursor_x += target_width + gap

    center_x = (min(x for x, _ in placed.values()) + max(x for x, _ in placed.values())) / 2
    return {node: (round(x - center_x, 1), y) for node, (x, y) in placed.items()}


def _offset_polyline(
    a: tuple[float, float], b: tuple[float, float], offset: float
) -> list[LayoutPoint]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / norm, dx / norm
    return [
        LayoutPoint(x=round(a[0] + nx * offset, 1), y=round(a[1] + ny * offset, 1)),
        LayoutPoint(x=round(b[0] + nx * offset, 1), y=round(b[1] + ny * offset, 1)),
    ]


def _point_segment_distance(
    point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.dist(point, a)
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.dist(point, (a[0] + t * dx, a[1] + t * dy))


def _interpolate(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _hash01(*parts: object) -> float:
    """确定性伪随机：同一格子永远同一个数，不用 random。"""
    text = "|".join(str(part) for part in parts)
    value = 0
    for char in text:
        value = (value * 131 + ord(char)) % 1_000_003
    return value / 1_000_003


# --- 组装 ---------------------------------------------------------------


def build_layout(dataset_root: Path | str | None = None) -> SceneLayout:
    root = Path(dataset_root) if dataset_root is not None else DEFAULT_DATASET_ROOT
    roads = _rows(root / "road_network.csv")
    pipelines = _rows(root / "pipeline_ledger.csv")
    facilities = _rows(root / "drainage_facilities.csv")
    plans = _rows(root / "construction_plans.csv")
    orders = _rows(root / "work_orders.csv")

    under_construction = sorted(
        {plan["road_id"] for plan in plans if (plan.get("closure_scope") or "").strip()}
    )
    # 封路是事件状态，不是道路台账属性：取积水深度达到封路阈值的工单
    flooded = {
        order["road_id"]: _parse_cm(order.get("description", ""))
        for order in orders
        if order.get("order_type") == "waterlogging"
    }
    closed_roads = sorted(
        {row["road_id"] for row in roads if _state_of(row) == "closed"}
        | {road_id for road_id, depth in flooded.items() if depth >= CLOSURE_THRESHOLD_CM}
    )

    edges = [
        (row["from_node"].strip(), row["to_node"].strip(), _num(row.get("length_m"), 1.0))
        for row in roads
    ]
    nodes = sorted({node for edge in edges for node in edge[:2]})
    coords = _spring_layout(nodes, edges)

    layout_roads: list[LayoutRoad] = []
    for row in sorted(roads, key=lambda item: item["road_id"]):
        a = coords[row["from_node"].strip()]
        b = coords[row["to_node"].strip()]
        # 封路优先于"施工中"：同一条路既在施工又被封，展示上按封路处理
        if row["road_id"] in closed_roads:
            state = "closed"
        elif row["road_id"] in under_construction:
            state = "under_construction"
        else:
            state = _state_of(row)
        layout_roads.append(
            LayoutRoad(
                road_id=row["road_id"],
                name=row["name"],
                district=row["district"],
                road_class=row["road_class"],
                state=state,
                width_m=round(int(_num(row.get("lanes"), 2)) * LANE_WIDTH_M, 1),
                z=round(_num(row.get("elevation_m"), 0.0), 2),
                length_m=round(_num(row.get("length_m"), 0.0), 1),
                polyline=[LayoutPoint(x=a[0], y=a[1]), LayoutPoint(x=b[0], y=b[1])],
                from_node=row["from_node"].strip(),
                to_node=row["to_node"].strip(),
                evidence=f"A-03/road_network/v1.1#row:{_line_of(root, 'road_network.csv', row['road_id'])}",
            )
        )

    road_index = {road.road_id: road for road in layout_roads}

    # 管线：按所属道路中线偏移，避免同一路段的多条线重叠
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in pipelines:
        grouped.setdefault(row["road_id"], []).append(row)
    layout_pipelines: list[LayoutPipeline] = []
    for road_id, group in sorted(grouped.items()):
        road = road_index.get(road_id)
        if road is None:
            continue
        a = (road.polyline[0].x, road.polyline[0].y)
        b = (road.polyline[1].x, road.polyline[1].y)
        span = len(group)
        for position, row in enumerate(sorted(group, key=lambda item: item["pipeline_id"])):
            offset = (position - (span - 1) / 2) * 1.8
            layout_pipelines.append(
                LayoutPipeline(
                    pipeline_id=row["pipeline_id"],
                    kind=row["kind"],
                    road_id=road_id,
                    diameter_mm=int(_num(row.get("diameter_mm"), 100)),
                    depth_m=round(_num(row.get("depth_m"), 1.0), 2),
                    z=round(-_num(row.get("depth_m"), 1.0), 2),
                    polyline=_offset_polyline(a, b, offset),
                    evidence=f"A-03/pipeline_ledger/v1#row:{_line_of(root, 'pipeline_ledger.csv', row['pipeline_id'])}",
                )
            )

    layout_facilities: list[LayoutFacility] = []
    for row in sorted(facilities, key=lambda item: item["facility_id"]):
        road = road_index.get(row["road_id"])
        if road is None:
            continue
        a = (road.polyline[0].x, road.polyline[0].y)
        b = (road.polyline[1].x, road.polyline[1].y)
        base = _interpolate(a, b, 0.35 if row["facility_type"] == "pump_station" else 0.62)
        side = -1.0 if row["facility_type"] == "pump_station" else 1.0
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy) or 1.0
        offset = road.width_m / 2 + CURB_MARGIN_M + 3.0
        xy = (base[0] + side * -dy / norm * offset, base[1] + side * dx / norm * offset)
        layout_facilities.append(
            LayoutFacility(
                facility_id=row["facility_id"],
                name=row["name"],
                facility_type=row["facility_type"],
                road_id=row["road_id"],
                state=row.get("state", "in_service"),
                capacity_m3h=int(_num(row.get("capacity_m3h"), 0)),
                footprint_m=round(6.0 + _num(row.get("capacity_m3h"), 0) / 400, 1),
                xy=LayoutPoint(x=round(xy[0], 1), y=round(xy[1], 1)),
                z=road.z,
                evidence=f"A-03/drainage_facility/v1#row:{_line_of(root, 'drainage_facilities.csv', row['facility_id'])}",
            )
        )

    # 建筑体块：按片区撒点，从片区中心向外填，避开道路缓冲带
    layout_buildings: list[LayoutBuilding] = []
    districts = sorted({road.district for road in layout_roads})
    for district in districts:
        members = [road for road in layout_roads if road.district == district]
        xs = [point.x for road in members for point in road.polyline]
        ys = [point.y for road in members for point in road.polyline]
        center = (sum(xs) / len(xs), sum(ys) / len(ys))
        step = BUILDING_GAP_M
        candidates: list[tuple[float, float]] = []
        y = min(ys) - 40.0
        while y <= max(ys) + 40.0:
            x = min(xs) - 40.0
            while x <= max(xs) + 40.0:
                clearance = min(
                    _point_segment_distance(
                        (x, y),
                        (segment.polyline[0].x, segment.polyline[0].y),
                        (segment.polyline[1].x, segment.polyline[1].y),
                    )
                    for segment in members
                )
                if clearance >= BUILDING_MIN_CLEARANCE_M + step / 2:
                    candidates.append((x, y))
                x += step
            y += step
        # 越靠近片区中心越密：先按到中心的距离排序取前 N 个，演示画面里才有"街区"感
        candidates.sort(key=lambda item: (math.dist(item, center), item[1], item[0]))
        for index, (x, y) in enumerate(candidates[:MAX_BUILDINGS_PER_DISTRICT]):
            spread = _hash01(district, round(x, 1), round(y, 1))
            width = 12.0 + 12.0 * _hash01(district, x, y, "w")
            depth = 10.0 + 9.0 * _hash01(district, y, x, "d")
            height = (12.0 + 20.0 * spread) if district == "A-03" else (10.0 + 14.0 * spread)
            layout_buildings.append(
                LayoutBuilding(
                    building_id=f"B-{district}-{index:03d}",
                    district=district,
                    footprint=[
                        LayoutPoint(x=round(x - width / 2, 1), y=round(y - depth / 2, 1)),
                        LayoutPoint(x=round(x + width / 2, 1), y=round(y - depth / 2, 1)),
                        LayoutPoint(x=round(x + width / 2, 1), y=round(y + depth / 2, 1)),
                        LayoutPoint(x=round(x - width / 2, 1), y=round(y + depth / 2, 1)),
                    ],
                    height_m=round(height, 1),
                )
            )

    # 水面：取积水工单，沿工单里的桩号位置铺一段
    layout_water: list[LayoutWater] = []
    for order in sorted(orders, key=lambda item: item["order_id"]):
        if order.get("order_type") != "waterlogging":
            continue
        road = road_index.get(order["road_id"])
        if road is None:
            continue
        depth = _parse_cm(order.get("description", ""))
        chainage = _parse_chainage(order.get("description", ""))
        a = (road.polyline[0].x, road.polyline[0].y)
        b = (road.polyline[1].x, road.polyline[1].y)
        total = math.dist(a, b) or 1.0
        ratio = min(0.9, max(0.1, chainage / max(road.length_m, 1.0)))
        center = _interpolate(a, b, ratio)
        half = min(90.0, total / 3)  # 示意图里积水段取 180m 量级，太短在俯视图里看不见
        dx, dy = (b[0] - a[0]) / total, (b[1] - a[1]) / total
        nx, ny = -dy, dx
        half_width = max(road.width_m / 2, 6.0)
        polygon: list[LayoutPoint] = []
        for along, across in ((-half, -half_width), (half, -half_width), (half, half_width), (-half, half_width)):
            polygon.append(
                LayoutPoint(
                    x=round(center[0] + dx * along + nx * across, 1),
                    y=round(center[1] + dy * along + ny * across, 1),
                )
            )
        layout_water.append(
            LayoutWater(
                incident_id=order["order_id"],
                district=road.district,
                road_id=road.road_id,
                depth_cm=depth,
                z=round(road.z + 0.05, 2),
                polygon=polygon,
                evidence=f"A-03/work_order/v1#row:{_line_of(root, 'work_orders.csv', order['order_id'])}",
            )
        )

    layout_districts: list[LayoutDistrict] = []
    for district in districts:
        points = [
            (point.x, point.y)
            for road in layout_roads
            if road.district == district
            for point in road.polyline
        ]
        if not points:
            continue
        center_x = sum(item[0] for item in points) / len(points)
        center_y = sum(item[1] for item in points) / len(points)
        radius = max(math.dist((center_x, center_y), point) for point in points) * 1.15
        layout_districts.append(
            LayoutDistrict(
                district_id=district,
                center=LayoutPoint(x=round(center_x, 1), y=round(center_y, 1)),
                radius_m=round(radius, 1),
            )
        )

    coords_all = [(point.x, point.y) for road in layout_roads for point in road.polyline]
    xs = [item[0] for item in coords_all] or [0.0]
    ys = [item[1] for item in coords_all] or [0.0]
    for item in layout_buildings:
        xs.extend(point.x for point in item.footprint)
        ys.extend(point.y for point in item.footprint)

    return SceneLayout(
        snapshot_hash=_snapshot_hash(root),
        bounds={
            "min": [round(min(xs) - 40, 1), round(min(ys) - 40, 1), -4.0],
            "max": [round(max(xs) + 40, 1), round(max(ys) + 40, 1), 40.0],
        },
        nodes={node: LayoutPoint(x=coords[node][0], y=coords[node][1]) for node in nodes},
        roads=layout_roads,
        pipelines=layout_pipelines,
        facilities=layout_facilities,
        buildings=layout_buildings,
        water=layout_water,
        districts=layout_districts,
        states={
            "closed_roads": closed_roads,
            "under_construction": under_construction,
            "flooded_roads": sorted({item.road_id for item in layout_water}),
        },
    )


def _parse_cm(text: str) -> float:
    """"积水约 18cm" -> 18.0。必须贴着 cm 取数，否则会把桩号 K1+050 的数字一起收进去。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*cm", text)
    return float(match.group(1)) if match else 0.0


def _parse_chainage(text: str) -> float:
    """从工单描述里取 K0+320 这类桩号，返回米。"""
    marker = text.find("K")
    while marker >= 0:
        head, _, rest = text[marker + 1 :].partition("+")
        if head.isdigit() and rest[:3].isdigit():
            return float(head) * 1000 + float(rest[:3])
        marker = text.find("K", marker + 1)
    return 0.0


def _line_of(root: Path, file_name: str, key: str) -> int:
    lines = (root / file_name).read_text(encoding="utf-8-sig").splitlines()
    for index, line in enumerate(lines, start=1):
        if key in line:
            return index
    return 0


def _snapshot_hash(root: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for name in (
        "road_network.csv",
        "pipeline_ledger.csv",
        "drainage_facilities.csv",
        "construction_plans.csv",
        "work_orders.csv",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def write_layout(path: Path | str, layout: SceneLayout) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(layout.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build engine-neutral scene layout")
    parser.add_argument("--root", default=None, help="dataset directory")
    parser.add_argument("--out", default=None, help="output json path")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else DEFAULT_DATASET_ROOT
    layout = build_layout(root)
    out = Path(args.out) if args.out else root / OUTPUT_NAME
    write_layout(out, layout)
    print(
        f"{out.name}: 道路 {len(layout.roads)} 条（封 {len(layout.states['closed_roads'])} / 施工 {len(layout.states['under_construction'])}）、"
        f"管线 {len(layout.pipelines)} 条、设施 {len(layout.facilities)} 处、"
        f"建筑 {len(layout.buildings)} 栋、水面 {len(layout.water)} 处"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
