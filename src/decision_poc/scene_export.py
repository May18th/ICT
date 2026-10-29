"""Engine-neutral scene export for Blender and Unreal import."""
from __future__ import annotations

from typing import Any
from .semantic import build_urban_semantic_store


def build_scene_payload() -> dict[str, Any]:
    store = build_urban_semantic_store()
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
