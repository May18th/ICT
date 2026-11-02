"""呈现层契约不变量：高亮只允许指向真源里真实存在、且布局里真的有 Actor 的 twin。

这三个约束是"六跳驱动高亮"能不能闭环的技术前提：

1. 回放第一跳的"积水现象"必须解析成 `scene.json` 里真实存在的 twin_id
   （历史实现 `f"TW-water-{event_id}"` 拼出来的 id 在真源和布局里都不存在）；
2. `scene_layout.json` 的每个对象都要带 `twin_id`，且该 id 必须是 `scene.json` 的 twin；
3. RenderJob 里任何一跳的 `highlights` 都必须能在真源里找到对应 twin——
   物理对象类（road / 管线 / 设施 / 水面）还要能在布局里找到同名 tag。

改这些映射规则前先跑本文件；它失败说明"画面亮的东西"又和真源脱钩了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc import visualize
from decision_poc.replay_export import build_replay, resolve_flood_twin
from decision_poc.scene_layout import build_layout
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"

# 有真实 Actor 的语义类；department/plan/resource/action 等是纯语义对象，不上画面
RENDERABLE_KINDS = {"road", "drainage", "gas", "power", "telecom", "water", "pump_station", "incident"}


def _scene_objects() -> list[dict]:
    scene = json.loads((DEFAULT_DATASET_ROOT / "scene.json").read_text(encoding="utf-8"))
    return scene["objects"]


def _scene_twins() -> dict[str, dict]:
    return {item["twin_id"]: item for item in _scene_objects()}


def _layout_twins() -> set[str]:
    layout = build_layout()
    twins: set[str] = set()
    for road in layout.roads:
        twins.add(road.twin_id)
    for pipeline in layout.pipelines:
        twins.add(pipeline.twin_id)
    for facility in layout.facilities:
        twins.add(facility.twin_id)
    for patch in layout.water:
        twins.add(patch.twin_id)
    return {twin for twin in twins if twin}


def test_flood_twin_is_a_real_scene_twin() -> None:
    twins = _scene_twins()
    flood = resolve_flood_twin(EVENT_ID)
    assert flood in twins, f"积水现象解析成 {flood!r}，不是 scene.json 的 twin"
    assert flood == f"TW-incident-{EVENT_ID}"


def test_layout_twin_ids_all_exist_in_scene_json() -> None:
    twins = _scene_twins()
    layout_twins = _layout_twins()
    assert layout_twins, "布局没有产出任何 twin_id"
    missing = sorted(layout_twins - set(twins))
    assert not missing, f"布局里的 twin_id 在真源中不存在: {missing}"


def test_layout_no_longer_invents_water_patch_twin() -> None:
    layout = build_layout()
    for patch in layout.water:
        assert patch.twin_id == f"TW-incident-{patch.incident_id}"
        assert not patch.twin_id.startswith("TW-water-WO-"), "水面不应再拼 TW-water-<工单> 这种假 id"


def test_replay_first_hop_highlights_real_scene_twin() -> None:
    script = build_replay(EVENT_ID, AS_OF)
    twins = _scene_twins()
    observed = script.steps[0]
    assert observed.stage == "observed"
    assert observed.highlights == [script.metrics["water_twin"]]
    for twin_id in observed.highlights:
        assert twin_id in twins


def test_render_job_highlights_resolve_to_truth_and_layout() -> None:
    snapshot = visualize.build_visual_snapshot(EVENT_ID, AS_OF)
    job = visualize.build_render_job(snapshot)
    steps = job.scene.get("steps") or []

    twins = _scene_twins()
    layout_twins = _layout_twins()

    all_highlights = {twin for step in steps for twin in step.get("highlights") or []}
    assert all_highlights, "推演作业没有任何高亮"

    missing_truth = sorted(twin for twin in all_highlights if twin not in twins)
    assert not missing_truth, f"高亮引用了真源里不存在的 twin: {missing_truth}"

    physical = [
        twin
        for twin in all_highlights
        if twins[twin].get("kind") in RENDERABLE_KINDS and not twin.startswith("TW-incident-")
    ]
    unrendered = sorted(twin for twin in physical if twin not in layout_twins)
    assert not unrendered, f"物理对象高亮在布局里没有 Actor: {unrendered}"
