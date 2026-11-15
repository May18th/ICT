#!/usr/bin/env python3
"""把下载的 OSM 建筑数据投影到 scene_layout 的示意图坐标系，生成俯视图建筑底图。

投影参数与 build_osm_basemap.py 一致（semantic_entities_legacy_osm.json 的 projection）：
  lat = lat0 + y/k_lat   ->   y = (lat - lat0)*k_lat
  lon = lon0 + x/k_lon   ->   x = (lon - lon0)*k_lon

产物：out/osm/base_buildings.json —— 精简的建筑轮廓（footprint 多边形）。

2026-11-14 补三样东西（3D 化必需）：
  height_m       推定建筑高度（米）
  height_source  高度来源，取值 osm_height / osm_levels / inferred_type
  building       OSM building 类型（apartments/house/commercial/...）

为什么要 height_source：成都这份数据里 height 标签只覆盖 4.7%、building:levels 覆盖
28.3%，剩下七成多的楼高度只能按类型猜。猜出来的高度和实测高度在图上长得一样，
不标注来源就等于把推定值当实测值用。下游渲染必须按这个字段区分图例。

为什么同时吃 way 和 relation：multipolygon 建筑的轮廓主体在 relation 上，第一版
只看 way，这类楼（含 IFS 这种综合体）只能拿到残缺的外环或干脆丢光。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 默认仍是原来那份（只覆盖中心 1618×1777m）。要出全业务范围（x ±1469.5m）的
# 建筑，用 --src out/osm/chengdu_bbox_raw.json（见 scripts/osm_pbf_extract.py）。
SRC = ROOT / "out" / "osm" / "chengdu_centre_buildings.json"
OUT = ROOT / "out" / "osm" / "base_buildings.json"

LAT0, LON0 = 30.657, 104.066
K_LAT, K_LON = 110540.0, 95761.381

# 建筑面积阈值（示意坐标系单位 = 米）：过滤掉过小噪声和超大异常
MIN_AREA = 25.0    # 5m×5m 以下视为噪声
MAX_AREA = 20000.0  # 超过 ~140m×140m 视为异常/综合体（仍保留，但标 large）

# 层高。住宅 2.8~3.0m、商业裙楼 4.5~5.0m，取 3.2 是「看不出错」的折中：
# 偏差在两个方向上都留了余量，比按住宅层高 2.8 推出来的矮楼更不容易被一眼看穿。
LEVEL_HEIGHT_M = 3.2

# 无 height 也无 levels 时的类型默认层数。取值依据成都中心城区常见形态：
# 老小区 6~7 层、临街商业 3~4 层、酒店 8 层上下、独栋 2 层。
# 这些是**推定值**，不是实测——渲染端必须按 height_source 标注。
TYPE_DEFAULT_LEVELS = {
    "apartments": 7, "residential": 7, "dormitory": 6,
    "house": 2, "detached": 2, "semidetached_house": 2, "terrace": 3,
    "commercial": 4, "retail": 3, "office": 7, "hotel": 8,
    "school": 4, "university": 4, "college": 4, "kindergarten": 2,
    "hospital": 6, "public": 4, "civic": 5, "government": 5,
    "industrial": 3, "warehouse": 3, "manufacture": 3,
    "garage": 1, "garages": 1, "carport": 1,
    "church": 3, "temple": 3, "mosque": 3,
    "construction": 4, "roof": 1,
    "yes": 3,
}
DEFAULT_LEVELS = 3


def to_xy(lat: float, lon: float) -> tuple[float, float]:
    x = (lon - LON0) * K_LON
    y = (lat - LAT0) * K_LAT
    return round(x, 2), round(y, 2)


def ring_area(points: list[tuple[float, float]]) -> float:
    """鞋带公式算多边形面积（平方米，示意坐标即米）。"""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def parse_height(value: str) -> float | None:
    """OSM height 可能是 '45'、'45 m'、'45.5'、'12~15'。解析不出来就返回 None，
    不要把脏值当 0 用——0 米和「不知道」是两回事。"""
    if not value:
        return None
    s = str(value).strip().lower().replace("meters", "").replace("meter", "")
    s = s.replace("m", "").replace("米", "").strip()
    for sep in ("~", "-", ";"):
        if sep in s:
            s = s.split(sep)[0].strip()
            break
    try:
        h = float(s)
    except ValueError:
        return None
    return h if h > 0 else None


def parse_levels(value: str) -> int | None:
    """building:levels 可能是 '3'、'3.5'、'3;4'、'B1+3'。只取第一段正整数。"""
    if not value:
        return None
    s = str(value).strip()
    for sep in (";", ","):
        if sep in s:
            s = s.split(sep)[0].strip()
    try:
        lv = int(float(s))
    except ValueError:
        return None
    return lv if lv > 0 else None


def resolve_height(tags: dict) -> tuple[float, str, int | None]:
    """返回 (高度米, 来源, 层数原值)。

    优先级 height > building:levels > 类型默认。这是 OSM 社区的通行做法，
    也是唯一能自证的口径：越靠前的来源越硬。
    """
    h = parse_height(tags.get("height"))
    lv = parse_levels(tags.get("building:levels"))
    if h is not None:
        return round(h, 1), "osm_height", lv
    if lv is not None:
        return round(lv * LEVEL_HEIGHT_M, 1), "osm_levels", lv
    btype = tags.get("building", "yes")
    default_lv = TYPE_DEFAULT_LEVELS.get(btype, DEFAULT_LEVELS)
    return round(default_lv * LEVEL_HEIGHT_M, 1), "inferred_type", lv


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="OSM 建筑轮廓 -> 底图数据")
    ap.add_argument("--src", type=Path, default=SRC,
                    help="Overpass out geom 格式的输入（默认中心区那份）")
    ap.add_argument("--out", type=Path, default=OUT, help="输出路径")
    args = ap.parse_args(argv)

    d = json.loads(args.src.read_text(encoding="utf-8"))
    buildings: list[dict] = []
    seen_ids: set[int] = set()

    def emit(eid: int, tags: dict, pts: list[tuple[float, float]]) -> None:
        if eid in seen_ids:
            return
        area = ring_area(pts)
        if area < MIN_AREA:
            return
        height_m, source, levels = resolve_height(tags)
        seen_ids.add(eid)
        buildings.append({
            "id": eid,
            "name": tags.get("name", ""),
            "levels": tags.get("building:levels", ""),
            "building": tags.get("building", ""),
            "height_m": height_m,
            "height_source": source,
            "area": round(area, 1),
            "large": area > MAX_AREA,
            "footprint": [{"x": p[0], "y": p[1]} for p in pts],
        })

    for e in d.get("elements", []):
        etype = e.get("type")
        tags = e.get("tags", {})
        if not tags.get("building"):
            continue

        if etype == "way":
            geom = e.get("geometry")
            if not geom or len(geom) < 4:
                continue
            pts = [to_xy(p["lat"], p["lon"]) for p in geom]
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            emit(e.get("id"), tags, pts)

        elif etype == "relation":
            # 每个 outer 环当一栋独立建筑。一个 relation 有多个 outer 时
            # 通常是「一个院子里的几栋楼」，各自有高度才画得对。
            for idx, ring in enumerate(e.get("rings") or []):
                if ring.get("role") != "outer":
                    continue
                geom = ring.get("geometry") or []
                if len(geom) < 4:
                    continue
                pts = [to_xy(p["lat"], p["lon"]) for p in geom]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                # relation 的 id 不能直接当建筑 id（多 outer 会撞），拼环序号后缀
                emit(int(f"{e['id']}{idx:03d}"), tags, pts)

    # 大建筑先画（底层），小建筑后画（上层），避免小楼被大综合体盖住
    buildings.sort(key=lambda b: (-b["area"], b["id"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({
            "source": "OpenStreetMap © contributors, ODbL 1.0",
            "projection": {"lat0": LAT0, "lon0": LON0, "k_lat": K_LAT, "k_lon": K_LON},
            "height_note": "height_source=osm_height 为 OSM 实测标签；=osm_levels 为"
                           "层数×3.2m 换算；=inferred_type 为按建筑类型推定，非实测",
            "buildings": buildings,
        }, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    from collections import Counter
    named = [b for b in buildings if b["name"]]
    src = Counter(b["height_source"] for b in buildings)
    print(f"written: {args.out} ({args.out.stat().st_size} bytes)")
    print(f"  buildings={len(buildings)}  有名={len(named)}  大综合体={sum(1 for b in buildings if b['large'])}")
    print(f"  高度来源: {dict(src)}")
    hs = [b["height_m"] for b in buildings]
    if hs:
        print(f"  高度范围: {min(hs):.1f} ~ {max(hs):.1f} m，中位数 {sorted(hs)[len(hs)//2]:.1f} m")
    if named[:5]:
        print(f"  样例名: {', '.join(b['name'] for b in named[:5])}")


if __name__ == "__main__":
    main()
