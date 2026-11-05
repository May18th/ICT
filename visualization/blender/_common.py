"""Blender 生成脚本共享的材质、对象、取景和预览辅助函数。"""

from __future__ import annotations

import math
from pathlib import Path

import bpy


COLORS = {
    "road_normal": (0.13, 0.14, 0.16, 1.0),
    "road_closed": (0.72, 0.10, 0.10, 1.0),
    "road_construction": (0.78, 0.62, 0.08, 1.0),
    "water": (0.04, 0.42, 0.95, 0.92),
    "building": (0.62, 0.65, 0.70, 1.0),
    "ground": (0.15, 0.16, 0.19, 1.0),
    "soil": (0.34, 0.20, 0.10, 1.0),
    "trench": (0.06, 0.05, 0.04, 1.0),
    "drainage": (0.10, 0.65, 0.85, 1.0),
    "gas": (0.95, 0.55, 0.05, 1.0),
    "power": (0.85, 0.20, 0.75, 1.0),
    "telecom": (0.25, 0.80, 0.35, 1.0),
    "water_pipe": (0.15, 0.35, 0.95, 1.0),
    "pump_station": (0.95, 0.95, 0.95, 1.0),
    "intake": (0.60, 0.85, 0.95, 1.0),
    "cone": (0.95, 0.35, 0.10, 1.0),
}


def _material(name: str, color: tuple[float, float, float, float], *, emission: float = 0.0):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    # 按节点类型找，不按名字找：中文界面下节点名是“原理化 BSDF”。
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


def _box(name: str, center, size, rotation_z: float, material, collection: str):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.scale = size
    obj.rotation_euler = (0.0, 0.0, rotation_z)
    obj.data.materials.append(material)
    _link(obj, collection)
    return obj


def _cylinder(name: str, start, end, radius: float, material, collection: str):
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


def _label(name: str, text: str, location, size: float = 12.0):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = text
    curve.size = size
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    obj.data.materials.append(_material("Label", (1.0, 1.0, 1.0, 1.0), emission=1.2))
    # 文字是自发光标牌，不该投影：悬空的大字会在路面投出“第二个标签”。
    obj.visible_shadow = False
    _link(obj, "Labels")
    return obj


def _reset() -> None:
    """清空当前场景，保证无头运行和 Live 编辑器重复运行结果一致。"""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        if collection.name != "Collection":
            bpy.data.collections.remove(collection)
    for material in list(bpy.data.materials):
        bpy.data.materials.remove(material)


def _camera_and_light(
    *,
    center_x: float,
    center_y: float,
    span: float,
    camera_name: str,
    sun_name: str,
    lens: float,
    camera_offset: tuple[float, float, float],
    clip_end_multiplier: float,
    world_name: str,
    scene_props: dict[str, object],
) -> None:
    """创建统一的东南三分之四俯视相机、太阳光、世界背景和场景元数据。"""
    location = (
        center_x + span * camera_offset[0],
        center_y + span * camera_offset[1],
        span * camera_offset[2],
    )
    bpy.ops.object.camera_add(location=location)
    camera = bpy.context.object
    camera.name = camera_name
    camera.data.lens = lens
    camera.data.clip_start = 1.0
    camera.data.clip_end = span * clip_end_multiplier
    from mathutils import Vector

    direction = Vector((center_x, center_y, 0.0)) - Vector(location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera

    bpy.ops.object.light_add(type="SUN", location=(center_x, center_y - span * 0.4, span))
    sun = bpy.context.object
    sun.name = sun_name
    sun.data.energy = 3.4
    sun.rotation_euler = (math.radians(52), 0.0, math.radians(35))

    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new(world_name)
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
    for key, value in scene_props.items():
        scene[key] = value


def render_preview(target: Path, *, root: Path | None = None) -> str | Path:
    """渲染一张 PNG 预览；指定 root 时返回相对 root 的 POSIX 路径。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(target)
    bpy.ops.render.render(write_still=True)
    if root is not None:
        return str(target.relative_to(root)).replace("\\", "/")
    return target
