#!/usr/bin/env python3
"""把下载的 OSM 道路数据投影到 scene_layout 的示意图坐标系，生成俯视图底图数据。

投影（来自 semantic_entities_legacy_osm.json）：
  lat0=30.657, lon0=104.066, k_lat=110540, k_lon=95761.381
  示意图坐标 (x, y) [米] 与经纬度的关系：
    lat = lat0 + y/k_lat   ->   y = (lat - lat0)*k_lat
    lon = lon0 + x/k_lon   ->   x = (lon - lon0)*k_lon

产物：out/osm/base_roads.json —— 精简的道路折线，按 highway 等级分层。
  只保留「机动车可通行」的道路（去掉 footway/steps/path 这类步行道），
  并按 OSM 等级映射到 主干/次干/支路 三档宽度。

数据来源：OpenStreetMap，ODbL 1.0 授权，需标注 © OpenStreetMap contributors。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "out" / "osm" / "chengdu_centre_raw.json"
OUT = ROOT / "out" / "osm" / "base_roads.json"

LAT0, LON0 = 30.657, 104.066
K_LAT, K_LON = 110540.0, 95761.381

# OSM highway 等级 -> 底图层级（0 主干 / 1 次干 / 2 支路）
LEVEL = {
    "motorway": 0, "motorway_link": 0, "trunk": 0, "trunk_link": 0,
    "primary": 0, "primary_link": 0,
    "secondary": 1, "secondary_link": 1,
    "tertiary": 2, "tertiary_link": 2,
    "residential": 2, "unclassified": 2, "living_street": 2,
    "service": 3, "road": 2,
}
# 不画步行道/台阶/自行车道（不是车行路网）
SKIP = {"footway", "steps", "path", "pedestrian", "cycleway", "construction", "proposed"}


def to_xy(lat: float, lon: float) -> tuple[float, float]:
    x = (lon - LON0) * K_LON
    y = (lat - LAT0) * K_LAT
    return round(x, 2), round(y, 2)


def main() -> None:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    roads: list[dict] = []
    for e in d.get("elements", []):
        if e.get("type") != "way":
            continue
        tags = e.get("tags", {})
        hw = tags.get("highway", "")
        if hw in SKIP:
            continue
        lvl = LEVEL.get(hw)
        if lvl is None:
            continue
        # 只保留 level 0/1（主干 + 次干，226 条）：底图定位是「真实路网参考纹理」，
        # 不是语义载体。level 2/3（支路/服务路，584 条）在全景里是灰雾噪点，且会
        # 让"示意路 vs 真实路"的语义矛盾更刺眼。降为纹理后只留骨架就够了。
        if lvl >= 2:
            continue
        geom = e.get("geometry")
        if not geom or len(geom) < 2:
            continue
        name = tags.get("name", "")
        polyline = [to_xy(p["lat"], p["lon"]) for p in geom]
        roads.append({
            "id": e.get("id"),
            "level": lvl,
            "highway": hw,
            "name": name,
            # 桥隧与层高：3D 里立交要抬高、隧道要压到地下。空字符串表示
            # OSM 没标（不等于「没有桥」），渲染端按普通路面处理。
            "bridge": tags.get("bridge", ""),
            "tunnel": tags.get("tunnel", ""),
            "layer": tags.get("layer", ""),
            # 车道数与路宽：实测覆盖很低（level 0/1 里 lanes 约一成），
            # 渲染端要么按 highway 等级给默认宽度，要么按这里的值细化。
            "lanes": tags.get("lanes", ""),
            "width": tags.get("width", ""),
            "polyline": [{"x": p[0], "y": p[1]} for p in polyline],
        })
    # 按层级降序：支路/服务路先画，主干道（level 0）最后画在最上层，
    # 避免细路覆盖粗主干，主干骨架才拉得出来。
    roads.sort(key=lambda r: (-r["level"], r["id"]))
    OUT.write_text(
        json.dumps({
            "source": "OpenStreetMap © contributors, ODbL 1.0",
            "projection": {"lat0": LAT0, "lon0": LON0, "k_lat": K_LAT, "k_lon": K_LON},
            "roads": roads,
        }, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    from collections import Counter
    c = Counter(r["level"] for r in roads)
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")
    print(f"  roads={len(roads)}  level分布={dict(sorted(c.items()))}")


if __name__ == "__main__":
    main()
