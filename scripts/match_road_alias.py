#!/usr/bin/env python3
"""给示意路网配上「最近的真实道路」，作为展示层的对照表。

为什么需要：场景里 7 条路名（兴华路、建设大道…）在 OSM 里不存在，而底图是真实
成都路网。评委看图第一反应是「这条路在成都哪儿」。改名代价极高（40+ 个文件、
4 套场景副本、通知文书、测试、平台 Agent 提示词），所以不动数据；改成在展示层
补一张对照表——鼠标放到示意路上就能看到它贴着哪条真实路、偏差多少米。

产物 samples/assets/osm/road_alias.json。**纯新增数据，不参与任何推演与判据。**

数据来源：OpenStreetMap（ODbL 1.0）+ scene_layout.json。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
BASEMAP = ROOT / "samples" / "assets" / "osm" / "base_roads.json"
OUT = ROOT / "samples" / "assets" / "osm" / "road_alias.json"

# 采样步长（米）。40m 对这个尺度够密：路宽 7~21m，40m 一个点不会漏掉弯道。
STEP_M = 40.0


def sample(polyline: list[dict], step: float = STEP_M) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i in range(len(polyline) - 1):
        x1, y1 = polyline[i]["x"], polyline[i]["y"]
        x2, y2 = polyline[i + 1]["x"], polyline[i + 1]["y"]
        seg = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(seg / step))
        for k in range(n):
            t = k / n
            out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    if polyline:
        out.append((polyline[-1]["x"], polyline[-1]["y"]))
    return out


def main() -> int:
    if not BASEMAP.is_file():
        raise SystemExit(f"缺 {BASEMAP}（先用 scripts/build_osm_basemap.py 出底图）")
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    basemap = json.loads(BASEMAP.read_text(encoding="utf-8"))

    # 真实路网顶点表。只保留有名字的——无名路对读者没意义。
    real: list[tuple[float, float, str, int]] = []
    for r in basemap.get("roads", []):
        name = r.get("name") or ""
        if not name:
            continue
        for p in r.get("polyline", []):
            real.append((p["x"], p["y"], name, r["id"]))
    if not real:
        raise SystemExit("底图里没有带名字的道路")

    rows = []
    for road in layout.get("roads", []):
        pts = sample(road.get("polyline") or [])
        if not pts:
            continue
        dists: list[float] = []
        tally: dict[str, int] = {}
        for x, y in pts:
            best = min(real, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
            dists.append(math.hypot(best[0] - x, best[1] - y))
            tally[best[2]] = tally.get(best[2], 0) + 1
        dists.sort()
        nearest = max(tally.items(), key=lambda kv: kv[1])[0]
        rows.append({
            "road_id": road.get("road_id"),
            "name": road.get("name"),
            "real_name": nearest,
            "median_m": round(dists[len(dists) // 2]),
            "p90_m": round(dists[min(len(dists) - 1, int(len(dists) * 0.9))]),
            "max_m": round(dists[-1]),
            "samples": len(pts),
        })

    rows.sort(key=lambda r: r["median_m"])
    payload = {
        "source": "示意路网来自 scene_layout.json；真实道路来自 OpenStreetMap © "
                  "contributors（ODbL 1.0）",
        "method": f"沿示意路每 {STEP_M:.0f} 米取点，找最近的真实道路顶点，取距离中位数",
        "note": "只用于展示层对照，不参与推演、不参与任何判据",
        "roads": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"写出 {OUT}")
    print(f"{'示意路':10s}{'真实道路':16s}{'中位':>6s}{'P90':>6s}")
    for r in rows:
        print(f"{r['name']:10s}{r['real_name']:16s}{r['median_m']:5d}m{r['p90_m']:5d}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
