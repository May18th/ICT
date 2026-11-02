"""Unreal Editor Python：按 scene_layout.json 搭建实时数字孪生关卡。

分工：Blender 负责离线建模（.blend/.glb + 材质），Unreal 负责"实时展示 + 状态变化 +
交互回放"。这里的场景不再靠手摆，而是读同一份 `samples/assets/urban/scene_layout.json`，
所以 Blender 里看到的路网和 UE 里的是同一套坐标。

关卡内容：

| 分组 | Actor | 说明 |
| --- | --- | --- |
| 道路 | `SM_Road_<road_id>` | 平面拉伸，颜色随 state（normal/closed/under_construction） |
| 建筑 | `SM_Building_<id>` | 简化体块，高度取自布局 |
| 管线 | `SM_Pipe_<pipeline_id>` | 圆柱埋在 `-depth_m`，颜色按专业 |
| 排水设施 | `SM_Facility_<id>` | 圆柱，半径随能力 |
| 水面 | `SM_Water_<order_id>` | 半透明平面，带深度标注 |
| 标注 | `TXT_<id>` | 道路/设施编号，便于现场讲解 |
| 灯光 | `UrbanTwin_Sun` / `UrbanTwin_Sky` | 可移动光，避免静态光烘焙依赖 |

每个 Actor 都带 tags：`twin:<id>`、`kind:<kind>`、`state:<state>`。状态切换与回放脚本
（`urban_twin_state.py`）只认这些 tag，不认 Actor 名字。

用法（在编辑器 Python 控制台）：

    py "D:/.../build_urban_level.py"

或命令行（无界面构建，用于验证）：

    UnrealEditor-Cmd.exe UrbanTwinDemo.uproject -run=pythonscript \\
        -script=".../build_urban_level.py" -unattended -nosplash -nullrhi
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import unreal

ROOT = Path(__file__).resolve().parents[4]
LAYOUT_PATH = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
LEVEL_PATH = "/Game/UrbanTwin/Maps/UrbanTwinDemo"
MATERIAL_ROOT = "/Game/UrbanTwin/Materials"

COLORS = {
    "road_normal": (0.10, 0.11, 0.13, 1.0),
    "road_closed": (0.72, 0.09, 0.09, 1.0),
    "road_construction": (0.80, 0.62, 0.06, 1.0),
    "building": (0.42, 0.46, 0.52, 1.0),
    "ground": (0.30, 0.32, 0.36, 1.0),
    "water": (0.04, 0.42, 0.95, 0.55),
    "drainage": (0.06, 0.60, 0.85, 1.0),
    "gas": (0.95, 0.55, 0.05, 1.0),
    "power": (0.85, 0.20, 0.75, 1.0),
    "telecom": (0.25, 0.80, 0.35, 1.0),
    "water_pipe": (0.15, 0.35, 0.95, 1.0),
    "pump_station": (0.92, 0.93, 0.95, 1.0),
    "intake": (0.60, 0.85, 0.95, 1.0),
}

CM = 100.0  # 布局单位是米，UE 是厘米

IMPORTED = "/Game/UrbanTwin/Imported"

# 真实资产（Content/UrbanTwin/Imported 下已导入的模型），把"色块示意"换成能讲的模型。
# 缺失的（RES-SAND / RES-GAS-DETECT / RES-GAS-VALVE / PS-02/PS-03 专属模型）在报告里显式列为缺口，
# 不拿别的模型冒充。
REAL_STATION = IMPORTED + "/Focus2/S31_PumpStation_DF-PS-01"
REAL_INTAKE = {
    "DF-IN-101": IMPORTED + "/EventProps/S31_Intake_DF-IN-101",
    "DF-IN-103": IMPORTED + "/EventProps/S31_Intake_DF-IN-103",
    "DF-IN-201": IMPORTED + "/EventProps/S31_Intake_DF-IN-201",
}
REAL_PUMP_TRUCKS = [
    IMPORTED + "/Focus2/S37_TripoPumpTruck_0",
    IMPORTED + "/Focus2/S37_TripoPumpTruck_1",
    IMPORTED + "/Focus2/S37_TripoPumpTruck_2",
]
REAL_PROPS = {
    "gen": IMPORTED + "/Focus2/S51_Generator",
    "light_tower": IMPORTED + "/Focus2/S51_LightTower",
    "command_van": IMPORTED + "/Focus2/S43_CommandVan",
    "police": IMPORTED + "/Focus2/S51_PoliceCar",
    "ambulance": IMPORTED + "/EventProps/S31_Ambulance",
    "firetruck": IMPORTED + "/EventProps/S31_FireTruck",
    "barrier": IMPORTED + "/EventProps/S31_StreetClosure_ACT-CLOSE-ROAD",
    "portal": IMPORTED + "/EventProps/S31_PortalBarrier_BLK-PORTAL",
    "road_control": IMPORTED + "/EventProps/S31_RoadControl_CF-ROADCTRL",
    "pit_fence": IMPORTED + "/EventProps/S31_PitSurface_Fence",
    "flood_gauge": IMPORTED + "/EventProps/S31_Obs_FloodGauge",
    "worker_stand": IMPORTED + "/Focus2/S52_WorkerStand",
    "worker_bend": IMPORTED + "/Focus2/S52_WorkerBend",
    "rain": "",
}


def log(message: str) -> None:
    unreal.log(f"[UrbanTwin] {message}")


# --- 资产 ---------------------------------------------------------------


def _material(name: str, color: tuple[float, float, float, float], translucent: bool = False):
    path = f"{MATERIAL_ROOT}/{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)
    factory = unreal.MaterialFactoryNew()
    material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        name, MATERIAL_ROOT, unreal.Material, factory
    )
    parameter = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionVectorParameter, -420, 0
    )
    parameter.set_editor_property("parameter_name", "Color")
    parameter.set_editor_property(
        "default_value", unreal.LinearColor(color[0], color[1], color[2], color[3])
    )
    unreal.MaterialEditingLibrary.connect_material_property(
        parameter, "", unreal.MaterialProperty.MP_BASE_COLOR
    )
    if translucent:
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        material.set_editor_property("two_sided", True)
        unreal.MaterialEditingLibrary.connect_material_property(
            parameter, "A", unreal.MaterialProperty.MP_OPACITY
        )
    unreal.MaterialEditingLibrary.recompile_material(material)
    # 必须落盘：create_asset 只创建内存里的 dirty 包，save_current_level() 不保存材质。
    # 不存盘时关卡能引用它们（编辑器会话内可见），但 urban_twin_state 用 load_asset
    # 按路径取材质会直接失败——实测表现为 apply_job 报 5 个 "could not be found
    # in the Asset Registry" 并以退出码 1 结束。
    _save_asset(material)
    return material


def _save_asset(asset) -> None:
    """UE 5.1 之后 save_asset(path) 被 save_loaded_asset(asset) 取代，两者都兼容一下。"""
    library = unreal.EditorAssetLibrary
    save_loaded = getattr(library, "save_loaded_asset", None)
    if callable(save_loaded):
        save_loaded(asset)
        return
    save = getattr(library, "save_asset", None)
    if callable(save):
        save(asset.get_path_name().split(".")[0])


def _palette() -> dict[str, object]:
    return {
        "road_normal": _material("M_Road_Normal", COLORS["road_normal"]),
        "road_closed": _material("M_Road_Closed", COLORS["road_closed"]),
        "road_construction": _material("M_Road_Construction", COLORS["road_construction"]),
        "building": _material("M_Building", COLORS["building"]),
        "ground": _material("M_Ground", COLORS["ground"]),
        "water": _material("M_FloodWater", COLORS["water"], translucent=True),
        "drainage": _material("M_Pipe_Drainage", COLORS["drainage"]),
        "gas": _material("M_Pipe_Gas", COLORS["gas"]),
        "power": _material("M_Pipe_Power", COLORS["power"]),
        "telecom": _material("M_Pipe_Telecom", COLORS["telecom"]),
        "water_pipe": _material("M_Pipe_Water", COLORS["water_pipe"]),
        "pump_station": _material("M_PumpStation", COLORS["pump_station"]),
        "intake": _material("M_Intake", COLORS["intake"]),
        # 回放高亮用：单独一个亮色材质，状态脚本按 tag 换上去
        "highlight": _material("M_Highlight", (0.10, 0.95, 1.0, 1.0)),
    }


def _mesh(path: str):
    return unreal.EditorAssetLibrary.load_asset(path)


def _actors():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _spawn(label: str, mesh_path: str, location, rotation, scale, material, tags) -> object:
    actor = _actors().spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(*location), unreal.Rotator(*rotation)
    )
    actor.set_actor_label(label)
    actor.set_actor_scale3d(unreal.Vector(*scale))
    component = actor.static_mesh_component
    component.set_static_mesh(_mesh(mesh_path))
    if material is not None:
        component.set_material(0, material)
    actor.set_editor_property("tags", [unreal.Name(tag) for tag in tags])
    return actor


def _mesh_extent(mesh_path: str):
    """返回 (最大平面尺寸_米, 底面相对原点的偏移_米)；资产缺失返回 (None, None)。"""
    mesh = _mesh(mesh_path)
    if mesh is None:
        return None, None
    try:
        bounds = mesh.get_bounds()
        extent = bounds.box_extent
        origin = bounds.origin
        size_m = max(extent.x, extent.y) * 2.0 / CM
        bottom_m = (origin.z - extent.z) / CM
        return size_m, bottom_m
    except Exception as error:  # noqa: BLE001
        log(f"读包围盒失败 {mesh_path}: {error}")
        return None, None


def _spawn_real(label: str, mesh_path: str, xy, z_m: float, yaw: float, tags, target_size_m=None):
    """放真实导入资产：按包围盒归一化到 target_size_m，底面落到 z_m。"""
    if not unreal.EditorAssetLibrary.does_asset_exist(mesh_path):
        log(f"缺少真实资产 {mesh_path}（{label}），跳过")
        return None
    size_m, bottom_m = _mesh_extent(mesh_path)
    scale = 1.0
    if size_m and size_m > 0:
        if target_size_m:
            scale = target_size_m / size_m
        elif size_m < 1.0:  # 米制单位模型：补 100 倍
            scale = CM
    actor = _actors().spawn_actor_from_class(
        unreal.StaticMeshActor,
        unreal.Vector(xy[0] * CM, xy[1] * CM, z_m * CM),
        unreal.Rotator(0, yaw, 0),
    )
    actor.set_actor_label(label)
    actor.set_actor_scale3d(unreal.Vector(scale, scale, scale))
    component = actor.static_mesh_component
    component.set_static_mesh(_mesh(mesh_path))
    actor.set_editor_property("tags", [unreal.Name(tag) for tag in tags])
    if bottom_m is not None:
        actor.set_actor_location(
            unreal.Vector(xy[0] * CM, xy[1] * CM, z_m * CM - bottom_m * scale), False, False
        )
    return actor


def _label_actor(text: str, location, size: float = 300.0):
    try:
        actor = _actors().spawn_actor_from_class(
            unreal.TextRenderActor, unreal.Vector(*location), unreal.Rotator(0, 0, 0)
        )
        component = actor.get_component_by_class(unreal.TextRenderComponent)
        if component is None:
            return None
        component.set_text(unreal.Text(text))
        component.set_world_size(size)
        actor.set_actor_label(f"TXT_{text}")
        return actor
    except Exception as error:  # 标注不是关键路径，失败不该挡住整关卡构建
        log(f"标注失败 {text}: {error}")
        return None


def _lighting() -> None:
    sun = _actors().spawn_actor_from_class(
        unreal.DirectionalLight, unreal.Vector(0, 0, 90000), unreal.Rotator(-48, -32, 0)
    )
    sun.set_actor_label("UrbanTwin_Sun")
    sun_component = sun.get_component_by_class(unreal.DirectionalLightComponent)
    sun_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    sun_component.set_intensity(8.0)
    for prop, value in (("atmosphere_sun_light", True), ("use_temperature", True),
                        ("temperature", 5600.0)):
        try:
            sun_component.set_editor_property(prop, value)
        except Exception:  # noqa: BLE001
            pass

    sky = _actors().spawn_actor_from_class(
        unreal.SkyLight, unreal.Vector(0, 0, 20000), unreal.Rotator(0, 0, 0)
    )
    sky.set_actor_label("UrbanTwin_Sky")
    sky_component = sky.get_component_by_class(unreal.SkyLightComponent)
    sky_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_component.set_intensity(3.5)
    for prop, value in (("real_time_capture", True), ("source_type", unreal.SkyLightSourceType.SLS_CAPTURED_SCENE)):
        try:
            sky_component.set_editor_property(prop, value)
        except Exception:  # noqa: BLE001
            pass

    # 没有大气就没有天空光，画面会黑成一片（实测第一次重建后整屏深蓝）
    try:
        atmosphere = _actors().spawn_actor_from_class(
            unreal.SkyAtmosphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0)
        )
        atmosphere.set_actor_label("UrbanTwin_Atmosphere")
    except Exception as error:  # noqa: BLE001
        log(f"大气组件创建失败（可忽略）: {error}")

    # 固定曝光：自动曝光在这种深色场景里会把画面压暗，导致"看得见但看不清"
    try:
        volume = _actors().spawn_actor_from_class(
            unreal.PostProcessVolume, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0)
        )
        volume.set_actor_label("UrbanTwin_PostProcess")
        volume.set_editor_property("unbound", True)
        settings = volume.get_editor_property("settings")
        settings.set_editor_property("override_auto_exposure_method", True)
        settings.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
        settings.set_editor_property("override_auto_exposure_bias", True)
        settings.set_editor_property("auto_exposure_bias", 13.0)
        settings.set_editor_property("override_auto_exposure_min_brightness", True)
        settings.set_editor_property("auto_exposure_min_brightness", 1.0)
        settings.set_editor_property("override_auto_exposure_max_brightness", True)
        settings.set_editor_property("auto_exposure_max_brightness", 1.0)
        volume.set_editor_property("settings", settings)
    except Exception as error:  # noqa: BLE001
        log(f"后处理体积创建失败（可忽略）: {error}")


def _camera(layout: dict) -> None:
    focus = {patch["district"] for patch in layout["water"]} or {
        district["district_id"] for district in layout["districts"]
    }
    points = [
        point
        for road in layout["roads"]
        if road["district"] in focus
        for point in road["polyline"]
    ] or [
        {"x": layout["bounds"]["min"][0], "y": layout["bounds"]["min"][1]},
        {"x": layout["bounds"]["max"][0], "y": layout["bounds"]["max"][1]},
    ]
    xs = [point["x"] for point in points]
    ys = [point["y"] for point in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    location = unreal.Vector(
        (cx - span * 0.45) * CM, (cy - span * 1.05) * CM, span * 0.85 * CM
    )
    target = unreal.Vector(cx * CM, cy * CM, 0.0)
    rotation = unreal.MathLibrary.find_look_at_rotation(location, target)
    camera = _actors().spawn_actor_from_class(unreal.CameraActor, location, rotation)
    camera.set_actor_label("UrbanTwin_Camera")
    try:
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).set_level_viewport_camera_info(
            location, rotation, "Perspective"
        )
    except Exception as error:
        log(f"设置视口相机失败（不影响关卡内容）: {error}")


# --- 补几何：此前"画不出来"的 3 个 twin --------------------------------

MISSING_GEOMETRY = (
    ("TW-incident-WO-20261026-013",
     IMPORTED + "/EventProps/S31_Incident_WO-20261026-013", "water"),
    ("TW-incident-WO-20261029-009",
     IMPORTED + "/EventProps/S31_Incident_WO-20261029-009", "water"),
    ("TW-work_site-CP-2026-041",
     IMPORTED + "/EventProps/S31_PitSurface_Fence", "work_site"),
)


def _missing_twin_geometry(layout: dict) -> dict[str, int]:
    """给历史事件水面与施工面补几何（此前台账里可见、场景里没有）。

    位置口径：历史水面放在对应本事件积水旁（同一街区，偏移 40–70 m）；
    施工面放在施工路段（R-103）中点。tag 里带 `evidence:geometry-backfilled`
    标明"补的几何"，不冒充原始布局导出。
    """
    counts = {"historical_water": 0, "work_site": 0, "geometry_missing": 0}
    patches = sorted(layout["water"], key=lambda patch: -len(patch["polygon"]))
    if not patches:
        return counts

    def center(patch):
        xs = [point["x"] for point in patch["polygon"]]
        ys = [point["y"] for point in patch["polygon"]]
        return (sum(xs) / len(xs), sum(ys) / len(ys)), patch["z"]

    c1, z1 = center(patches[0])
    c2, z2 = center(patches[1]) if len(patches) > 1 else (c1, z1)
    spots = {
        "TW-incident-WO-20261026-013": ((c1[0] + 70, c1[1] + 40), z1),
        "TW-incident-WO-20261029-009": ((c2[0] - 60, c2[1] - 50), z2),
    }
    for road in layout["roads"]:
        if road["state"] != "under_construction":
            continue
        a, b = road["polyline"][0], road["polyline"][1]
        spots["TW-work_site-CP-2026-041"] = (((a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2), road["z"])
        break

    for twin, asset, kind in MISSING_GEOMETRY:
        if twin not in spots:
            counts["geometry_missing"] += 1
            continue
        xy, z = spots[twin]
        tags = [f"twin:{twin}", f"kind:{kind}", "asset:real", "evidence:geometry-backfilled"]
        actor = _spawn_real(
            "SM_Twin_" + twin.replace("TW-", ""), asset, xy, z, 0.0, tags,
            target_size_m=60.0 if kind == "water" else 40.0,
        )
        if actor is None:
            counts["geometry_missing"] += 1
        elif kind == "water":
            counts["historical_water"] += 1
        else:
            counts["work_site"] += 1
    log(f"补齐缺失 twin 几何 {counts}")
    return counts


# --- 真实道具与资源 ------------------------------------------------------


def _midpoint(road) -> tuple[float, float]:
    start, end = road["polyline"][0], road["polyline"][1]
    return ((start["x"] + end["x"]) / 2.0, (start["y"] + end["y"]) / 2.0)


def _scene_props(layout: dict) -> dict[str, int]:
    """把资源/道具摆到现场：泵车、发电机、灯塔、指挥车、救护消防、围挡、封路、水尺、工人、雨。

    tag 口径与设施一致：能对上决策层 twin 的挂 `twin:<id>`，对不上的只挂 `kind:prop`
    （例如施工面 CP-2026-041 是否补几何仍是待定口径，这里只放围挡，不冒充已补）。
    """
    counts = {"prop": 0, "worker": 0, "prop_missing": 0, "rain": 0}

    patches = sorted(layout["water"], key=lambda patch: -len(patch["polygon"]))
    if not patches:
        return counts

    def center(patch):
        xs = [point["x"] for point in patch["polygon"]]
        ys = [point["y"] for point in patch["polygon"]]
        return (sum(xs) / len(xs), sum(ys) / len(ys)), patch["z"]

    (cx, cy), cz = center(patches[0])
    (bx, by), bz = center(patches[1]) if len(patches) > 1 else ((cx + 60, cy), cz)

    def place(label, asset, xy, z, yaw, tags, target=12.0):
        actor = _spawn_real(label, asset, xy, z, yaw, tags, target_size_m=target)
        if actor is None:
            counts["prop_missing"] += 1
            return None
        counts["prop"] += 1
        return actor

    # 现场一（A-03）：5 台移动泵车 + 发电机 + 灯塔 + 指挥车 + 救护/消防 + 水尺 + 工人
    truck_tags = ["twin:TW-resource-RES-PUMP-M", "kind:resource", "asset:real", "zone:A-03"]
    for index in range(5):
        place(
            f"SM_Prop_PumpTruck_A{index}",
            REAL_PUMP_TRUCKS[index % len(REAL_PUMP_TRUCKS)],
            (cx - 55 + index * 26, cy + 95),
            cz, 90.0, truck_tags, target=9.0,
        )
    place("SM_Prop_Generator", REAL_PROPS["gen"], (cx - 95, cy + 70), cz, 0.0,
          ["twin:TW-resource-RES-GEN", "kind:resource", "asset:real"], target=4.0)
    place("SM_Prop_LightTower", REAL_PROPS["light_tower"], (cx - 120, cy + 40), cz, 0.0,
          ["twin:TW-resource-RES-GEN", "kind:resource", "asset:real"], target=6.0)
    place("SM_Prop_CommandVan", REAL_PROPS["command_van"], (cx - 130, cy + 95), cz, 0.0,
          ["kind:prop", "asset:real"], target=7.0)
    place("SM_Prop_Police", REAL_PROPS["police"], (cx - 155, cy + 60), cz, 0.0,
          ["kind:prop", "asset:real"], target=5.0)
    place("SM_Prop_Ambulance", REAL_PROPS["ambulance"], (cx - 155, cy + 110), cz, 0.0,
          ["kind:prop", "asset:real"], target=6.0)
    place("SM_Prop_FireTruck", REAL_PROPS["firetruck"], (cx - 185, cy + 85), cz, 0.0,
          ["kind:prop", "asset:real"], target=10.0)
    place("SM_Prop_Gauge", REAL_PROPS["flood_gauge"], (cx - 20, cy - 60), cz, 0.0,
          ["kind:prop", "asset:real"], target=2.5)
    place("SM_Prop_Barrier", REAL_PROPS["barrier"], (cx - 40, cy + 150), cz, 0.0,
          ["twin:TW-resource-RES-BARRIER", "kind:resource", "asset:real"], target=10.0)
    place("SM_Prop_Sign", REAL_PROPS["road_control"], (cx - 80, cy + 150), cz, 0.0,
          ["twin:TW-resource-RES-SIGN", "kind:resource", "asset:real"], target=2.5)
    for index, (dx, dy) in enumerate(((-30, 30), (-15, 55), (10, 40), (25, 70))):
        key = "worker_stand" if index % 2 == 0 else "worker_bend"
        place(f"SM_Prop_Worker_A{index}", REAL_PROPS[key], (cx + dx, cy + dy), cz, 0.0,
              ["kind:prop", "asset:real"], target=1.8)
        counts["worker"] += 1

    # 现场二（B-01）：3 台泵车 + 发电机 + 围挡 + 工人
    truck_b_tags = ["twin:TW-resource-RES-PUMP-M-B01", "kind:resource", "asset:real", "zone:B-01"]
    for index in range(3):
        place(
            f"SM_Prop_PumpTruck_B{index}",
            REAL_PUMP_TRUCKS[index % len(REAL_PUMP_TRUCKS)],
            (bx - 20 + index * 24, by + 70),
            bz, 90.0, truck_b_tags, target=9.0,
        )
    place("SM_Prop_Generator_B", REAL_PROPS["gen"], (bx - 55, by + 40), bz, 0.0,
          ["twin:TW-resource-RES-GEN", "kind:resource", "asset:real"], target=4.0)
    place("SM_Prop_Portal_B", REAL_PROPS["portal"], (bx, by + 90), bz, 0.0,
          ["kind:prop", "asset:real"], target=8.0)
    for index, (dx, dy) in enumerate(((-8, 30), (14, 45))):
        place(f"SM_Prop_Worker_B{index}", REAL_PROPS["worker_stand"], (bx + dx, by + dy), bz, 0.0,
              ["kind:prop", "asset:real"], target=1.8)
        counts["worker"] += 1

    # 施工路段：只放围挡做视觉落地，不挂 twin（CP-2026-041 是否补几何仍是待定口径）
    for road in layout["roads"]:
        if road["state"] != "under_construction":
            continue
        mx, my = _midpoint(road)
        place(f"SM_Prop_PitFence_{road['road_id']}", REAL_PROPS["pit_fence"], (mx, my), road["z"], 0.0,
              ["kind:work_site", "asset:real", f"road:{road['road_id']}"], target=12.0)

    # 雨：整片罩住场景（weather twin 可高亮）
    bounds = layout["bounds"]
    width = (bounds["max"][0] - bounds["min"][0]) * CM / 100.0
    depth = (bounds["max"][1] - bounds["min"][1]) * CM / 100.0
    rain = _spawn_real(
        "SM_Prop_Rain", REAL_PROPS["rain"],
        ((bounds["min"][0] + bounds["max"][0]) / 2.0, (bounds["min"][1] + bounds["max"][1]) / 2.0),
        40.0, 0.0,
        ["twin:TW-weather-WA-20261029-01", "kind:weather", "asset:real"],
        target_size_m=max(width, depth),
    )
    if rain is not None:
        counts["rain"] += 1
    else:
        counts["prop_missing"] += 1

    log(f"真实道具完成 {counts}")
    return counts


# --- 几何 ---------------------------------------------------------------


def build(layout: dict) -> dict[str, int]:
    palette = _palette()
    counts = {"road": 0, "pipeline": 0, "facility": 0, "building": 0, "water": 0, "label": 0,
              "facility_real": 0, "facility_fallback": 0, "prop": 0, "worker": 0,
              "prop_missing": 0, "rain": 0, "historical_water": 0, "work_site": 0,
              "geometry_missing": 0, "pipeline_upper": 0, "pipeline_lower": 0}

    for district in layout["districts"]:
        _spawn(
            f"SM_Ground_{district['district_id']}",
            "/Engine/BasicShapes/Plane.Plane",
            (district["center"]["x"] * CM, district["center"]["y"] * CM, -30.0),
            (0, 0, 0),
            (district["radius_m"] * 2 * CM / 100.0, district["radius_m"] * 2 * CM / 100.0, 1.0),
            palette["ground"],
            [f"kind:ground", f"district:{district['district_id']}"],
        )

    for road in layout["roads"]:
        start, end = road["polyline"][0], road["polyline"][1]
        dx, dy = (end["x"] - start["x"]) * CM, (end["y"] - start["y"]) * CM
        length = math.hypot(dx, dy)
        yaw = math.degrees(math.atan2(dy, dx))
        road_twin = road.get("twin_id") or "TW-road-" + road["road_id"]
        material = palette.get(f"road_{road['state']}", palette["road_normal"])
        actor = _spawn(
            f"SM_Road_{road['road_id']}",
            "/Engine/BasicShapes/Plane.Plane",
            ((start["x"] + end["x"]) / 2 * CM, (start["y"] + end["y"]) / 2 * CM, road["z"] * CM),
            (0, 0, yaw),
            (length / 100.0, road["width_m"] * 2.5 * CM / 100.0, 1.0),
            material,
            [f"twin:{road_twin}", "kind:road", f"state:{road['state']}"],
        )
        actor.set_editor_property(
            "tags",
            [
                unreal.Name(f"twin:{road_twin}"),
                unreal.Name("kind:road"),
                unreal.Name(f"state:{road['state']}"),
                unreal.Name(f"evidence:{road['evidence']}"),
            ],
        )
        counts["road"] += 1
        if _label_actor(f"{road['road_id']} {road['name']}", (start["x"] * CM, start["y"] * CM, 1200.0)):
            counts["label"] += 1

    for pipeline in layout["pipelines"]:
        start, end = pipeline["polyline"][0], pipeline["polyline"][1]
        z_start = pipeline.get("z_start", pipeline["z"])
        z_end = pipeline.get("z_end", pipeline["z"])
        layer = pipeline.get("layer") or ("lower" if pipeline["kind"] == "drainage" else "upper")
        # 布局坐标 → UE：x*100、y 取反、z*100；管底标高 → 管轴中心（+半径）
        dx = (end["x"] - start["x"]) * CM
        dy = -(end["y"] - start["y"]) * CM
        dz = (z_end - z_start) * CM
        horizontal = math.hypot(dx, dy)
        length = math.hypot(horizontal, dz)
        yaw = math.degrees(math.atan2(dy, dx))
        pitch = -math.degrees(math.atan2(dz, horizontal)) if horizontal > 1e-6 else 0.0
        pipeline_twin = pipeline.get("twin_id") or "TW-" + pipeline["kind"] + "-" + pipeline["pipeline_id"]
        radius = max(0.35, pipeline["diameter_mm"] / 2000)
        mid_z = (z_start + z_end) / 2 * CM + radius * CM
        _spawn(
            f"SM_Pipe_{pipeline['pipeline_id']}",
            "/Engine/BasicShapes/Cylinder.Cylinder",
            ((start["x"] + end["x"]) / 2 * CM, -(start["y"] + end["y"]) / 2 * CM, mid_z),
            (pitch, 90, yaw),
            (radius * 2, radius * 2, length / 100.0),
            palette.get(pipeline["kind"], palette["drainage"]),
            [
                f"twin:{pipeline_twin}",
                f"kind:{pipeline['kind']}",
                f"layer:{layer}",
                f"evidence:{pipeline['evidence']}",
            ],
        )
        counts["pipeline"] += 1
        counts[f"pipeline_{layer}"] = counts.get(f"pipeline_{layer}", 0) + 1

    for facility in layout["facilities"]:
        is_pump = facility["facility_type"] == "pump_station"
        facility_twin = facility.get("twin_id") or "TW-" + facility["facility_type"] + "-" + facility["facility_id"]
        tags = [
            f"twin:{facility_twin}",
            f"kind:{facility['facility_type']}",
            f"state:{facility['state']}",
            f"evidence:{facility['evidence']}",
            "asset:real",
        ]
        asset = REAL_STATION if is_pump else REAL_INTAKE.get(facility["facility_id"])
        xy = (facility["xy"]["x"], facility["xy"]["y"])
        placed = None
        if asset:
            placed = _spawn_real(
                f"SM_Facility_{facility['facility_id']}", asset, xy, facility["z"],
                0.0, tags, target_size_m=facility["footprint_m"],
            )
        if placed is None:
            # 资产缺失才退回基础形状，并在 counts 里留痕（不允许静默降级）
            _spawn(
                f"SM_Facility_{facility['facility_id']}",
                "/Engine/BasicShapes/Cylinder.Cylinder",
                (xy[0] * CM, xy[1] * CM, (facility["z"] + 2.0) * CM),
                (0, 0, 0),
                (facility["footprint_m"] * CM / 100.0, facility["footprint_m"] * CM / 100.0, 4.0),
                palette["pump_station" if is_pump else "intake"],
                tags,
            )
            counts["facility_fallback"] = counts.get("facility_fallback", 0) + 1
        else:
            counts["facility_real"] = counts.get("facility_real", 0) + 1
        counts["facility"] += 1
        if _label_actor(
            f"{facility['facility_id']} {facility['capacity_m3h']}m3/h",
            (facility["xy"]["x"] * CM, facility["xy"]["y"] * CM, (facility["z"] + 8) * CM),
            size=200.0,
        ):
            counts["label"] += 1

    for building in layout["buildings"]:
        xs = [point["x"] for point in building["footprint"]]
        ys = [point["y"] for point in building["footprint"]]
        width, depth = (max(xs) - min(xs)) * CM, (max(ys) - min(ys)) * CM
        _spawn(
            f"SM_Building_{building['building_id']}",
            "/Engine/BasicShapes/Cube.Cube",
            (sum(xs) / len(xs) * CM, sum(ys) / len(ys) * CM, building["height_m"] * CM / 2),
            (0, 0, 0),
            (width / 100.0, depth / 100.0, building["height_m"] * CM / 100.0),
            palette["building"],
            ["kind:building", f"district:{building['district']}"],
        )
        counts["building"] += 1

    for patch in layout["water"]:
        patch_twin = patch.get("twin_id") or "TW-incident-" + patch["incident_id"]
        xs = [point["x"] for point in patch["polygon"]]
        ys = [point["y"] for point in patch["polygon"]]
        _spawn(
            f"SM_Water_{patch['incident_id']}",
            "/Engine/BasicShapes/Plane.Plane",
            (sum(xs) / len(xs) * CM, sum(ys) / len(ys) * CM, (patch["z"] + 1.2) * CM),
            (0, 0, 0),
            ((max(xs) - min(xs)) * CM / 100.0, (max(ys) - min(ys)) * CM / 100.0, 1.0),
            palette["water"],
            [
                f"twin:{patch_twin}",
                "kind:water",
                f"state:{patch['depth_cm']:g}cm",
                f"evidence:{patch['evidence']}",
            ],
        )
        counts["water"] += 1

    counts.update(_missing_twin_geometry(layout))
    counts.update(_scene_props(layout))
    _lighting()
    _camera(layout)
    log(f"构建完成 {counts}")
    return counts


def main() -> int:
    if not LAYOUT_PATH.exists():
        log(f"缺少布局文件 {LAYOUT_PATH}，先跑 python -m decision_poc.scene_layout")
        return 2
    layout = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))

    unreal.EditorAssetLibrary.make_directory("/Game/UrbanTwin/Maps")
    unreal.EditorAssetLibrary.make_directory(MATERIAL_ROOT)
    level_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(LEVEL_PATH):
        level_subsystem.load_level(LEVEL_PATH)
        for actor in _actors().get_all_level_actors():
            _actors().destroy_actor(actor)
    else:
        level_subsystem.new_level(LEVEL_PATH)

    build(layout)
    level_subsystem.save_current_level()
    # 兜底再存一次整目录：上面的逐资产保存在某些引擎版本上会静默失败
    save_directory = getattr(unreal.EditorAssetLibrary, "save_directory", None)
    if callable(save_directory):
        save_directory(MATERIAL_ROOT, only_if_is_dirty=False)
    log(f"关卡已保存 {LEVEL_PATH}")
    return 0


if __name__ == "__main__":
    main()
