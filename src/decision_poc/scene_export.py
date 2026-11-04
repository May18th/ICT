"""Engine-neutral scene export for Blender and Unreal import."""
from __future__ import annotations

from typing import Any

from .semantic import TwinSemanticStore, build_urban_semantic_store


def build_scene_payload(store: TwinSemanticStore | None = None) -> dict[str, Any]:
    """`store` 可传入复用：可视化层要保证 scene_hash 与推演用的是同一份快照，
    不能这边重新建一个 store 再算哈希。
    """
    store = store or build_urban_semantic_store()
    objects = []
    for item in store.objects():
        objects.append({
            "twin_id": item.twin_id,
            "kind": item.kind.value,
            "name": item.name,
            "position": item.geometry_ref or "",
            "state": item.lifecycle_state.value,
            "region_id": item.region_id,
            "asset_ids": item.asset_ids,
            "attributes": item.attributes,
        })
    # Buildings are geometry-only scene objects. They are kept in the same
    # source snapshot so the 3D layer can validate district ownership.
    from pathlib import Path
    import json
    layout_path = Path(__file__).resolve().parents[2] / "samples" / "assets" / "urban" / "scene_layout.json"
    if layout_path.is_file():
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        for building in layout.get("buildings", []):
            objects.append({
                "twin_id": building["building_id"],
                "kind": "building",
                "name": building["building_id"],
                "position": f"building:{building['building_id']}",
                "state": "in_service",
                "region_id": building.get("district"),
                "asset_ids": [],
                "attributes": {"district": building.get("district"), "height_m": building.get("height_m")},
            })
    return {
        "schema_version": "urban-twin-scene-v1",
        "snapshot_hash": store.snapshot_hash(),
        "objects": objects,
        "notes": "position is a stable geometry_ref; Blender/Unreal may resolve it to scene coordinates",
    }


def write_scene(path: str) -> None:
    import json
    from pathlib import Path
    Path(path).write_text(json.dumps(build_scene_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples/assets/urban/scene.json")
    args = parser.parse_args()
    write_scene(args.out)

__all__ = ["build_scene_payload", "write_scene"]
