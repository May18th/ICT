#!/usr/bin/env python3
"""从省级 PBF 里按 bbox 抽出底图要素，写成与 Overpass `out geom` 同构的 JSON。

为什么不用 Overpass：这台机器到 overpass-api.de 的请求被中间层回 406（nginx 伪
造，非服务端），其它全球镜像要么不通、要么要求白名单；osm.ch 是瑞士区域实例，
查成都返回 0 条。能走通的只有 download.openstreetmap.fr 的省级摘录，所以要自己
从 PBF 里切。

产物 out/osm/chengdu_bbox_raw.json 的 elements 结构与 Overpass
`[out:json];way[...](bbox);out geom;` 一致（way 直接带 geometry 数组、带 tags），
这样 build_osm_basemap.py 一类下游脚本按原样读，不用改解析逻辑。

三类要素都抽（2026-11-14 补 relation/node）：
  way       —— 与原来一致：type/id/tags/geometry
  node      —— type/id/tags/lat/lon，只留有标签且命中 NODE_KEYS 的点
  relation  —— 只抽 type=multipolygon，拼好环后输出
               type/id/tags/rings=[{role, geometry}]/complete

为什么必须补 relation：第一版只处理 way，结果整片城区只剩 1 条 waterway 线、
5 个 natural=water 面——成都中心城区的府河/南河是 multipolygon，全被丢了。
大型建筑综合体的主体也常常在 relation 上。

为什么必须补 node：地铁站出入口、公交站、红绿灯、消防栓全是点要素，一个都没抽到。
内涝处置里「最近的消防栓/地铁口在哪」是实打实的业务问题。

为什么能一遍扫完：OSM 文件格式保证 node 全部排在 way 之前、way 排在 relation 之前，
所以扫到 way 时 node 坐标表已完整，扫到 relation 时 way 的 node refs 已完整。

用法：
    PYTHONPATH=out/osm/tools .venv/Scripts/python.exe scripts/osm_pbf_extract.py
    PYTHONPATH=out/osm/tools .venv/Scripts/python.exe scripts/osm_pbf_extract.py --margin 1.4

数据来源：OpenStreetMap，ODbL 1.0。衍生数据同样受 ODbL 约束，提交时须标注
「© OpenStreetMap contributors」并说明许可。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBF = ROOT / "out" / "osm" / "src" / "sichuan-latest.osm.pbf"
OUT = ROOT / "out" / "osm" / "chengdu_bbox_raw.json"

# 投影参数（与 build_osm_basemap.py 一致，来自 semantic_entities_legacy_osm.json）
LAT0, LON0 = 30.657, 104.066
K_LAT, K_LON = 110540.0, 95761.381

# 业务范围（scene_layout.json 的 bounds）：x ±1469.5m / y ±512.1m
HALF_X = 1469.5
HALF_Y = 512.1

# 保留哪些标签键。底图分层需要的就是这几类，全留给下游决定画不画，
# 免得以后想加铁路又得重跑 PBF（一次扫描 1.5 分钟）。
KEYS = (
    # name 必须留：建筑 tooltip、公园/河流/学校的中文名全在它上面。第一版漏了，
    # 结果 1310 栋建筑一个名字都没有——数字看着正常，功能已经废了。
    "name",
    "highway", "building", "landuse", "natural", "waterway",
    "leisure", "railway", "amenity", "man_made", "barrier",
    "building:levels", "height", "tunnel", "bridge", "layer", "area",
    # --- 2026-11-14 补：3D 体块与点状 POI 需要 ---
    # 建筑体块细分（裙楼/塔楼）与真实高度上下界。成都这份数据里 building:part
    # 覆盖率是 0%，留着是为了将来换数据源时不用再改抽取器。
    "building:part", "min_height", "building:min_height",
    "roof:shape", "roof:height",
    # 道路几何属性。实测 lanes/width/oneway/surface/maxspeed/sidewalk 在成都
    # 这份数据里覆盖率都是 0%，仍然白名单留着——它们是 3D 车道建模的依据，
    # 换成有数据源的 bbox 时能直接吃到。
    "lanes", "width", "oneway", "surface", "maxspeed", "sidewalk",
    # 点状 POI
    "public_transport", "entrance", "emergency", "tourism",
    # relation 的类型标记（multipolygon / boundary），拼环必须用
    "type",
)

# 至少命中一个「主类别」才留。KEYS 里的 building:levels / height / layer 是附加
# 属性，单独出现说明不了这是什么——PBF 里有 526 条 way 只带 height 或
# building:levels 而不带 building，它们是 multipolygon 建筑的外环，主体标签在
# relation 上。没有依据就别猜，这类直接丢。
PRIMARY = (
    "highway", "building", "landuse", "natural", "waterway",
    "leisure", "railway", "amenity", "man_made", "barrier",
)

# 点要素白名单：命中任一即为有效点。name 只作附加信息，不单独成立。
NODE_KEYS = (
    "amenity", "public_transport", "highway", "railway", "man_made",
    "emergency", "tourism", "natural", "barrier", "entrance",
)


def load_pbf(path: Path) -> bytes:
    """读成 bytes。

    pyosmium 在 Windows 上用 ANSI 代码页的 fopen 开文件，路径含中文必失败
    （报 "The system cannot find the file specified"，看着像文件丢了；这台机器
    8.3 短名没开，临时目录又被沙箱拒写）。Python 的 open 走 Unicode API，
    所以先在 Python 侧读入内存，再交给 osmium.io.FileBuffer。
    """
    return path.read_bytes()


def stitch_rings(segments: list[list[int]]) -> list[list[int]]:
    """把碎段按端点拼成环。segments 是 node id 序列。

    OSM 的 multipolygon 把一条闭合边拆成若干 way，顺序随机、方向也不保证。
    这里做贪心拼接：首尾相接就接上，接不动就另起一环。返回的每个环首尾相同
    表示闭合成功，不同表示是断环（数据残缺，调用方会丢弃）。
    """
    pool = [list(s) for s in segments if len(s) >= 2]
    rings: list[list[int]] = []
    while pool:
        cur = pool.pop(0)
        # 上限防病态数据把 while 拖死。正常环的碎段数远小于这个。
        for _ in range(10000):
            if len(cur) >= 2 and cur[0] == cur[-1]:
                break
            merged = False
            for i, seg in enumerate(pool):
                if seg[0] == cur[-1]:
                    cur += seg[1:]
                elif seg[-1] == cur[-1]:
                    cur += seg[::-1][1:]
                elif seg[-1] == cur[0]:
                    cur = seg + cur[1:]
                elif seg[0] == cur[0]:
                    cur = seg[::-1] + cur[1:]
                else:
                    continue
                pool.pop(i)
                merged = True
                break
            if not merged:
                break
        rings.append(cur)
    return rings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 PBF 按 bbox 抽底图要素（node/way/relation）")
    ap.add_argument("--pbf", type=Path, default=PBF)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--margin", type=float, default=1.10,
                    help="业务范围外扩倍数（默认 1.10）")
    ap.add_argument("--node-pad", type=float, default=0.03,
                    help="收集节点坐标时额外外扩的度数（默认 0.03≈3km，"
                         "让跨边界的 way 也能拿全顶点）")
    args = ap.parse_args(argv)

    if not args.pbf.is_file():
        print(f"缺 PBF：{args.pbf}", file=sys.stderr)
        return 2

    import osmium
    import osmium.io

    lat_s = LAT0 - HALF_Y * args.margin / K_LAT
    lat_n = LAT0 + HALF_Y * args.margin / K_LAT
    lon_w = LON0 - HALF_X * args.margin / K_LON
    lon_e = LON0 + HALF_X * args.margin / K_LON
    # 取节点坐标时放宽一档，避免 way 跨出 bbox 后顶点缺失、折线断开
    nlat_s, nlat_n = lat_s - args.node_pad, lat_n + args.node_pad
    nlon_w, nlon_e = lon_w - args.node_pad, lon_e + args.node_pad

    t0 = time.time()
    data = load_pbf(args.pbf)
    print(f"PBF {len(data)/1e6:.1f} MB 已读入内存（{time.time()-t0:.1f}s）")
    print(f"输出 bbox  lat [{lat_s:.6f}, {lat_n:.6f}]  lon [{lon_w:.6f}, {lon_e:.6f}]")

    # --- 一遍扫描 -----------------------------------------------------
    coords: dict[int, tuple[float, float]] = {}
    in_box: set[int] = set()
    # bbox 内 way 的 node refs。relation 在 way 之后才出现，此刻还不知道哪些
    # way 是 member，所以先全存——bbox 内 way 只有约 2 千条，内存可忽略。
    way_refs: dict[int, list[int]] = {}
    elements: list[dict] = []
    relations_raw: list[tuple[int, dict, list[tuple[str, int]]]] = []
    dropped_short = 0
    node_count = 0

    # 不传 entities：默认 osmium.osm.ALL，一次遍历拿到 node/way/relation。
    # PBF 格式保证 node 先于 way、way 先于 relation，所以走到 way 时坐标表已完整。
    buf = osmium.io.FileBuffer(data, "pbf")
    proc = osmium.FileProcessor(buf)
    for obj in proc:
        if obj.is_node():
            lat, lon = obj.location.lat, obj.location.lon
            if not (nlat_s <= lat <= nlat_n and nlon_w <= lon <= nlon_e):
                continue
            coords[obj.id] = (round(lat, 7), round(lon, 7))
            inside = lat_s <= lat <= lat_n and lon_w <= lon <= lon_e
            if inside:
                in_box.add(obj.id)
            tags = {t.k: t.v for t in obj.tags if t.k in KEYS}
            if inside and tags and any(k in tags for k in NODE_KEYS):
                elements.append({
                    "type": "node",
                    "id": obj.id,
                    "tags": tags,
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                })
                node_count += 1
        elif obj.is_way():
            refs = [n.ref for n in obj.nodes]
            if not any(r in in_box for r in refs):
                continue
            way_refs[obj.id] = refs
            tags = {t.k: t.v for t in obj.tags if t.k in KEYS}
            if not tags or not any(k in tags for k in PRIMARY):
                continue
            geom = []
            for r in refs:
                c = coords.get(r)
                if c is not None:
                    geom.append({"lat": c[0], "lon": c[1]})
            if len(geom) < 2:
                dropped_short += 1
                continue
            elements.append({
                "type": "way",
                "id": obj.id,
                "tags": tags,
                "geometry": geom,
            })
        elif obj.is_relation():
            tags = {t.k: t.v for t in obj.tags if t.k in KEYS}
            # 只关心 multipolygon：boundary 是行政边界，跟底图无关
            if tags.get("type") != "multipolygon":
                continue
            members = [
                (m.role or "", m.ref) for m in obj.members if m.type == "w"
            ]
            if members:
                relations_raw.append((obj.id, tags, members))

    print(f"节点坐标表 {len(coords):,} 个，其中落在输出 bbox 内 {len(in_box):,}")
    print(f"bbox 内 way {len(way_refs):,} 条")

    # --- relation 拼环 -------------------------------------------------
    rel_ok = 0
    rel_broken = 0
    for rel_id, tags, members in relations_raw:
        by_role: dict[str, list[list[int]]] = {}
        for role, wid in members:
            refs = way_refs.get(wid)
            if not refs:
                continue  # member way 落在 bbox 外（外扩不够），这一环不完整
            by_role.setdefault(role or "outer", []).append(refs)
        if not by_role:
            continue
        rings = []
        complete = True
        for role, segs in by_role.items():
            for ring in stitch_rings(segs):
                # 闭合判定：首尾同节点
                if len(ring) < 4 or ring[0] != ring[-1]:
                    complete = False
                    continue
                geom = []
                for r in ring:
                    c = coords.get(r)
                    if c is None:
                        complete = False
                        break
                    geom.append({"lat": c[0], "lon": c[1]})
                else:
                    if len(geom) >= 4:
                        rings.append({"role": role, "geometry": geom})
        if not rings:
            rel_broken += 1
            continue
        elements.append({
            "type": "relation",
            "id": rel_id,
            "tags": tags,
            "rings": rings,
            "complete": complete,
        })
        rel_ok += 1

    print(f"relation 拼环成功 {rel_ok} 个，无可用环 {rel_broken} 个")

    payload = {
        "version": 0.6,
        "generator": f"osm_pbf_extract.py / pyosmium from {args.pbf.name}",
        "osm3s": {
            "timestamp_osm_base": "see out/osm/src/sichuan-latest.osm.pbf.state.txt",
            "copyright": "The data included in this document is from "
                         "www.openstreetmap.org. The data is made available "
                         "under ODbL.",
            "license": "ODbL-1.0",
        },
        "bounds": {
            "minlat": round(lat_s, 7), "minlon": round(lon_w, 7),
            "maxlat": round(lat_n, 7), "maxlon": round(lon_e, 7),
            "projection": {"lat0": LAT0, "lon0": LON0, "k_lat": K_LAT, "k_lon": K_LON},
            "note": "业务范围 x ±1469.5m / y ±512.1m 外扩 "
                    f"{args.margin:.2f} 倍；坐标系与 scene_layout 一致",
        },
        "elements": elements,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    from collections import Counter
    kinds: Counter = Counter()
    for e in elements:
        t = e["tags"]
        if e["type"] == "node":
            kinds["node"] += 1
            continue
        if e["type"] == "relation":
            kinds["relation"] += 1
            continue
        for k in ("building", "highway", "landuse", "natural", "waterway",
                  "leisure", "railway", "amenity", "man_made", "barrier"):
            if k in t:
                kinds[k] += 1
                break
        else:
            kinds["其他"] += 1

    print(f"写出 {args.out}（{args.out.stat().st_size/1e6:.2f} MB）")
    print(f"  element={len(elements)}")
    print(f"  分类={dict(kinds)}  顶点不足丢弃={dropped_short}")
    print(f"  耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
