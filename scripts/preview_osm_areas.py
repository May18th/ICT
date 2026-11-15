#!/usr/bin/env python3
"""把 base_areas.json 画成一张预览图，用来核对位置是否正确。

为什么需要这一步：坐标投影写错的时候，数据看着「有 120 个面、0.08MB」完全正常，
但画出来会整体偏移或者颠倒（纬度往北是 y 正方向，屏幕 y 往下，很容易翻）。
这一步只做核对，不进演示。

产物 out/osm/preview_areas.svg —— 直接双击或用无头浏览器截图看。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "out" / "osm" / "base_areas.json"
ROADS = ROOT / "out" / "osm" / "base_roads.json"
BUILDINGS = ROOT / "out" / "osm" / "base_buildings_full.json"
if not BUILDINGS.is_file():   # 没有全宽版就退回中心区那份
    BUILDINGS = ROOT / "out" / "osm" / "base_buildings.json"
OUT = ROOT / "out" / "osm" / "preview_areas.svg"

# 业务范围（scene_layout bounds），画个框出来对照
BIZ = (-1469.5, -512.1, 1469.5, 512.1)

# kind/subkind -> 填充色。底图不上饱和度，避免抢业务层。
COLOR = {
    ("natural", "water"): "#bcd8ea",
    ("natural", "wood"): "#cfe0c6",
    ("natural", "scrub"): "#d8e6ce",
    ("natural", "grassland"): "#dcebd2",
    ("landuse", "residential"): "#eef1f6",
    ("landuse", "commercial"): "#f2edf7",
    ("landuse", "retail"): "#f2edf7",
    ("landuse", "industrial"): "#eeeff1",
    ("landuse", "construction"): "#f7ecdf",
    ("landuse", "brownfield"): "#f5efe6",
    ("landuse", "railway"): "#e9eaee",
    ("landuse", "institutional"): "#edf0f5",
    ("landuse", "education"): "#eaf3ea",
    ("landuse", "military"): "#ececec",
    ("landuse", "grass"): "#dcebd2",
    ("landuse", "village_green"): "#dcebd2",
    ("landuse", "recreation_ground"): "#dcebd2",
    ("leisure", "park"): "#d6e8ca",
    ("leisure", "garden"): "#d9ebcf",
    ("leisure", "pitch"): "#d2e4c6",
    ("leisure", "track"): "#e4e7ea",
    ("leisure", "playground"): "#f0e8dd",
    ("leisure", "sports_centre"): "#e2e9de",
    ("amenity", "school"): "#e8f0ea",
    ("amenity", "university"): "#e8f0ea",
    ("amenity", "college"): "#e8f0ea",
    ("amenity", "hospital"): "#f8ecec",
    ("amenity", "parking"): "#f0f0f2",
    ("amenity", "marketplace"): "#f4f0e6",
    ("amenity", "fountain"): "#bcd8ea",
    ("railway", "platform"): "#e6e6ea",
}
DEFAULT = "#f0f2f5"


def main() -> int:
    if not SRC.is_file():
        print(f"缺 {SRC}", file=sys.stderr)
        return 2
    d = json.loads(SRC.read_text(encoding="utf-8"))
    roads = json.loads(ROADS.read_text(encoding="utf-8")) if ROADS.is_file() else {"roads": []}
    blds = (json.loads(BUILDINGS.read_text(encoding="utf-8"))
            if BUILDINGS.is_file() else {"buildings": []})

    pad = 220
    x0, y0, x1, y1 = BIZ
    vb_x, vb_y = x0 - pad, -(y1 + pad)   # 屏幕 y 向下，所以要取负
    vb_w, vb_h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad

    def px(p):
        return f'{p["x"]:.1f},{-p["y"]:.1f}'

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb_x:.1f} {vb_y:.1f} '
        f'{vb_w:.1f} {vb_h:.1f}" width="1580" height="620">',
        '<rect x="-99999" y="-99999" width="199998" height="199998" fill="#f7f8fa"/>',
    ]
    # 地块（面）
    for a in d.get("areas", []):
        col = COLOR.get((a["kind"], a["subkind"]), DEFAULT)
        pts = " ".join(px(p) for p in a["polygon"])
        parts.append(f'<polygon points="{pts}" fill="{col}" stroke="#d7dce3" '
                     f'stroke-width="1.2" stroke-opacity=".7"/>')
    # 轨道 / 河道（线）
    for l in d.get("lines", []):
        pts = " ".join(px(p) for p in l["polyline"])
        if l["kind"] == "waterway":
            parts.append(f'<polyline points="{pts}" fill="none" stroke="#9dc5de" '
                         f'stroke-width="9" stroke-linecap="round"/>')
        else:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="#b9bcc4" '
                         f'stroke-width="4" stroke-dasharray="14 8"/>')
    # 建筑：中心区 OSM 常常只有 building 没有 landuse，不叠建筑会看到大片空白，
    # 误判成"这片没数据"。
    for b in blds.get("buildings", []):
        pts = " ".join(px(p) for p in b["footprint"])
        parts.append(f'<polygon points="{pts}" fill="#dfe3e9" stroke="#ccd2da" '
                     f'stroke-width="0.6"/>')
    # 参考路网（base_roads，真实 OSM 主干）
    for r in roads.get("roads", []):
        pts = " ".join(px(p) for p in r["polyline"])
        w = 7 if r["level"] == 0 else 5
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#c9d1da" '
                     f'stroke-width="{w}" stroke-linecap="round"/>')
    # 业务范围框
    parts.append(
        f'<rect x="{x0}" y="{-y1}" width="{x1-x0}" height="{y1-y0}" fill="none" '
        f'stroke="#e05b5b" stroke-width="3" stroke-dasharray="18 10"/>'
    )
    parts.append(
        f'<text x="{x0+14}" y="{-y1+40}" font-family="sans-serif" font-size="34" '
        f'fill="#e05b5b">业务范围 x±1469.5m / y±512.1m</text>'
    )
    parts.append("</svg>")
    OUT.write_text("".join(parts), encoding="utf-8")

    print(f"写出 {OUT}（{OUT.stat().st_size/1e3:.1f} KB）")
    print(f"  areas={len(d.get('areas',[]))}  lines={len(d.get('lines',[]))}  "
          f"roads={len(roads.get('roads',[]))}")

    png = OUT.with_suffix(".png")
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if edge.is_file():
        subprocess.run(
            [str(edge), "--headless=new", "--disable-gpu",
             "--enable-unsafe-swiftshader", "--window-size=1600,660",
             f"--screenshot={png}", OUT.as_uri()],
            capture_output=True, timeout=90,
        )
        print(f"  截图 {png}" if png.is_file() else "  截图失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
