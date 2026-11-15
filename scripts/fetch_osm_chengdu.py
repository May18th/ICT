#!/usr/bin/env python3
"""下载成都中心 OSM 底图数据，供俯视图叠真实底图用。

投影参数（来自 samples/assets/urban/semantic_entities_legacy_osm.json 的 projection 字段）：
  lat0 = 30.657, lon0 = 104.066   （成都市中心）
  k_lat = 110540.0, k_lon = 95761.381
  即 示意图坐标 (x, y) -> 真实经纬度：
    lat = lat0 + y / k_lat
    lon = lon0 + x / k_lon
  （scene_layout.json 的 x/y 单位是米，k 是每度的米数）

OSM 数据源：OpenStreetMap，ODbL 1.0 授权，需标注 © OpenStreetMap contributors。

用法：
  python scripts/fetch_osm_chengdu.py
产物：out/osm/chengdu_centre_raw.json（overpass JSON 原始格式）
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "osm" / "chengdu_centre_raw.json"

LAT0, LON0 = 30.657, 104.066

# 覆盖两个片区（A-03 在负 x 侧约 -1430~-150，B-01 在正 x 侧约 540~1430）：
# 示意图坐标范围 x -1430~1430, y -472~472，换算成经纬度跨度约 lon 3km / lat 0.85km。
# 这里取稍大的包围盒。
QUERY = (
    '[out:json][timeout:50];'
    '(way["highway"](30.63,104.04,30.68,104.10);'
    'way["building"](30.63,104.04,30.68,104.10);'
    'way["waterway"](30.63,104.04,30.68,104.10);'
    'way["natural"="water"](30.63,104.04,30.68,104.10);'
    'way["landuse"](30.63,104.04,30.68,104.10);'
    'relation["boundary"](30.63,104.04,30.68,104.10););'
    'out body;>;out skel qt;'
)

HOSTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = urllib.parse.urlencode({"data": QUERY}).encode()
    for host in HOSTS:
        try:
            req = urllib.request.Request(host, data=data, headers={"User-Agent": "wb-map2d/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            if len(raw) < 500:
                print(f"{host}: 响应过小({len(raw)}B)，跳过")
                continue
            OUT.write_bytes(raw)
            d = json.loads(raw)
            n_ways = len(d.get("elements", []))
            print(f"OK {host}: {len(raw)} bytes, {n_ways} elements -> {OUT}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"{host}: {type(e).__name__} {e}")
            time.sleep(1)
    print("所有 overpass 实例均失败（502/504/超时）。可能是网络限制，稍后重试或本机执行。")


if __name__ == "__main__":
    main()
