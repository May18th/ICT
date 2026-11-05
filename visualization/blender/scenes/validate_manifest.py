# -*- coding: utf-8 -*-
r"""资产清单回归校验：把 `asset_manifest.json` 与四个场景 `.blend` 逐项反查。

背景：`generate_urban_twin.py` 生成的 manifest 曾经与真实产物不一致（只登记 construction、
`water` 计数口径错、漏施工对象类别）。本脚本作为**资产清单回归测试**，跑完四场景重建后必跑：

    & 'D:\Program Files\Blender Foundation\Blender5.4\blender.exe' -b \
        --python visualization/blender/scenes/validate_manifest.py

校验规则（与生成器的计数口径一致）：
- 聚合字段：`pipeline` = `Pipelines` 集合对象数、`facility` = `Facilities` 集合对象数；
- 语义字段：其余按对象自定义属性 `kind` 计数（`road/building/water/cone/...`）；
- 文件存在性：每个场景的 `blend`/`glb`/`preview` 路径必须真实存在。

退出码：0 = 全部一致；1 = 有任一不一致或缺失。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[3]  # agent_poc/
MANIFEST = ROOT / "visualization" / "blender" / "scenes" / "asset_manifest.json"

# 语义字段 → 对象 `kind`（聚合字段 pipeline/facility 单独按集合数，见下）
SEMANTIC_KEYS = (
    "road", "building", "water", "cone", "construction_fence", "excavation",
    "trench_opening", "excavated_soil", "gas_exposed", "hazard_sign",
)


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures: list[str] = []

    for entry in manifest["scenarios"]:
        scenario = entry["scenario"]

        # 文件存在性
        for field in ("blend", "glb", "preview"):
            p = entry.get(field)
            if p and not (ROOT / p).is_file():
                failures.append(f"{scenario}: {field} 缺失 {p}")

        bpy.ops.wm.open_mainfile(filepath=str(ROOT / entry["blend"]))
        kinds = Counter(str(obj.get("kind")) for obj in bpy.data.objects if obj.get("kind"))
        pipeline = len(bpy.data.collections["Pipelines"].objects) if "Pipelines" in bpy.data.collections else 0
        facility = len(bpy.data.collections["Facilities"].objects) if "Facilities" in bpy.data.collections else 0

        actual = {"pipeline": pipeline, "facility": facility}
        actual.update({k: kinds[k] for k in SEMANTIC_KEYS})
        expected = dict(entry["objects"])

        if actual != expected:
            failures.append(f"{scenario}: expected={expected} actual={actual}")
        else:
            print(f"[manifest-ok] {scenario} {actual}")

    if failures:
        print("MANIFEST_INVALID")
        for f in failures:
            print("  -", f)
        return 1
    print("MANIFEST_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
