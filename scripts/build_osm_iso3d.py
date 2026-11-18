#!/usr/bin/env python3
"""OSM 底图 -> 等轴测（isometric）3D 城市图，纯 SVG 输出。

为什么做等轴测而不是 three.js：本机 8G 内存带不动 WebGL 场景，
而等轴测图是纯多边形，浏览器渲染开销接近零，还能直接转 PNG 进 PPT。

投影：真等轴测
    sx = (x - y) * cos45
    sy = (x + y) * sin45 * sin35.26  -  z * cos35.26
三个轴的投影长度都是 0.816，比例不失真——不需要手工夸张高度，也就不用为
「图上的楼比实际高」写免责声明。

遮挡：画家算法。建筑按质心 (x+y) 升序绘制，远处先画、近处后画覆盖。
单栋楼内先画侧面（只画外法线朝向观察者的那些边），顶面最后画。

数据来源与口径：
  - 建筑高度 height_m 来自 base_buildings_full.json 的 height_source：
    osm_height（OSM 实测标签）/ osm_levels（层数 × 3.2m）/ inferred_type（按类型推定）
  - 三种来源在图上用同一套配色（区分会让图变花），但图例标注了推定占比——
    把推定值当实测值用是这个项目最不该犯的错。
  - OpenStreetMap © contributors, ODbL 1.0

用法：
    .venv/Scripts/python.exe scripts/build_osm_iso3d.py
    .venv/Scripts/python.exe scripts/build_osm_iso3d.py --width 2000 --z-scale 1.0
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OSM = ROOT / "out" / "osm"
BLD = OSM / "base_buildings_full.json"
ROADS = OSM / "base_roads.json"
AREAS = OSM / "base_areas.json"
OUT = OSM / "iso3d.svg"

# 真等轴测的三个系数
COS45 = math.cos(math.radians(45))                  # 0.70711
SIN45_SIN3526 = math.sin(math.radians(45)) * math.sin(math.radians(35.264))
COS3526 = math.cos(math.radians(35.264))            # 0.81650

# --- 配色：冷灰蓝，浅色地面 + 明显深于地面的体块 ---
# 第一版配色（地面 #f4f6f9 / 顶面 #dfe4ec）在等轴测里几乎融成一片，看着像雾。
# 2D 地图能靠细线拉对比，等轴测没有线，只能靠明度差，所以顶面/侧面都要压深。
C_GROUND = "#eef1f5"
C_LANDUSE = "#dde4ec"
C_GREEN = "#cfe0c8"
C_WATER = "#a9cfe1"
C_ROAD = "#ffffff"
C_ROAD_EDGE = "#c3ccd9"
C_TOP = "#c7d2e1"         # 建筑顶面（低层）
C_TOP_MID = "#b5c5dd"     # 中高层顶面
C_TOP_HIGH = "#9db2d6"    # 超高层顶面：越高越深，天际线才有层次
C_SIDE_A = "#a6b3c8"      # 朝 +x 的侧面（受光）
C_SIDE_B = "#8290a8"      # 朝 +y 的侧面（背光）
C_TALL = "#9db2d6"        # 兼容旧名
C_SHADOW = "#3b4658"      # 接地阴影
C_LABEL = "#4a5361"
C_HINT = "#7b8492"


def project(x: float, y: float, z: float = 0.0, z_scale: float = 1.0) -> tuple[float, float]:
    """世界坐标（米）-> SVG 坐标（y 向下）。"""
    sx = (x - y) * COS45
    sy = (x + y) * SIN45_SIN3526 - z * z_scale * COS3526
    return sx, -sy


def signed_area(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def ensure_ccw(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """统一成逆时针，侧面可见性判断才有一致的法线方向。"""
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts if signed_area(pts) > 0 else pts[::-1]


def _dp(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker 简化。1~2m 的误差在 1600px 的全景图上不到 1 像素。"""
    if len(pts) < 3:
        return pts
    x1, y1 = pts[0]
    x2, y2 = pts[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    best_i, best_d = -1, -1.0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if norm < 1e-9:
            d = math.hypot(px - x1, py - y1)
        else:
            d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
        if d > best_d:
            best_i, best_d = i, d
    if best_d <= tol:
        return [pts[0], pts[-1]]
    left = _dp(pts[: best_i + 1], tol)
    right = _dp(pts[best_i:], tol)
    return left[:-1] + right


def simplify_ring(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """闭合环简化：从最远点对切成两段分别简化，避免起点被当成端点。"""
    if len(pts) < 6 or tol <= 0:
        return pts
    # 找离 pts[0] 最远的点，切成两段
    x0, y0 = pts[0]
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
    a = _dp(pts[: far + 1], tol)
    b = _dp(pts[far:] + [pts[0]], tol)
    out = a[:-1] + b[:-1]
    return out if len(out) >= 3 else pts


def fmt(v: float) -> str:
    """坐标保留 1 位小数，够用且能显著压小文件。"""
    return f"{v:.1f}"


def path_of(pts: list[tuple[float, float]], close: bool = True) -> str:
    head = f"M{fmt(pts[0][0])} {fmt(pts[0][1])}"
    body = "".join(f"L{fmt(p[0])} {fmt(p[1])}" for p in pts[1:])
    return head + body + ("Z" if close else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OSM -> 等轴测 3D 城市图（SVG）")
    ap.add_argument("--buildings", type=Path, default=BLD)
    ap.add_argument("--roads", type=Path, default=ROADS)
    ap.add_argument("--areas", type=Path, default=AREAS)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--width", type=int, default=1800, help="输出像素宽")
    ap.add_argument("--z-scale", type=float, default=1.0,
                    help="高度夸张系数（1.0=真实比例，等轴测本身已不失真）")
    ap.add_argument("--simplify", type=float, default=1.2,
                    help="轮廓简化容差（米），0 关闭")
    ap.add_argument("--min-height", type=float, default=3.0,
                    help="低于这个高度的建筑按面片画（雨棚/围墙不画体块）")
    args = ap.parse_args(argv)

    for p in (args.buildings, args.roads, args.areas):
        if not p.is_file():
            print(f"缺输入：{p}")
            return 2

    bdata = json.loads(args.buildings.read_text(encoding="utf-8"))
    rdata = json.loads(args.roads.read_text(encoding="utf-8"))
    adata = json.loads(args.areas.read_text(encoding="utf-8"))
    buildings = bdata.get("buildings") or []
    roads = rdata.get("roads") or []
    areas = adata.get("areas") or []

    # --- 包围盒：只用建筑定视野（含底面与顶面） ---
    # 道路不参与定视野。Overpass 的 way 只要有一个节点落在 bbox 内就整条保留，
    # 端点可能远在城市另一头——用它定视野，画面会被几条长路撑成一大片空白。
    # 超出视野的道路段由后面的 clipPath 裁掉。
    xs: list[float] = []
    ys: list[float] = []
    for b in buildings:
        h = float(b.get("height_m") or 0.0)
        for pt in b["footprint"]:
            for z in (0.0, h):
                sx, sy = project(pt["x"], pt["y"], z, args.z_scale)
                xs.append(sx)
                ys.append(sy)
    for a in areas:  # 地块一并算，避免边缘的水面/绿地被裁掉
        for pt in a.get("polygon") or []:
            sx, sy = project(pt["x"], pt["y"], 0, args.z_scale)
            xs.append(sx)
            ys.append(sy)
    if not xs:
        print("没有可绘制的要素")
        return 1

    pad = 40.0
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    span_x, span_y = max_x - min_x, max_y - min_y
    # 底部标注区：按字号算，不能按画幅比例拍脑袋——第一版用 span_y*0.10，
    # 图例 6 项放不下，直接压到标题行上叠成一坨。
    fs = span_x * 0.0098
    lh = fs * 1.5
    foot_h = fs * 5.4
    view_h = span_y + foot_h
    height_px = int(args.width * view_h / span_x)

    def P(x: float, y: float, z: float = 0.0) -> tuple[float, float]:
        return project(x, y, z, args.z_scale)

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{fmt(min_x)} {fmt(min_y)} {fmt(span_x)} {fmt(view_h)}" '
        f'width="{args.width}" height="{height_px}" '
        f'font-family="Microsoft YaHei, PingFang SC, sans-serif">'
    )
    out.append("<title>成都中心城区等轴测体块图（OpenStreetMap）</title>")
    # 地面
    out.append(
        f'<rect x="{fmt(min_x)}" y="{fmt(min_y)}" width="{fmt(span_x)}" '
        f'height="{fmt(view_h)}" fill="{C_GROUND}"/>'
    )

    # --- 地块 / 绿地 / 水体（都是地面上的平面，直接投影 z=0） ---
    layer_area: list[str] = []
    for a in areas:
        poly = a.get("polygon") or []
        if len(poly) < 3:
            continue
        pts = ensure_ccw([(p["x"], p["y"]) for p in poly])
        if len(pts) < 3:
            continue
        sub = a.get("subkind", "")
        kind = a.get("kind", "")
        if kind == "natural" and sub in ("water",):
            fill, op = C_WATER, "0.95"
        elif kind in ("leisure", "natural") or sub in ("grass", "wood", "forest"):
            fill, op = C_GREEN, "0.85"
        else:
            fill, op = C_LANDUSE, "0.75"
        d = path_of([P(x, y) for x, y in pts])
        for hole in a.get("holes") or []:
            hp = ensure_ccw([(p["x"], p["y"]) for p in hole])
            if len(hp) >= 3:
                d += path_of([P(x, y) for x, y in hp])
        layer_area.append(
            f'<path d="{d}" fill="{fill}" fill-opacity="{op}" fill-rule="evenodd" stroke="none"/>'
        )
    # 地块与道路超出视野的部分裁掉：视野由建筑定，长路会伸出画面
    out.append(
        f'<defs><clipPath id="frame">'
        f'<rect x="{fmt(min_x)}" y="{fmt(min_y)}" width="{fmt(span_x)}" height="{fmt(span_y)}"/>'
        f'</clipPath></defs>'
    )
    out.append('<g clip-path="url(#frame)">')
    out.extend(layer_area)

    # --- 道路（白底 + 细描边，压在建筑之下） ---
    layer_road: list[str] = []
    for r in roads:
        line = r.get("polyline") or []
        if len(line) < 2:
            continue
        try:
            lanes = int(str(r.get("lanes") or "").split(";")[0])
        except ValueError:
            lanes = 0
        base_w = 28.0 if r.get("level") == 0 else 16.0
        w = lanes * 3.4 if lanes else base_w
        w = max(8.0, min(w, 46.0))
        if r.get("tunnel"):
            continue  # 隧道在地面下，等轴测里看不见，画了反而错
        pts = [P(p["x"], p["y"]) for p in line]
        d = path_of(pts, close=False)
        layer_road.append(
            f'<path d="{d}" fill="none" stroke="{C_ROAD_EDGE}" '
            f'stroke-width="{fmt(w + 2.0)}" stroke-linecap="round" stroke-linejoin="round"/>'
        )
        layer_road.append(
            f'<path d="{d}" fill="none" stroke="{C_ROAD}" '
            f'stroke-width="{fmt(w)}" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    out.extend(layer_road)
    out.append("</g>")

    # --- 建筑：按质心 (x+y) 升序，远先近后 ---
    def depth_key(b: dict) -> float:
        fp = b["footprint"]
        n = len(fp)
        return sum(p["x"] + p["y"] for p in fp) / n

    drawn = 0
    sides_n = 0
    for b in sorted(buildings, key=depth_key):
        h = float(b.get("height_m") or 0.0)
        raw = [(p["x"], p["y"]) for p in b["footprint"]]
        if len(raw) < 3:
            continue
        ring = ensure_ccw(raw)
        if args.simplify > 0:
            ring = ensure_ccw(simplify_ring(ring, args.simplify))
        n = len(ring)
        if n < 3:
            continue

        # 顶面按高度分档：同色时高低楼分不出来，一眼是「一片灰盒子」。
        # 分三档后地标会自己跳出来，比加标注省事也更诚实（只改色不改形）。
        if h >= 120:
            top_fill = C_TOP_HIGH
        elif h >= 50:
            top_fill = C_TOP_MID
        else:
            top_fill = C_TOP

        if h < args.min_height:
            # 太矮的按地面色块画，不画体块（否则满屏小碎片）
            pts = [P(x, y) for x, y in ring]
            out.append(
                f'<path d="{path_of(pts)}" fill="{C_LANDUSE}" stroke="none"/>'
            )
            drawn += 1
            continue

        # 接地阴影：偏移量按高度缩放。固定偏移时 248m 的 IFS 和 12m 的老楼影子
        # 一样长，一眼就假；按高度给，高楼拖长影、矮楼留短影。
        sh_dx = h * 0.28 + 2.0
        sh_dy = h * 0.16 + 1.5
        sh = [P(x + sh_dx, y + sh_dy) for x, y in ring]
        out.append(
            f'<path d="{path_of(sh)}" fill="{C_SHADOW}" fill-opacity="0.28" stroke="none"/>'
        )

        # 侧面：外法线 (dy, -dx)，朝观察者（+x,+y）即 nx+ny>0
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            dx, dy = x2 - x1, y2 - y1
            if dy - dx <= 0:
                continue
            quad = [P(x1, y1), P(x2, y2), P(x2, y2, h), P(x1, y1, h)]
            color = C_SIDE_A if abs(dy) > abs(dx) else C_SIDE_B
            out.append(
                f'<path d="{path_of(quad)}" fill="{color}" stroke="none"/>'
            )
            sides_n += 1
        # 顶面最后画
        top = [P(x, y, h) for x, y in ring]
        out.append(
            f'<path d="{path_of(top)}" fill="{top_fill}" stroke="{C_SIDE_B}" '
            f'stroke-width="0.6" stroke-opacity="0.5"/>'
        )
        drawn += 1

    # --- 底部标注区：左侧标题与口径，右侧图例（左右分栏，互不重叠） ---
    from collections import Counter
    src = Counter(b.get("height_source") for b in buildings)
    total = max(1, len(buildings))
    note1 = (f"成都中心城区等轴测体块图 · {len(buildings)} 栋建筑 / {len(roads)} 条主次干道 / "
             f"{len(areas)} 个地块")
    note2 = (f"建筑高度：OSM 实测 {src['osm_height']} 栋（{src['osm_height']/total*100:.0f}%）、"
             f"层数×3.2m 换算 {src['osm_levels']} 栋（{src['osm_levels']/total*100:.0f}%）、"
             f"按建筑类型推定 {src['inferred_type']} 栋（{src['inferred_type']/total*100:.0f}%，非实测）")
    note3 = "地图数据 © OpenStreetMap contributors，ODbL 1.0 · 投影为真等轴测，高度未夸张"

    tx = min_x + span_x * 0.012
    base_y = max_y + fs * 1.7
    out.append(
        f'<text x="{fmt(tx)}" y="{fmt(base_y)}" font-size="{fmt(fs*1.12)}" '
        f'fill="{C_LABEL}" font-weight="600">{note1}</text>'
    )
    out.append(
        f'<text x="{fmt(tx)}" y="{fmt(base_y + fs*1.45)}" font-size="{fmt(fs*0.95)}" '
        f'fill="{C_HINT}">{note2}</text>'
    )
    out.append(
        f'<text x="{fmt(tx)}" y="{fmt(base_y + fs*2.7)}" font-size="{fmt(fs*0.88)}" '
        f'fill="{C_HINT}">{note3}</text>'
    )

    items = [
        (C_TOP, "建筑顶面"),
        (C_SIDE_A, "受光面"),
        (C_SIDE_B, "背光面"),
        (C_WATER, "水体"),
        (C_GREEN, "绿地"),
        (C_ROAD, "道路"),
    ]
    lg_x = min_x + span_x * 0.655
    lg_y = base_y - fs * 1.0
    sw = fs * 0.9
    col_w = span_x * 0.112
    for i, (col, label) in enumerate(items):
        ci, row = i % 2, i // 2
        x = lg_x + ci * col_w
        y = lg_y + row * lh
        out.append(
            f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(sw)}" height="{fmt(sw)}" '
            f'fill="{col}" stroke="{C_SIDE_B}" stroke-width="0.4"/>'
        )
        out.append(
            f'<text x="{fmt(x + sw * 1.6)}" y="{fmt(y + sw * 0.9)}" '
            f'font-size="{fmt(fs)}" fill="{C_LABEL}">{label}</text>'
        )

    out.append("</svg>")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(out), encoding="utf-8")

    print(f"written: {args.out} ({args.out.stat().st_size/1e6:.2f} MB)")
    print(f"  建筑 {drawn} 栋（其中体块 {drawn - sum(1 for b in buildings if (b.get('height_m') or 0) < args.min_height)} 栋）")
    print(f"  侧面多边形 {sides_n} 个  道路段 {len(roads)}  地块 {len(areas)}")
    print(f"  viewBox {span_x:.0f} × {view_h:.0f} 米 -> {args.width} × {height_px} px")
    print(f"  高度来源 {dict(src)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
