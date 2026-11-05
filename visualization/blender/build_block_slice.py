"""Blender 端最小切片：一个路口街区，验证"数据 → 几何 → 材质 → 取景 → 出图"链路。

为什么先做切片
--------------
正式资产由 `generate_urban_twin.py` 从 `scene_layout.json` 批量生成（7 路 / 10 管线 /
7 设施 / 120 建筑 / 4 套场景）。改正式脚本前先用一个 ~30 对象的切片验证约定是否成立：

- 对象名 → 自定义属性 `twin_id` / `kind` / `state` / `evidence`（导出 glTF 进 extras）；
- 集合按 kind 分组（Roads / Pipelines / Facilities / Water / Buildings / Labels）；
- 材质名与正式脚本一致（Road_Normal / Road_Closed / FloodWater / PumpStation …）；
- 相机取景与正式脚本同一套算法（对准"有事的那块"，不是把整城塞进一帧）。

本文件是独立切片：几何来自写死的切片常量、不读 layout；辅助函数（COLORS/_material 等）与
`generate_urban_twin.py` 存在重复，属已知技术债，待合并到共享模块后再统一。约定一致，
所以切片通过 = 正式脚本的约定成立。

用法：
    # 无头（推荐，不占界面）
    blender -b -P visualization/blender/build_block_slice.py

    # 在当前已打开的 Blender 里直接建（Live 编辑器 / MCP execute_blender_code）
    exec(open(r"<repo>/visualization/blender/build_block_slice.py", encoding="utf-8").read())
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import (
    COLORS,
    _box,
    _camera_and_light,
    _cylinder,
    _label,
    _link,
    _material,
    _reset,
    _tag,
    render_preview as _render_preview,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "visualization" / "blender" / "scenes"
PREVIEW = OUT_DIR / "preview_block_slice.png"

# --- 切片常量（米） -------------------------------------------------------

BLOCK_HALF = 60.0
ROAD_WIDTH = 8.0
WATER_DEPTH_CM = 22.0
WATER_CENTER = (12.0, 7.0)
WATER_SIZE = (26.0, 17.0)


def _polygon(name, points, z, material, collection):
    """水面/地块是任意多边形，直接建面；缩放 cube 会带出厚度和法线问题。"""
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    vertices = [(item[0], item[1], z) for item in points]
    mesh.from_pydata(vertices, [], [list(range(len(vertices)))])
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


# --- 切片几何 -------------------------------------------------------------


def build() -> dict[str, int]:
    _reset()
    counts = {"road": 0, "pipeline": 0, "facility": 0, "water": 0, "cone": 0, "building": 0}

    ground_mat = _material("Ground", COLORS["ground"])
    road_normal = _material("Road_Normal", COLORS["road_normal"])
    road_closed = _material("Road_Closed", COLORS["road_closed"], emission=0.15)
    water_mat = _material("FloodWater", COLORS["water"], emission=0.25)
    cone_mat = _material("Cone", COLORS["cone"], emission=0.2)
    pipe_mat = _material("Pipe_Drainage", COLORS["drainage"])
    pump_mat = _material("PumpStation", COLORS["pump_station"])
    building_mat = _material("Building", COLORS["building"])

    # 地块
    _box("Ground_Block", (0.0, 0.0, -0.4), (BLOCK_HALF * 2, BLOCK_HALF * 2, 0.4),
         0.0, ground_mat, "Ground")
    # 文字是 3D 曲线，size 是米：写 22 就是 22 米高的字，比楼还高。片区标签单独放大抬高，
    # 其余标签压到 4~6 米，只做对象识别用。
    _label("DistrictLabel_A03", "A-03", (0.0, 0.0, 34.0), size=11.0)

    # 两条路：东西向已封（事件态），南北向正常（对照）
    roads = [
        ("RD-02", (-BLOCK_HALF, 0.0), (BLOCK_HALF, 0.0), "closed", road_closed),
        ("RD-05", (0.0, -BLOCK_HALF), (0.0, BLOCK_HALF), "normal", road_normal),
    ]
    for road_id, start, end, state, material in roads:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy) or 1.0
        obj = _box(
            f"Road_{road_id}",
            ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, 0.25),
            (length, ROAD_WIDTH, 0.5),
            math.atan2(dy, dx),
            material,
            "Roads",
        )
        _tag(obj, twin_id=f"TW-road-{road_id}", kind="road", state=state,
             evidence=f"工单 WO-20261029-007：{road_id} 状态 {state}")
        counts["road"] += 1
        _label(f"RoadLabel_{road_id}", road_id,
               (start[0] + dx * 0.75, start[1] + dy * 0.75, 5.0), size=5.0)

    # 封路锥桶：沿封闭段按 40m 间隔，和正式脚本同一套排布
    start, end = roads[0][1], roads[0][2]
    dx, dy = end[0] - start[0], end[1] - start[1]
    steps = max(3, int(math.hypot(dx, dy) // 30))
    for index in range(steps):
        ratio = (index + 0.5) / steps
        cx, cy = start[0] + dx * ratio, start[1] + dy * ratio
        cone = _cylinder(f"Cone_RD-02_{index:02d}", (cx, cy, 0.5), (cx, cy, 2.8),
                         0.55, cone_mat, "Roads")
        _tag(cone, twin_id=f"TW-cone-RD-02-{index}", kind="cone", state="closed")
        counts["cone"] += 1

    # 地下雨水管：z 取台账埋深的负值（-2.4m），和正式脚本口径一致
    pipe = _cylinder("Pipe_RD-02_drainage", (-BLOCK_HALF, 4.0, -2.4), (BLOCK_HALF, 4.0, -2.4),
                     0.4, pipe_mat, "Pipelines")
    _tag(pipe, twin_id="TW-drainage-DN800-01", kind="drainage", state="in_service",
         evidence="管线台账：DN800 雨水管，埋深 2.4m")
    counts["pipeline"] += 1

    # 泵站：footprint 8m，高 4m
    pump = _cylinder("Facility_PS-01", (-24.0, 15.0, 0.2), (-24.0, 15.0, 4.2),
                     4.0, pump_mat, "Facilities")
    _tag(pump, twin_id="TW-pump_station-PS-01", kind="pump_station", state="in_service",
         evidence="抽排设备台账：PS-01 流量 1200 m3/h")
    counts["facility"] += 1
    _label("FacilityLabel_PS-01", "PS-01 1200m3/h", (-24.0, 15.0, 7.0), size=4.0)

    # 积水：任意四边形，深度写进 state 与标签
    cx, cy = WATER_CENTER
    hx, hy = WATER_SIZE[0] / 2, WATER_SIZE[1] / 2
    patch = _polygon(
        "Water_WO-20261029-007",
        [(cx - hx, cy - hy), (cx + hx, cy - hy * 0.8), (cx + hx * 0.9, cy + hy), (cx - hx * 0.85, cy + hy)],
        1.2,
        water_mat,
        "Water",
    )
    _tag(patch, twin_id="TW-water-WO-20261029-007", kind="water",
         state=f"{WATER_DEPTH_CM:g}cm", evidence="工单：积水 22cm，已过封路阈值 25cm 边界")
    counts["water"] += 1
    _label("WaterLabel", f"{WATER_DEPTH_CM:g}cm", (cx, cy, 5.0), size=6.0)

    # 建筑：只放 3 栋做尺度参照，正式场景是 120 栋
    for index, (bx, by, w, d, h) in enumerate(
        [(32.0, -34.0, 14.0, 11.0, 16.0), (-31.0, -36.0, 12.0, 12.0, 22.0), (36.0, 30.0, 16.0, 12.0, 10.0)]
    ):
        obj = _box(f"Building_B-{index:02d}", (bx, by, h / 2), (w, d, h), 0.0,
                   building_mat, "Buildings")
        _tag(obj, twin_id=f"B-{index:02d}", kind="building", state="A-03")
        counts["building"] += 1

    _camera_and_light(
        center_x=0.0,
        center_y=0.0,
        span=BLOCK_HALF * 2,
        camera_name="SliceCamera",
        sun_name="SliceSun",
        lens=35,
        camera_offset=(0.55, -1.15, 1.15),
        clip_end_multiplier=8,
        world_name="SliceWorld",
        scene_props={"slice": "block-A03", "water_depth_cm": WATER_DEPTH_CM},
    )
    return counts


def render_preview() -> Path:
    return _render_preview(PREVIEW)


def main() -> int:
    counts = build()
    path = render_preview()
    print(
        f"[slice] 道路 {counts['road']} / 管线 {counts['pipeline']} / 设施 {counts['facility']} / "
        f"建筑 {counts['building']} / 水面 {counts['water']} / 锥桶 {counts['cone']}"
    )
    print(f"[slice] 预览图 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
