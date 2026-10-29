"""Build a small, engine-neutral urban twin scene from scene.json."""
import json
import math
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[2]
SCENE_JSON = ROOT / "samples" / "assets" / "urban" / "scene.json"
OUTPUT = ROOT / "visualization" / "blender" / "urban_twin.blend"


def material(name, color, alpha=1.0):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, alpha)
    if alpha < 1:
        mat.surface_render_method = 'DITHERED'
    return mat


def cube(name, location, scale, mat):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(mat)
    return obj


def main():
    payload = json.loads(SCENE_JSON.read_text(encoding="utf-8"))
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    road_mat = material("Road", (0.12, 0.16, 0.22))
    pipe_mat = material("Pipeline", (0.95, 0.32, 0.08))
    water_mat = material("FloodWater", (0.05, 0.45, 0.9), 0.55)
    node_mat = material("RiskNode", (1.0, 0.65, 0.08))
    cube("Road_R101", (0, 0, 0), (8, 0.5, 0.08), road_mat)
    cube("Road_R103", (0, 3, 0), (8, 0.5, 0.08), road_mat)
    cube("Flooded_A03", (0, 1.5, 0.04), (2.2, 1.5, 0.04), water_mat)
    cube("Gas_Worksite", (2.5, 0, 0.2), (1.0, 0.7, 0.2), pipe_mat)
    for index, item in enumerate(payload["objects"]):
        if item["kind"] in {"drainage", "pump_station", "gas", "power", "telecom"}:
            angle = index * 0.7
            obj = cube(item["twin_id"], (math.cos(angle) * 5, math.sin(angle) * 3, 0.25), (0.18, 0.18, 0.25), node_mat)
            obj["twin_id"] = item["twin_id"]
            obj["geometry_ref"] = item["position"]
    bpy.ops.object.camera_add(location=(0, -18, 16), rotation=(math.radians(32), 0, 0))
    bpy.context.scene.camera = bpy.context.object
    bpy.context.scene.world.color = (0.015, 0.025, 0.05)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT))
    print(f"saved {OUTPUT}")


if __name__ == "__main__":
    main()
