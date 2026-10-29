"""Unreal Editor Python: build a simple urban-twin presentation level.

Run inside the UE 5.8 Python console or with ``py build_urban_level.py`` after
opening UrbanTwinDemo.uproject. The script deliberately uses basic engine
primitives, so no marketplace assets are required.
"""
import json
from pathlib import Path

import unreal


ROOT = Path(__file__).resolve().parents[4]
SCENE_PATH = ROOT / "samples" / "assets" / "urban" / "scene.json"


def _material(name, color):
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    factory = unreal.MaterialFactoryNew()
    material = asset_tools.create_asset(name, "/Game/UrbanTwin/Materials", unreal.Material, factory)
    if material:
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_O_PAQUE)
        material.set_editor_property("diffuse_color", unreal.LinearColor(*color, 1.0))
    return material


def _spawn_box(label, location, scale, material=None):
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(*location), unreal.Rotator(0, 0, 0)
    )
    actor.set_actor_label(label)
    actor.set_actor_scale3d(unreal.Vector(*scale))
    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube.Cube")
    actor.static_mesh_component.set_static_mesh(mesh)
    if material:
        actor.static_mesh_component.set_material(0, material)
    return actor


def build():
    payload = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    unreal.EditorLevelLibrary.select_nothing()
    road = _material("M_Road", (0.12, 0.16, 0.22))
    water = _material("M_FloodWater", (0.03, 0.35, 0.9))
    risk = _material("M_RiskNode", (1.0, 0.5, 0.03))
    _spawn_box("Road_R101", (0, 0, 0), (8, .5, .08), road)
    _spawn_box("Road_R103", (0, 300, 0), (8, .5, .08), road)
    _spawn_box("Flooded_A03", (0, 150, 12), (2.2, 1.5, .04), water)
    _spawn_box("Gas_Worksite", (250, 0, 25), (1, .7, .2), risk)
    for item in payload.get("objects", []):
        if item.get("kind") in {"drainage", "pump_station", "gas", "power", "telecom"}:
            actor = _spawn_box(item["twin_id"], (0, 0, 35), (.18, .18, .25), risk)
            actor.set_metadata("twin_id", item["twin_id"])
            actor.set_metadata("geometry_ref", item.get("position", ""))
    unreal.EditorLevelLibrary.save_current_level()
    unreal.log("UrbanTwin level generated from " + str(SCENE_PATH))


if __name__ == "__main__":
    build()
