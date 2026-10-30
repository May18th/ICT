"""Blender 端：把 scene_layout.json 变成可复用的三维资产（离线、程序化、可批量）。

分工在这里落地：Blender 只做"离线建模与资产预处理"，不做推演、不连接服务。输入是
`src/decision_poc/scene_layout.py` 算出的引擎无关布局，输出三样东西：

| 产物 | 用途 |
| --- | --- |
| `scenes/*.blend` | 可继续编辑的源场景（材质、集合、命名都在） |
| `scenes/*.glb` | 给 Unreal / 其它引擎的交换格式，对象名即 twin_id |
| `scenes/asset_manifest.json` | 资产清单：每个文件对应哪个场景、包含哪些对象、各是什么状态 |

四套预设场景对应任务书里的"批量生成不同内涝场景"：

- `normal`：不铺水面、不画封路，用作对照；
- `flood`：把工单里的积水铺成水面，标注深度；
- `closure`：积水 + 封路（红色路面 + 锥桶）；
- `construction`：在 closure 基础上保留占道施工段（黄色路面色带）。

用法（不用开界面）：

    blender -b -P visualization/blender/generate_urban_twin.py -- --scenarios all
    blender -b -P visualization/blender/generate_urban_twin.py -- --scenario flood --preview
    blender -b -P visualization/blender/generate_urban_twin.py -- --scenarios all --no-glb
    blender -b -P visualization/blender/generate_urban_twin.py -- --scenarios all --focus B-01

`--preview` 额外渲一张 `scenes/preview_<scenario>.png`；`--focus` 指定取景对准哪个
片区，缺省取最深积水所在片区（A-03）。

在已打开的 Live 编辑器里跑（MCP `execute_blender_code`，有 GPU 上下文、渲图更稳）：

    exec(open(r"<repo>/visualization/blender/generate_urban_twin.py", encoding="utf-8").read(), ns)
    manifest = ns["build_all"](layout, ["normal", "flood", "closure", "construction"], preview=True)
    ns["write_manifest"](layout, manifest)

对象上写自定义属性（`twin_id`、`kind`、`state`、`evidence`），导出 glTF 时进 extras，
引擎侧据此认对象，不靠命名约定猜。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
LAYOUT_PATH = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
OUT_DIR = ROOT / "visualization" / "blender" / "scenes"

COLORS = {
    "road_normal": (0.13, 0.14, 0.16, 1.0),
    "road_closed": (0.72, 0.10, 0.10, 1.0),
    "road_construction": (0.78, 0.62, 0.08, 1.0),
    "water": (0.04, 0.42, 0.95, 0.92),
    "building": (0.62, 0.65, 0.70, 1.0),
    "ground": (0.15, 0.16, 0.19, 1.0),
    "drainage": (0.10, 0.65, 0.85, 1.0),
    "gas": (0.95, 0.55, 0.05, 1.0),
    "power": (0.85, 0.20, 0.75, 1.0),
    "telecom": (0.25, 0.80, 0.35, 1.0),
    "water_pipe": (0.15, 0.35, 0.95, 1.0),
    "pump_station": (0.95, 0.95, 0.95, 1.0),
    "intake": (0.60, 0.85, 0.95, 1.0),
    "cone": (0.95, 0.35, 0.10, 1.0),
}

SCENARIOS = {
    "normal": {"water": False, "closure": False},
    "flood": {"water": True, "closure": False},
    "closure": {"water": True, "closure": True},
    "construction": {"water": True, "closure": True},
}

# --- 取景 ----------------------------------------------------------------
#
# 布局横跨 2939m×1024m，21m 宽的路在 1600px 上只剩十几像素——按"整座城"取景，
# 出来的图就是一张空地上有两条细线。取景窗口改为对准事件片区：以最深积水为中心，
# 边长按水面尺寸放大后夹到 [600, 1400] 米，路面/锥桶/水面才读得出来。
FOCUS_MIN_SPAN_M = 600.0
FOCUS_MAX_SPAN_M = 1400.0
FOCUS_WATER_MULTIPLIER = 4.5


def _focus_district(layout: dict, explicit: str | None = None) -> str:
    """主事件片区：显式指定 > 最深积水所在片区 > 第一个片区。"""
    if explicit:
        known = {district["district_id"] for district in layout["districts"]}
        if explicit not in known:
            raise SystemExit(f"未知片区 {explicit}，可选 {sorted(known)}")
        return explicit
    patches = sorted(layout.get("water", []), key=lambda item: -float(item["depth_cm"]))
    if patches:
        return patches[0]["district"]
    return layout["districts"][0]["district_id"]


def _focus_window(layout: dict, district: str) -> tuple[float, float, float]:
    """返回 (中心 x, 中心 y, 窗口边长)。窗口是正方形，避免宽高比不同导致缩放不一致。"""
    patches = [item for item in layout.get("water", []) if item["district"] == district]
    if patches:
        xs = [point["x"] for patch in patches for point in patch["polygon"]]
        ys = [point["y"] for patch in patches for point in patch["polygon"]]
    else:
        district_obj = next(
            item for item in layout["districts"] if item["district_id"] == district
        )
        xs = [district_obj["center"]["x"]]
        ys = [district_obj["center"]["y"]]
    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    span = min(FOCUS_MAX_SPAN_M, max(FOCUS_MIN_SPAN_M, extent * FOCUS_WATER_MULTIPLIER))
    return center_x, center_y, span


def _argv() -> dict[str, object]:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    options: dict[str, object] = {
        "scenarios": "all",
        "scenario": None,
        "glb": True,
        "preview": False,
        "focus": None,
    }
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--scenarios" and index + 1 < len(argv):
            options["scenarios"] = argv[index + 1]
            index += 2
        elif token == "--scenario" and index + 1 < len(argv):
            options["scenario"] = argv[index + 1]
            index += 2
        elif token == "--focus" and index + 1 < len(argv):
            options["focus"] = argv[index + 1]
            index += 2
        elif token == "--preview":
            options["preview"] = True
            index += 1
        elif token == "--no-glb":
            options["glb"] = False
            index += 1
        else:
            index += 1
    return options


def _material(name: str, color: tuple[float, float, float, float], *, emission: float = 0.0):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    # 按节点类型找，不按名字找：中文界面下节点名是"原理化 BSDF"，写死英文名会拿到 None
    bsdf = next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        material.use_nodes = True
        bsdf = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    for key, value in (
        ("Base Color", color),
        ("Alpha", color[3]),
        ("Roughness", 0.65),
    ):
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


def _box(name: str, center: tuple[float, float, float], size: tuple[float, float, float],
         rotation_z: float, material, collection: str):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.scale = size
    obj.rotation_euler = (0.0, 0.0, rotation_z)
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


def _plane_polygon(name: str, points: list[dict], z: float, material, collection: str):
    """水面是任意四边形，直接建面；用缩放 cube 会带出多余的厚度和法线问题。"""
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    vertices = [(item["x"], item["y"], z) for item in points]
    mesh.from_pydata(vertices, [], [list(range(len(vertices)))])
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


def _cylinder(name: str, start: tuple[float, float, float], end: tuple[float, float, float],
              radius: float, material, collection: str):
    """两点之间的圆柱：管线沿道路中线铺，竖向位置取台账 depth_m 的负值。"""
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


def _label(name: str, text: str, location: tuple[float, float, float], size: float = 12.0):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = text
    curve.size = size
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    obj.data.materials.append(_material("Label", (1.0, 1.0, 1.0, 1.0), emission=1.2))
    # 文字是自发光标牌，不该投影：悬空的大字会在路面投出"第二个标签"，看图时会被
    # 误读成重复对象（实测过一次，A-03 在画面里出现了两遍）。
    obj.visible_shadow = False
    _link(obj, "Labels")
    return obj


def _reset() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        if collection.name != "Collection":
            bpy.data.collections.remove(collection)
    for material in list(bpy.data.materials):
        bpy.data.materials.remove(material)


def build_scene(
    layout: dict, flags: dict[str, bool], scenario: str, focus: str | None = None
) -> dict[str, int]:
    counts = {"road": 0, "pipeline": 0, "facility": 0, "building": 0, "water": 0, "cone": 0}
    # 标签与锥桶的尺寸随取景窗口缩放：窗口 1000m 时按 1.0 倍，600m 时按 0.6 倍。
    # 写死绝对尺寸的话，换个取景尺度就有的是天那么大、有的是看不见。
    focus_district = _focus_district(layout, focus)
    center_x, center_y, span = _focus_window(layout, focus_district)
    scale = span / 1000.0
    road_materials = {
        "normal": _material("Road_Normal", COLORS["road_normal"]),
        "closed": _material("Road_Closed", COLORS["road_closed"], emission=0.15),
        "under_construction": _material("Road_Construction", COLORS["road_construction"]),
    }
    building_material = _material("Building", COLORS["building"])
    ground_material = _material("Ground", COLORS["ground"])
    border_material = _material("DistrictBorder", (0.30, 0.55, 0.85, 1.0), emission=0.6)
    lane_material = _material("Road_Lane", (0.88, 0.90, 0.94, 1.0), emission=0.4)
    water_material = _material("FloodWater", COLORS["water"], emission=0.25)
    cone_material = _material("Cone", COLORS["cone"], emission=0.2)
    pipe_materials = {
        "drainage": _material("Pipe_Drainage", COLORS["drainage"]),
        "gas": _material("Pipe_Gas", COLORS["gas"]),
        "power": _material("Pipe_Power", COLORS["power"]),
        "telecom": _material("Pipe_Telecom", COLORS["telecom"]),
        "water": _material("Pipe_Water", COLORS["water_pipe"]),
    }

    # 地面用一张整底板，不用"每片区一块"：两块 1200m 的板同高共面重叠会 z-fighting，
    # 俯视图上表现为一道斜向接缝，看着像渲染 bug。片区边界改用细边线表达。
    bounds = layout["bounds"]
    span_x = bounds["max"][0] - bounds["min"][0]
    span_y = bounds["max"][1] - bounds["min"][1]
    _box(
        "Ground_Terrain",
        (
            (bounds["min"][0] + bounds["max"][0]) / 2,
            (bounds["min"][1] + bounds["max"][1]) / 2,
            -0.4,
        ),
        # 底板放到 3 倍边界：1.06 倍时板边会落进取景框，看起来像悬空的板子
        (span_x * 3.0, span_y * 3.0, 0.4),
        0.0,
        ground_material,
        "Ground",
    )

    for district in layout["districts"]:
        cx = district["center"]["x"]
        cy = district["center"]["y"]
        half = district["radius_m"]
        thickness = 3.0 * scale
        for suffix, (bx, by, sx, sy) in {
            "N": (cx, cy + half, half * 2, thickness),
            "S": (cx, cy - half, half * 2, thickness),
            "W": (cx - half, cy, thickness, half * 2),
            "E": (cx + half, cy, thickness, half * 2),
        }.items():
            _box(
                f"DistrictBorder_{district['district_id']}_{suffix}",
                (bx, by, -0.18),
                (sx, sy, 0.06),
                0.0,
                border_material,
                "Ground",
            )
        _label(
            f"DistrictLabel_{district['district_id']}",
            district["district_id"],
            (district["center"]["x"], district["center"]["y"], span * 0.05),
            size=40.0 * scale,
        )

    for road in layout["roads"]:
        start, end = road["polyline"][0], road["polyline"][1]
        state = road["state"]
        if state == "closed" and not flags["closure"]:
            state = "normal"  # 对照场景：只画物理状态，不叠事件状态
        material = road_materials.get(state, road_materials["normal"])
        dx, dy = end["x"] - start["x"], end["y"] - start["y"]
        length = math.hypot(dx, dy) or 1.0
        obj = _box(
            f"Road_{road['road_id']}",
            ((start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2, road["z"]),
            (length, road["width_m"] * 2.5, 0.35),
            math.atan2(dy, dx),
            material,
            "Roads",
        )
        _tag(obj, twin_id=f"TW-road-{road['road_id']}", kind="road", state=state, evidence=road["evidence"])
        counts["road"] += 1
        _label(f"RoadLabel_{road['road_id']}", road["road_id"], (start["x"], start["y"], road["z"] + 8.0 * scale), size=14.0 * scale)

        # 道路中线：深色路面在俯视图里没有任何纹理时会被读成一道阴影，不像路。
        angle = math.atan2(dy, dx)
        dash_count = max(2, int(length // 70))
        dash_length = length / dash_count * 0.45
        for index in range(dash_count):
            ratio = (index + 0.5) / dash_count
            _box(
                f"Lane_{road['road_id']}_{index:02d}",
                (start["x"] + dx * ratio, start["y"] + dy * ratio, road["z"] + 0.30),
                (dash_length, 0.8 * scale, 0.06),
                angle,
                lane_material,
                "Roads",
            )

        if state == "closed":
            steps = max(2, int(length // 40))
            cone_radius = max(0.35, 1.5 * scale)
            cone_height = max(1.2, 5.0 * scale)
            for index in range(steps):
                ratio = (index + 0.5) / steps
                cone = _cylinder(
                    f"Cone_{road['road_id']}_{index:02d}",
                    (start["x"] + dx * ratio, start["y"] + dy * ratio, road["z"] + 0.2),
                    (start["x"] + dx * ratio, start["y"] + dy * ratio, road["z"] + 0.2 + cone_height),
                    cone_radius,
                    cone_material,
                    "Roads",
                )
                _tag(cone, twin_id=f"TW-cone-{road['road_id']}-{index}", kind="cone", state="closed")
                counts["cone"] += 1

    for pipeline in layout["pipelines"]:
        start, end = pipeline["polyline"][0], pipeline["polyline"][1]
        obj = _cylinder(
            f"Pipe_{pipeline['pipeline_id']}",
            (start["x"], start["y"], pipeline["z"]),
            (end["x"], end["y"], pipeline["z"]),
            max(0.35, pipeline["diameter_mm"] / 2000),
            pipe_materials.get(pipeline["kind"], pipe_materials["drainage"]),
            "Pipelines",
        )
        _tag(
            obj,
            twin_id=f"TW-{pipeline['kind']}-{pipeline['pipeline_id']}",
            kind=pipeline["kind"],
            state="in_service",
            evidence=pipeline["evidence"],
        )
        counts["pipeline"] += 1

    for facility in layout["facilities"]:
        is_pump = facility["facility_type"] == "pump_station"
        obj = _cylinder(
            f"Facility_{facility['facility_id']}",
            (facility["xy"]["x"], facility["xy"]["y"], facility["z"] + 0.2),
            (facility["xy"]["x"], facility["xy"]["y"], facility["z"] + 4.0),
            facility["footprint_m"] / 2,
            _material("PumpStation" if is_pump else "Intake",
                      COLORS["pump_station"] if is_pump else COLORS["intake"]),
            "Facilities",
        )
        _tag(
            obj,
            twin_id=f"TW-{facility['facility_type']}-{facility['facility_id']}",
            kind=facility["facility_type"],
            state=facility["state"],
            evidence=facility["evidence"],
        )
        counts["facility"] += 1
        _label(
            f"FacilityLabel_{facility['facility_id']}",
            f"{facility['facility_id']} {facility['capacity_m3h']}m3/h",
            (facility["xy"]["x"], facility["xy"]["y"], facility["z"] + 20.0 * scale),
            size=10.0 * scale,
        )

    for building in layout["buildings"]:
        xs = [point["x"] for point in building["footprint"]]
        ys = [point["y"] for point in building["footprint"]]
        obj = _box(
            f"Building_{building['building_id']}",
            (sum(xs) / len(xs), sum(ys) / len(ys), building["height_m"] / 2),
            (max(xs) - min(xs), max(ys) - min(ys), building["height_m"]),
            0.0,
            building_material,
            "Buildings",
        )
        _tag(obj, twin_id=building["building_id"], kind="building", state=building["district"])
        counts["building"] += 1

    if flags["water"]:
        for patch in layout["water"]:
            obj = _plane_polygon(
                f"Water_{patch['incident_id']}", patch["polygon"], patch["z"] + 1.2, water_material, "Water"
            )
            _tag(
                obj,
                twin_id=f"TW-water-{patch['incident_id']}",
                kind="water",
                state=f"{patch['depth_cm']:g}cm",
                evidence=patch["evidence"],
            )
            xs = [point["x"] for point in patch["polygon"]]
            ys = [point["y"] for point in patch["polygon"]]
            _label(
                f"WaterLabel_{patch['incident_id']}",
                f"{patch['depth_cm']:g}cm",
                (sum(xs) / len(xs), sum(ys) / len(ys), patch["z"] + 12.0 * scale),
                size=16.0 * scale,
            )
            counts["water"] += 1

    _camera_and_light(layout, scenario, focus_district, center_x, center_y, span)
    return counts


def _camera_and_light(
    layout: dict,
    scenario: str,
    focus_district: str,
    center_x: float,
    center_y: float,
    span: float,
) -> None:
    """相机站在取景窗口外做东南 3/4 俯视，仰角约 38°。

    三条实测教训：
    1. 相机必须在窗口外——放进场景里会被前景建筑挡住路口；
    2. 50mm 在 1.5 倍窗口距离上横向覆盖约 1.1 倍窗口宽，窗口刚好占满画面；
    3. 默认裁切面 1000m，相机在两千米外会把整座城裁掉，只剩背景色。
    """
    location = (center_x + span * 0.50, center_y - span * 1.10, span * 0.95)
    bpy.ops.object.camera_add(location=location)
    camera = bpy.context.object
    camera.name = "DemoCamera"
    camera.data.lens = 50
    camera.data.clip_start = 1.0
    camera.data.clip_end = span * 12
    # 用 track quat 对准场景中心：手写欧拉角极易把镜头抬到城市上方看天空
    from mathutils import Vector

    direction = Vector((center_x, center_y, 0.0)) - Vector(location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera

    bpy.ops.object.light_add(type="SUN", location=(center_x, center_y - span * 0.4, span))
    sun = bpy.context.object
    sun.name = "DemoSun"
    sun.data.energy = 3.4
    sun.rotation_euler = (math.radians(52), 0.0, math.radians(35))

    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("UrbanTwinWorld")
        bpy.context.scene.world = world
    world.use_nodes = True
    background = next(
        (node for node in world.node_tree.nodes if node.type == "BACKGROUND"), None
    )
    if background is not None:
        background.inputs["Color"].default_value = (0.28, 0.36, 0.46, 1.0)
        background.inputs["Strength"].default_value = 1.5

    scene = bpy.context.scene
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    # 场景自定义属性跟着 .blend 一起存，引擎侧/复核者能直接看出这张图对准了谁
    scene["scenario"] = scenario
    scene["layout_snapshot"] = layout.get("snapshot_hash", "")
    scene["focus_district"] = focus_district
    scene["focus_span_m"] = span
    scene["camera_lens_mm"] = 50


def render_preview(scenario: str) -> str:
    """渲一张预览图，相对路径返回，供清单引用。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"preview_{scenario}.png"
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(target)
    bpy.ops.render.render(write_still=True)
    return str(target.relative_to(ROOT)).replace("\\", "/")


def export(scenario: str, counts: dict[str, int], with_glb: bool) -> dict[str, object]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    blend_path = OUT_DIR / f"urban_twin_{scenario}.blend"
    glb_path = OUT_DIR / f"urban_twin_{scenario}.glb"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    if with_glb:
        bpy.ops.export_scene.gltf(
            filepath=str(glb_path),
            export_format="GLB",
            use_selection=False,
            export_extras=True,  # 自定义属性进 extras，引擎侧按 twin_id 认对象
            export_yup=True,
        )
    return {
        "scenario": scenario,
        "blend": str(blend_path.relative_to(ROOT)).replace("\\", "/"),
        "glb": str(glb_path.relative_to(ROOT)).replace("\\", "/") if with_glb else None,
        "objects": counts,
    }


def build_all(
    layout: dict,
    names: list[str],
    *,
    focus: str | None = None,
    with_glb: bool = True,
    preview: bool = False,
) -> list[dict[str, object]]:
    """按场景逐个重建并导出。抽出来是为了 Live 编辑器里也能一次跑完（见模块文档）。"""
    manifest: list[dict[str, object]] = []
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit(f"未知场景：{name}，可选 {list(SCENARIOS)}")
        _reset()
        counts = build_scene(layout, SCENARIOS[name], name, focus=focus)
        entry = export(name, counts, with_glb)
        if preview:
            entry["preview"] = render_preview(name)
        manifest.append(entry)
        print(
            f"[{name}] 道路 {counts['road']} / 管线 {counts['pipeline']} / 设施 {counts['facility']} / "
            f"建筑 {counts['building']} / 水面 {counts['water']} / 锥桶 {counts['cone']} → {entry['blend']}"
        )
    return manifest


def write_manifest(layout: dict, manifest: list[dict[str, object]]) -> Path:
    manifest_path = OUT_DIR / "asset_manifest.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "urban-twin-assets-v1",
                "layout": str(LAYOUT_PATH.relative_to(ROOT)).replace("\\", "/"),
                "layout_snapshot": layout.get("snapshot_hash", ""),
                "blender": bpy.app.version_string,
                "scenarios": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    options = _argv()
    if not LAYOUT_PATH.exists():
        print(f"缺少布局文件：{LAYOUT_PATH}\n先运行 python -m decision_poc.scene_layout")
        return 2
    layout = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    if options["scenario"]:
        names = [str(options["scenario"])]
    elif options["scenarios"] == "all":
        names = list(SCENARIOS)
    else:
        names = str(options["scenarios"]).split(",")

    manifest = build_all(
        layout,
        names,
        focus=options["focus"],
        with_glb=bool(options["glb"]),
        preview=bool(options["preview"]),
    )
    manifest_path = write_manifest(layout, manifest)
    print(f"清单 → {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
