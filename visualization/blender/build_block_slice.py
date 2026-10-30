"""Blender 端最小切片：一个路口街区，验证"数据 → 几何 → 材质 → 取景 → 出图"链路。

为什么先做切片
--------------
正式资产由 `generate_urban_twin.py` 从 `scene_layout.json` 批量生成（7 路 / 8 管线 /
6 设施 / 120 建筑 / 4 套场景）。改正式脚本前先用一个 ~30 对象的切片验证约定是否成立：

- 对象名 → 自定义属性 `twin_id` / `kind` / `state` / `evidence`（导出 glTF 进 extras）；
- 集合按 kind 分组（Roads / Pipelines / Facilities / Water / Buildings / Labels）；
- 材质名与正式脚本一致（Road_Normal / Road_Closed / FloodWater / PumpStation …）；
- 相机取景与正式脚本同一套算法（对准"有事的那块"，不是把整城塞进一帧）。

本文件与正式脚本的差别只有一处：几何来自写死的切片常量，不读 layout。两者共享
同样的约定，所以切片通过 = 正式脚本的约定成立。

用法：
    # 无头（推荐，不占界面）
    blender -b -P visualization/blender/build_block_slice.py

    # 在当前已打开的 Blender 里直接建（Live 编辑器 / MCP execute_blender_code）
    exec(open(r"<repo>/visualization/blender/build_block_slice.py", encoding="utf-8").read())
"""

from __future__ import annotations

import math
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "visualization" / "blender" / "scenes"
PREVIEW = OUT_DIR / "preview_block_slice.png"

# 与 generate_urban_twin.py 的 COLORS 保持一致，切片和正式资产不能有两种红
COLORS = {
    "road_normal": (0.13, 0.14, 0.16, 1.0),
    "road_closed": (0.72, 0.10, 0.10, 1.0),
    "road_construction": (0.78, 0.62, 0.08, 1.0),
    "water": (0.04, 0.42, 0.95, 0.92),
    "building": (0.62, 0.65, 0.70, 1.0),
    "ground": (0.15, 0.16, 0.19, 1.0),
    "drainage": (0.10, 0.65, 0.85, 1.0),
    "pump_station": (0.95, 0.95, 0.95, 1.0),
    "cone": (0.95, 0.35, 0.10, 1.0),
}

# --- 切片常量（米） -------------------------------------------------------

BLOCK_HALF = 60.0
ROAD_WIDTH = 8.0
WATER_DEPTH_CM = 22.0
WATER_CENTER = (12.0, 7.0)
WATER_SIZE = (26.0, 17.0)


# --- 基础设施（从正式脚本抄写法，不另起一套） -----------------------------


def _material(name: str, color: tuple[float, float, float, float], *, emission: float = 0.0):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    # 按节点类型找，不按名字找：中文界面下节点名是"原理化 BSDF"
    bsdf = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        material.use_nodes = True
        bsdf = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    for key, value in (("Base Color", color), ("Alpha", color[3]), ("Roughness", 0.65)):
        socket = bsdf.inputs.get(key)
        if socket is not None:
            socket.default_value = value
    if emission:
        for key, value in (("Emission Color", color), ("Emission Strength", emission)):
            socket = bsdf.inputs.get(key)
            if socket is not None:
                socket.default_value = value
    if color[3] < 1.0 and hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    return material


def _collection(name: str):
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _link(obj, collection_name: str) -> None:
    collection = _collection(collection_name)
    for existing in list(obj.users_collection):
        existing.objects.unlink(obj)
    collection.objects.link(obj)


def _tag(obj, *, twin_id: str, kind: str, state: str, evidence: str = "") -> None:
    obj["twin_id"] = twin_id
    obj["kind"] = kind
    obj["state"] = state
    if evidence:
        obj["evidence"] = evidence


def _box(name, center, size, rotation_z, material, collection):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.scale = size
    obj.rotation_euler = (0.0, 0.0, rotation_z)
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


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


def _cylinder(name, start, end, radius, material, collection):
    dx, dy, dz = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
    length = math.sqrt(dx * dx + dy * dy + dz * dz) or 0.001
    location = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, (start[2] + end[2]) / 2)
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, vertices=16, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.rotation_euler = (
        0.0,
        math.acos(max(-1.0, min(1.0, dz / length))),
        math.atan2(dy, dx),
    )
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


def _label(name, text, location, size=10.0):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = text
    curve.size = size
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    obj.data.materials.append(_material("Label", (1.0, 1.0, 1.0, 1.0), emission=1.2))
    # 文字是自发光标牌，不该投影：悬空的 14m 大字会在路面投出一个"第二个标签"，
    # 看图时会被误读成重复对象。
    obj.visible_shadow = False
    _link(obj, "Labels")
    return obj


def _reset() -> None:
    """清空场景。Live 编辑器里跑也一样，所以切片脚本可以反复执行。"""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        if collection.name != "Collection":
            bpy.data.collections.remove(collection)
    for material in list(bpy.data.materials):
        bpy.data.materials.remove(material)


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

    _camera_and_light()
    return counts


def _camera_and_light() -> None:
    """取景对准积水片区，不是把整块地塞进一帧（塞进去路面只有几个像素）。

    相机必须站在街区**外面**：放在街区内会被前景建筑挡住路口。这里用东南 3/4 俯视，
    仰角 ~45°，距离 ~1.7 倍街区尺寸；35mm 在 200m 外横向覆盖约 210m，120m 街区带边距。
    """
    center = (0.0, 0.0)
    span = BLOCK_HALF * 2
    location = (center[0] + span * 0.55, center[1] - span * 1.15, span * 1.15)

    bpy.ops.object.camera_add(location=location)
    camera = bpy.context.object
    camera.name = "SliceCamera"
    camera.data.lens = 35
    # 默认裁切面 1000m，相机在 2000m 外会把整块地裁掉——只看到背景色
    camera.data.clip_start = 1.0
    camera.data.clip_end = span * 8
    from mathutils import Vector

    direction = Vector((center[0], center[1], 0.0)) - Vector(location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera

    bpy.ops.object.light_add(type="SUN", location=(center[0], center[1] - span * 0.4, span))
    sun = bpy.context.object
    sun.name = "SliceSun"
    sun.data.energy = 3.4
    sun.rotation_euler = (math.radians(52), 0.0, math.radians(35))

    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("SliceWorld")
        bpy.context.scene.world = world
    world.use_nodes = True
    background = next((node for node in world.node_tree.nodes if node.type == "BACKGROUND"), None)
    if background is not None:
        background.inputs["Color"].default_value = (0.28, 0.36, 0.46, 1.0)
        background.inputs["Strength"].default_value = 1.5

    scene = bpy.context.scene
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    scene["slice"] = "block-A03"
    scene["water_depth_cm"] = WATER_DEPTH_CM


def render_preview() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(PREVIEW)
    bpy.ops.render.render(write_still=True)
    return PREVIEW


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
