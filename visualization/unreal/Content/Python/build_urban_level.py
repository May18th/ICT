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
    # UE 5.8 does not expose a direct diffuse-color property on UMaterial.
    # Keep the hook so a MaterialInstance-based palette can be added later.
    return None


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


def _spawn_lighting():
    sun = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.DirectionalLight, unreal.Vector(0, 0, 1200), unreal.Rotator(-45, -35, 0)
    )
    sun.set_actor_label("UrbanTwin_Sun")
    sun_component = sun.get_component_by_class(unreal.DirectionalLightComponent)
    sun_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    sun_component.set_editor_property("intensity", 5.0)
    sky = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.SkyLight, unreal.Vector(0, 0, 800), unreal.Rotator(0, 0, 0)
    )
    sky.set_actor_label("UrbanTwin_Sky")
    sky_component = sky.get_component_by_class(unreal.SkyLightComponent)
    sky_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    sky_component.set_editor_property("intensity", 1.5)


def build():
    payload = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    unreal.EditorAssetLibrary.make_directory("/Game/UrbanTwin/Maps")
    level_path = "/Game/UrbanTwin/Maps/UrbanTwinLevel"
    if unreal.EditorAssetLibrary.does_asset_exist(level_path):
        unreal.EditorLoadingAndSavingUtils.load_map(level_path)
    else:
        unreal.EditorLevelLibrary.new_level(level_path)
    unreal.EditorLevelLibrary.select_nothing()
    _spawn_lighting()
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
            actor.set_editor_property(
                "tags",
                [
                    unreal.Name(item["twin_id"]),
                    unreal.Name("geometry_ref=" + item.get("position", "")),
                ],
            )
    unreal.EditorLevelLibrary.save_current_level()
    unreal.log("UrbanTwin level generated from " + str(SCENE_PATH))


if __name__ == "__main__":
    build()
