#!/usr/bin/env python3
"""探测 PBF 在指定 bbox 内有哪些要素类别，只统计不落盘。

用法（pyosmium 不在项目依赖里，用 out/osm/tools 里的 wheel 解压版）：
    PYTHONPATH=out/osm/tools .venv/Scripts/python.exe scripts/osm_probe.py

为什么要这一步：底图上要补的是地块底色（landuse）、水系（natural=water /
waterway）、以及下穿隧道相关的铁路，但没人知道这片 bbox 里这些标签到底有没有、
有多少。先数清楚再决定保留哪些，比凭猜写过滤条件靠谱。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBF = ROOT / "out" / "osm" / "src" / "sichuan-latest.osm.pbf"


def load_pbf(path: Path) -> tuple[bytes, "object"]:
    """把 PBF 读成 bytes，再用 osmium.FileBuffer 喂给 C++ 层。

    为什么要绕这一圈：pyosmium 在 Windows 上用 ANSI 代码页的 fopen 开文件，
    路径含中文必失败（报 "The system cannot find the file specified"，看着像
    文件丢了；8.3 短名在这台机器上没开，临时目录又被沙箱拒写）。
    Python 自己的 open 走 Unicode API，所以先在 Python 侧读成 bytes，再交给
    FileBuffer —— FileBuffer 接受任何 buffer 协议对象。118 MB 常驻内存无压力。
    """
    import osmium.io

    data = path.read_bytes()
    buf = osmium.io.FileBuffer(data, "pbf")
    return data, buf

# 投影参数（与 build_osm_basemap.py 一致，来自 semantic_entities_legacy_osm.json）
LAT0, LON0 = 30.657, 104.066
K_LAT, K_LON = 110540.0, 95761.381

# 业务范围 x ±1469.5m / y ±512.1m，外扩 10% 作底图余量
HALF_X = 1469.5 * 1.10
HALF_Y = 512.1 * 1.10
LAT_S = LAT0 - HALF_Y / K_LAT
LAT_N = LAT0 + HALF_Y / K_LAT
LON_W = LON0 - HALF_X / K_LON
LON_E = LON0 + HALF_X / K_LON

# 关心的标签键。值与用途对应：底图分层的依据全在这里。
KEYS = (
    "highway", "building", "landuse", "natural", "waterway",
    "leisure", "railway", "amenity", "man_made", "barrier",
)


def in_box(lat: float, lon: float) -> bool:
    return LAT_S <= lat <= LAT_N and LON_W <= lon <= LON_E


def main() -> int:
    import osmium
    import osmium.io

    if not PBF.is_file():
        print(f"缺 PBF：{PBF}", file=sys.stderr)
        return 2
    data, _ = load_pbf(PBF)
    print(f"PBF {len(data)/1e6:.1f} MB  已读入内存")
    print(f"bbox  lat [{LAT_S:.6f}, {LAT_N:.6f}]  lon [{LON_W:.6f}, {LON_E:.6f}]")

    # 第一遍：bbox 内的 node id 集合。第二遍判 way 是否落进来要用。
    nodes: set[int] = set()
    n_node_total = 0
    for obj in osmium.FileProcessor(osmium.io.FileBuffer(data, "pbf"), osmium.osm.NODE):
        n_node_total += 1
        if in_box(obj.location.lat, obj.location.lon):
            nodes.add(obj.id)
    print(f"node 总数 {n_node_total:,}，bbox 内 {len(nodes):,}")

    cats: dict[str, Counter] = {k: Counter() for k in KEYS}
    keyed: Counter = Counter()   # way 上有几个我们关心的键
    n_way_total = 0
    n_way_hit = 0
    named: Counter = Counter()
    for obj in osmium.FileProcessor(osmium.io.FileBuffer(data, "pbf"), osmium.osm.WAY):
        n_way_total += 1
        # obj.nodes 的元素是 NodeRef 对象，不是 int；写成 n in nodes 会永远 False
        if not any(n.ref in nodes for n in obj.nodes):
            continue
        n_way_hit += 1
        tags = {t.k: t.v for t in obj.tags}
        hit = 0
        for k in KEYS:
            if k in tags:
                cats[k][tags[k]] += 1
                hit += 1
        if hit:
            keyed[hit] += 1
            if tags.get("name"):
                named[next(k for k in KEYS if k in tags)] += 1

    print(f"way 总数 {n_way_total:,}，落在 bbox 内 {n_way_hit:,}")
    print(f"含关心标签的 way 上有几个键: {dict(sorted(keyed.items()))}")
    for k in KEYS:
        if cats[k]:
            top = ", ".join(f"{v}×{n}" for v, n in cats[k].most_common(10))
            print(f"  {k:9s} 共 {sum(cats[k].values()):4d} 种值 {len(cats[k]):3d} | {top}")
        else:
            print(f"  {k:9s} 无")
    print("带 name 的分布:", dict(named))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
