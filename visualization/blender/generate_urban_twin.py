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
- `construction`：在 closure 基础上表达占道施工段、围挡、开挖、土方、警示牌和暴露管线。

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
LAYOUT_PATH = ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
OUT_DIR = ROOT / "visualization" / "blender" / "scenes"

SCENARIOS = {
    "normal": {"water": False, "closure": False},
    "flood": {"water": True, "closure": False},
    "closure": {"water": True, "closure": True},
    "construction": {"water": True, "closure": True, "construction": True},
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


def build_scene(
    layout: dict, flags: dict[str, bool], scenario: str, focus: str | None = None
) -> dict[str, int]:
    counts = {
        "road": 0,
        "pipeline": 0,
        "facility": 0,
        "building": 0,
        "water": 0,
        "cone": 0,
        "construction_fence": 0,
        "excavation": 0,
        "trench_opening": 0,
        "excavated_soil": 0,
        "gas_exposed": 0,
        "hazard_sign": 0,
    }
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
    soil_material = _material("ExcavatedSoil", COLORS["soil"])
    trench_material = _material("TrenchVoid", COLORS["trench"])
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

    if flags.get("construction"):
        # states.under_construction 兼容两种形态：字符串数组 或 [{road_id: ...}] 字典数组
        construction_roads = {
            item["road_id"] if isinstance(item, dict) else item
            for item in layout.get("states", {}).get("under_construction", [])
        }
        for road in layout["roads"]:
            if road["road_id"] not in construction_roads:
                continue
            start, end = road["polyline"][0], road["polyline"][1]
            dx, dy = end["x"] - start["x"], end["y"] - start["y"]
            length = math.hypot(dx, dy) or 1.0
            angle = math.atan2(dy, dx)
            for side in (-1, 1):
                fence = _box(
                    f"ConstructionFence_{road['road_id']}_{side}",
                    ((start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2 + side * road["width_m"] * 0.9, road["z"] + 2.0),
                    (length * 0.55, 0.35, 4.0), angle, cone_material, "Construction",
                )
                _tag(fence, twin_id=f"TW-work_site-fence-{road['road_id']}-{side}", kind="construction_fence", state="active")
                counts["construction_fence"] += 1
            pit = _box(
                f"Excavation_{road['road_id']}",
                ((start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2, road["z"] - 1.2),
                (length * 0.35, road["width_m"] * 1.8, 2.4), angle, ground_material, "Construction",
            )
            _tag(pit, twin_id=f"TW-excavation-{road['road_id']}", kind="excavation", state="open")
            counts["excavation"] += 1

            # 让开挖在鸟瞰和近景都可读。道路面上先放一块深色坑底，再用两侧土壁表达剖面，
            # 避免地下 box 被整块地面遮住而只剩围挡。
            trench = _box(
                f"TrenchOpening_{road['road_id']}",
                ((start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2, road["z"] + 0.24),
                (length * 0.35, road["width_m"] * 1.55, 0.08), angle, trench_material, "Construction",
            )
            _tag(trench, twin_id=f"TW-trench-{road['road_id']}", kind="trench_opening", state="open")
            counts["trench_opening"] += 1
            for side in (-1, 1):
                soil = _box(
                    f"TrenchSoil_{road['road_id']}_{side}",
                    ((start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2 + side * road["width_m"] * 0.95, road["z"] + 0.65),
                    (length * 0.35, 0.45, 0.9), angle, soil_material, "Construction",
                )
                _tag(soil, twin_id=f"TW-soil-{road['road_id']}-{side}", kind="excavated_soil", state="stockpiled")
                counts["excavated_soil"] += 1

            for index, ratio in enumerate((0.28, 0.72)):
                sign = _box(
                    f"HazardSign_{road['road_id']}_{index}",
                    (start["x"] + dx * ratio, start["y"] + dy * ratio, road["z"] + 3.0),
                    (2.0, 0.18, 1.2), angle, cone_material, "Construction",
                )
                _tag(sign, twin_id=f"TW-hazard-sign-{road['road_id']}-{index}", kind="hazard_sign", state="active")
                counts["hazard_sign"] += 1

            for pipeline in layout["pipelines"]:
                if pipeline["road_id"] != road["road_id"] or pipeline["kind"] != "gas":
                    continue
                start, end = pipeline["polyline"][0], pipeline["polyline"][1]
                exposed = _cylinder(
                    f"ExposedGas_{pipeline['pipeline_id']}",
                    (start["x"], start["y"], road["z"] + 1.45),
                    (end["x"], end["y"], road["z"] + 1.45),
                    max(0.5, pipeline["diameter_mm"] / 1800),
                    pipe_materials["gas"],
                    "Construction",
                )
                _tag(
                    exposed,
                    twin_id=f"TW-exposed-{pipeline['pipeline_id']}",
                    kind="gas_exposed",
                    state="exposed",
                    evidence=pipeline["evidence"],
                )
                counts["gas_exposed"] += 1

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
        # `water` 同时是管线 kind 和积水面 kind；manifest 记录实际对象总数，
        # `pipeline` 仍保留全部管线聚合数，便于按资产类别核对。
        if pipeline["kind"] == "water":
            counts["water"] += 1

    for facility in layout["facilities"]:
        facility_type = facility["facility_type"].strip()
        is_pump = facility_type == "pump_station"
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
            twin_id=f"TW-{facility_type}-{facility['facility_id']}",
            kind=facility_type,
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

    _camera_and_light(
        center_x=center_x,
        center_y=center_y,
        span=span,
        camera_name="DemoCamera",
        sun_name="DemoSun",
        lens=50,
        camera_offset=(0.50, -1.10, 0.95),
        clip_end_multiplier=12,
        world_name="UrbanTwinWorld",
        scene_props={
            "scenario": scenario,
            "layout_snapshot": layout.get("snapshot_hash", ""),
            "focus_district": focus_district,
            "focus_span_m": span,
            "camera_lens_mm": 50,
        },
    )
    return counts


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
            entry["preview"] = _render_preview(OUT_DIR / f"preview_{name}.png", root=ROOT)
        manifest.append(entry)
        print(
            f"[{name}] 道路 {counts['road']} / 管线 {counts['pipeline']} / 设施 {counts['facility']} / "
            f"建筑 {counts['building']} / 水对象 {counts['water']} / 锥桶 {counts['cone']} → {entry['blend']}"
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
