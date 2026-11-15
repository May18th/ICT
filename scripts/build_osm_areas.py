#!/usr/bin/env python3
"""把 osm_pbf_extract.py 抽出的 Overpass 格式 JSON 转成底图分层数据。

与 build_osm_basemap.py（道路）和 build_osm_buildings.py（建筑）并列，
这一份补的是**地块底色**：居住区、绿地、商业、在建工地、水体、公园，以及
地铁线。这三类合起来，底图才有「城区 / 绿地 / 水系」的层次，而不是一片灰。

产物 out/osm/base_areas.json：
  areas[]  面要素（有闭合轮廓、面积可算）
  lines[]  线要素（河道、地铁）

坐标与 scene_layout 一致：
    x = (lon - lon0) * k_lon,  y = (lat - lat0) * k_lat   [米]

数据来源：OpenStreetMap，ODbL 1.0。衍生数据同样受 ODbL 约束。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "out" / "osm" / "chengdu_bbox_raw.json"
OUT = ROOT / "out" / "osm" / "base_areas.json"

LAT0, LON0 = 30.657, 104.066
K_LAT, K_LON = 110540.0, 95761.381

# 面要素：标签 -> (kind, 语义说明)。kind 决定填色，subkind 保留原始 OSM 值。
# 只收底图用得上的：地块底色不该出现加油站、停车场这类细节，那是专题图层的事。
AREA_KIND = {
    "landuse": {
        "residential", "commercial", "industrial", "retail", "construction",
        "brownfield", "greenfield", "railway", "cemetery", "institutional",
        "military", "farmland", "meadow", "orchard", "forest", "village_green",
        "recreation_ground", "grass", "education",
    },
    "natural": {"water", "wood", "scrub", "grassland", "bare_soil", "beach"},
    "leisure": {"park", "garden", "pitch", "track", "playground",
                "sports_centre", "golf_course"},
    "amenity": {"parking", "fountain", "marketplace", "school", "hospital",
                "university", "college"},
}

# 线要素：河道与轨道
LINE_KIND = {
    "waterway": {"river", "canal", "stream", "ditch", "drain"},
    "railway": {"subway", "rail", "light_rail", "tram", "monorail",
                "narrow_gauge", "construction", "platform"},
}

# 面要素的分类优先级：一条 way 可能同时带 landuse 和 leisure，取更「重」的那个。
# school 这类 amenity 优先于 landuse=education，否则校园会被涂成一片居住区。
PRIORITY = ("natural", "waterway", "landuse", "leisure", "amenity", "railway")


def to_xy(lat: float, lon: float) -> tuple[float, float]:
    return round((lon - LON0) * K_LON, 2), round((lat - LAT0) * K_LAT, 2)


def is_closed(geom: list[dict]) -> bool:
    a, b = geom[0], geom[-1]
    return abs(a["lat"] - b["lat"]) < 1e-7 and abs(a["lon"] - b["lon"]) < 1e-7


def shoelace(pts: list[dict]) -> float:
    """平面多边形面积（m²）。点已是米制坐标，直接鞋带公式。"""
    s = 0.0
    n = len(pts)
    for i in range(n - 1):
        s += pts[i]["x"] * pts[i + 1]["y"] - pts[i + 1]["x"] * pts[i]["y"]
    return abs(s) / 2.0


def classify(tags: dict) -> tuple[str, str, str] | None:
    """返回 (layer, kind, subkind)；layer 是 'area' 或 'line'。"""
    for key in PRIORITY:
        val = tags.get(key)
        if not val:
            continue
        if key in AREA_KIND and val in AREA_KIND[key]:
            return "area", key, val
        if key in LINE_KIND and val in LINE_KIND[key]:
            return "line", key, val
    return None


def kind_cn(layer: str, key: str, sub: str) -> str:
    """给人看的类别名。底图上不写英文——图例里要是 `landuse=residential`
    看着就像半成品。"""
    m = {
        ("natural", "water"): "水体",
        ("natural", "wood"): "林地",
        ("natural", "scrub"): "灌木",
        ("natural", "grassland"): "草地",
        ("natural", "bare_soil"): "裸土",
        ("natural", "beach"): "滩地",
        ("landuse", "residential"): "居住区",
        ("landuse", "commercial"): "商业区",
        ("landuse", "industrial"): "工业区",
        ("landuse", "retail"): "商业区",
        ("landuse", "construction"): "在建工地",
        ("landuse", "brownfield"): "待建地",
        ("landuse", "greenfield"): "待建地",
        ("landuse", "railway"): "铁路用地",
        ("landuse", "cemetery"): "墓地",
        ("landuse", "institutional"): "机构用地",
        ("landuse", "military"): "军事用地",
        ("landuse", "farmland"): "农田",
        ("landuse", "meadow"): "草甸",
        ("landuse", "orchard"): "果园",
        ("landuse", "forest"): "林地",
        ("landuse", "village_green"): "绿地",
        ("landuse", "recreation_ground"): "休闲绿地",
        ("landuse", "grass"): "草地",
        ("landuse", "education"): "校园",
        ("leisure", "park"): "公园",
        ("leisure", "garden"): "花园",
        ("leisure", "pitch"): "球场",
        ("leisure", "track"): "跑道",
        ("leisure", "playground"): "活动场",
        ("leisure", "sports_centre"): "体育场",
        ("leisure", "golf_course"): "高尔夫球场",
        ("amenity", "parking"): "停车场",
        ("amenity", "fountain"): "喷泉",
        ("amenity", "marketplace"): "集市",
        ("amenity", "school"): "学校",
        ("amenity", "hospital"): "医院",
        ("amenity", "university"): "高校",
        ("amenity", "college"): "院校",
        ("waterway", "river"): "河道",
        ("waterway", "canal"): "渠",
        ("waterway", "stream"): "溪流",
        ("waterway", "ditch"): "沟渠",
        ("waterway", "drain"): "排水沟",
        ("railway", "subway"): "地铁",
        ("railway", "rail"): "铁路",
        ("railway", "light_rail"): "轻轨",
        ("railway", "tram"): "有轨电车",
        ("railway", "monorail"): "单轨",
        ("railway", "narrow_gauge"): "窄轨",
        ("railway", "construction"): "在建轨道",
        ("railway", "platform"): "站台",
    }
    return m.get((key, sub), f"{key}={sub}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成底图地块/水系/轨道数据")
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-area", type=float, default=400.0,
                    help="面要素最小面积 m²（默认 400，小于这个在全景里看不见）")
    args = ap.parse_args(argv)

    if not args.src.is_file():
        raise SystemExit(f"缺输入：{args.src}（先跑 scripts/osm_pbf_extract.py）")

    d = json.loads(args.src.read_text(encoding="utf-8"))
    areas: list[dict] = []
    lines: list[dict] = []
    skipped: dict[str, int] = {}

    for e in d.get("elements", []):
        etype = e.get("type")
        # 点要素不进面/线图层（node 没有 geometry，落下去会被 len<2 拦掉，
        # 这里显式跳过更清楚）
        if etype == "node":
            continue
        tags = e.get("tags") or {}

        # --- relation：multipolygon 的水面/绿地/校园 ---
        # 第一版只处理 way，结果整片城区只剩 1 条河道线、5 个水面。
        # relation 的 outer 当外轮廓、inner 当洞，面积要扣掉洞。
        if etype == "relation":
            hit = classify(tags)
            if hit is None:
                skipped["relation未分类"] = skipped.get("relation未分类", 0) + 1
                continue
            layer, key, sub = hit
            if layer != "area":
                skipped["relation非面"] = skipped.get("relation非面", 0) + 1
                continue
            rings = e.get("rings") or []
            outers = [r for r in rings
                      if r.get("role") == "outer" and len(r.get("geometry") or []) >= 4]
            inners = [r for r in rings
                      if r.get("role") == "inner" and len(r.get("geometry") or []) >= 4]
            if not outers:
                continue
            for oi, ring in enumerate(outers):
                pts = [dict(zip(("x", "y"), to_xy(p["lat"], p["lon"]))) for p in ring["geometry"]]
                a = shoelace(pts)
                holes: list[list[dict]] = []
                if oi == 0:
                    # 洞按「全归第一个外环」处理：多外环的 multipolygon 在
                    # 成都这份数据里只有 2 个，逐洞做点在多边形内判断不划算。
                    for ir in inners:
                        hp = [dict(zip(("x", "y"), to_xy(p["lat"], p["lon"]))) for p in ir["geometry"]]
                        ha = shoelace(hp)
                        if ha > 0:
                            holes.append(hp)
                            a -= ha
                if a < args.min_area:
                    skipped["面积过小"] = skipped.get("面积过小", 0) + 1
                    continue
                item = {
                    "id": int(f"{e['id']}{oi:03d}"),
                    "kind": key,
                    "subkind": sub,
                    "label": kind_cn(layer, key, sub),
                    "name": tags.get("name", ""),
                    "area": round(a, 1),
                    "polygon": pts,
                }
                if holes:
                    item["holes"] = holes
                areas.append(item)
            continue

        geom = e.get("geometry") or []
        if len(geom) < 2:
            continue
        hit = classify(tags)
        if hit is None:
            skipped["未分类"] = skipped.get("未分类", 0) + 1
            continue
        layer, key, sub = hit
        pts = [dict(zip(("x", "y"), to_xy(p["lat"], p["lon"]))) for p in geom]
        closed = is_closed(geom) and len(pts) >= 4
        item = {
            "id": e.get("id"),
            "kind": key,
            "subkind": sub,
            "label": kind_cn(layer, key, sub),
            "name": tags.get("name", ""),
        }
        if layer == "area" or (layer == "line" and closed and key != "waterway"):
            if not closed:
                # 面类别但没有闭合轮廓：多半是 relation 的边，画出来是断片
                skipped["面未闭合"] = skipped.get("面未闭合", 0) + 1
                continue
            a = shoelace(pts)
            if a < args.min_area:
                skipped["面积过小"] = skipped.get("面积过小", 0) + 1
                continue
            item["area"] = round(a, 1)
            item["polygon"] = pts
            areas.append(item)
        else:
            item["polyline"] = pts
            lines.append(item)

    # 面从大到小：大块先画在底层，小块的公园/球场叠在上面
    areas.sort(key=lambda x: -x["area"])
    lines.sort(key=lambda x: (x["kind"], x["id"] or 0))

    payload = {
        "source": "OpenStreetMap © contributors, ODbL 1.0",
        "source_file": str(args.src.name),
        "projection": {"lat0": LAT0, "lon0": LON0, "k_lat": K_LAT, "k_lon": K_LON},
        "areas": areas,
        "lines": lines,
    }
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    from collections import Counter
    ca = Counter((a["kind"], a["subkind"]) for a in areas)
    cl = Counter((l["kind"], l["subkind"]) for l in lines)
    print(f"写出 {args.out}（{args.out.stat().st_size/1e6:.2f} MB）")
    print(f"  areas={len(areas)}  lines={len(lines)}  跳过={skipped}")
    print("  面类别:", dict(ca.most_common()))
    print("  线类别:", dict(cl.most_common()))
    if areas:
        big = areas[0]
        print(f"  最大面: {big['label']} {big['name'] or '(无名)'} {big['area']/1e4:.2f} 公顷")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
