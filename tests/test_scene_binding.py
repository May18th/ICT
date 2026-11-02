"""实体重投影：决策层 twin → 3D 实体 ↔ Actor tag 的绑定表。

锁的是"高亮能落到画面上"这件事的前提，不是画面好不好看：

- 实体 id 只能来自真源，不能手写；
- 回放里每个被高亮的 id 必须要么能画、要么显式记进 `unrepresented`，不许静默丢；
- hop 只能来自推演的六跳；
- Actor tag 与关卡构建脚本一致（改了任一侧，这里就挂）；
- 同一份真源重跑逐位一致，换布局就换指纹。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from decision_poc import scene_binding
from decision_poc.scene_binding import (
    DEFAULT_OUT,
    HOP_ORDER,
    build_projection,
    load_layout,
    load_projection,
    load_replay,
    load_scene,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UE_PYTHON = REPO_ROOT / "visualization" / "unreal" / "Content" / "Python"


@pytest.fixture(scope="module")
def projection():
    return build_projection()


@pytest.fixture(scope="module")
def scene_ids() -> set[str]:
    return {str(item["twin_id"]) for item in load_scene()["objects"]}


# --- 真源 ---------------------------------------------------------------


def test_entities_come_from_scene_source(projection, scene_ids: set[str]) -> None:
    """所有 id 必须来自 scene.json；投影只做对齐，不新造对象。"""
    entity_ids = [entity.twin_id for entity in projection.entities]
    unrepresented_ids = [item.twin_id for item in projection.unrepresented]
    assert set(entity_ids) | set(unrepresented_ids) | set(projection.excluded_twin_ids) == scene_ids
    assert not (set(entity_ids) & set(unrepresented_ids))
    assert not (set(entity_ids) | set(unrepresented_ids)) & set(projection.excluded_twin_ids)
    assert len(entity_ids) == 23
    assert projection.summary.entities == projection.summary.drawable == 23


def test_sources_agree_before_projection(projection) -> None:
    """回放与语义快照必须是同一次；布局里的 twin 必须都在语义真源里。"""
    scene = load_scene()
    layout = load_layout()
    replay = load_replay()
    assert projection.replay_snapshot_hash == projection.scene_snapshot_hash == scene["snapshot_hash"]
    assert replay["snapshot_hash"] == scene["snapshot_hash"]
    layout_twins = set(scene_binding.layout_geometry(layout))
    assert layout_twins <= {str(item["twin_id"]) for item in scene["objects"]}
    assert layout["snapshot_hash"] != scene["snapshot_hash"], "两套哈希语义不同，不该相等"


# --- hop ----------------------------------------------------------------


def test_hop_comes_from_deduction_and_is_last_hop(projection) -> None:
    """hop 只能取六跳里的值；primary_hop 是回放里出现过的最后一跳。"""
    for entity in projection.entities:
        assert set(entity.hops) <= set(HOP_ORDER)
        assert entity.hops == [hop for hop in HOP_ORDER if hop in entity.hops]
        if not entity.hops:
            assert entity.primary_hop is None
            continue
        assert entity.primary_hop == entity.hops[-1]
    event_entities = [entity for entity in projection.entities if entity.hops]
    assert len(event_entities) == projection.summary.event_entities == 16
    assert sum(projection.summary.by_hop.values()) == projection.summary.event_entities
    # 道路落在处置顺序、泵站与雨水口落在资源、燃气管线落在职责冲突
    by_id = {entity.twin_id: entity for entity in projection.entities}
    assert by_id["TW-road-R-101"].primary_hop == "action"
    assert by_id["TW-pump_station-DF-PS-01"].primary_hop == "resource"
    assert by_id["TW-gas-PL-GAS-101"].primary_hop == "conflict"
    assert by_id["TW-incident-WO-20261029-007"].primary_hop == "observed"


# --- 不许静默丢 ----------------------------------------------------------


def test_highlight_resolved_or_explicitly_unrepresented(projection) -> None:
    """回放高亮的每个 id 都要么可画、要么写明为什么画不出来。"""
    steps_by_twin, _ = scene_binding.highlight_index(load_replay())
    drawable = {entity.twin_id for entity in projection.entities if entity.highlight_steps}
    missing = {item.twin_id for item in projection.unrepresented}
    assert drawable | missing == set(steps_by_twin)
    assert not (drawable & missing)
    assert projection.summary.highlight_ids == len(steps_by_twin) == 19
    assert projection.summary.highlight_ids_drawable == len(drawable) == 16
    assert projection.summary.unrepresented == len(missing) == 3
    assert missing == {
        "TW-incident-WO-20261026-013",
        "TW-incident-WO-20261029-009",
        "TW-work_site-CP-2026-041",
    }
    for item in projection.unrepresented:
        assert item.reason
        assert item.highlight_steps == steps_by_twin[item.twin_id]


# --- Actor tag 契约 ------------------------------------------------------


def test_actor_tags_match_level_builder_contract(projection) -> None:
    """tag 必须能让 `urban_twin_state.highlight()` 按 `twin:<id>` 找到 Actor。"""
    by_kind = {
        section: {entity.twin_id: entity for entity in projection.entities if entity.geometry.layout_section == section}
        for section in scene_binding.GEOMETRY_SECTIONS
    }
    for entity in projection.entities:
        geometry = entity.geometry
        assert geometry is not None
        assert geometry.actor_tags[0] == f"twin:{entity.twin_id}"
        assert f"twin:{entity.twin_id}" in geometry.actor_tags
        assert sum(tag.startswith("twin:") for tag in geometry.actor_tags) == 1
        assert len(geometry.actor_tags) == len(set(geometry.actor_tags))

    road = by_kind["roads"]["TW-road-R-101"]
    assert road.geometry.actor_tags == [
        "twin:TW-road-R-101",
        "kind:road",
        f"state:{road.layout_state}",
        f"evidence:{road.geometry.evidence}",
    ]
    water = by_kind["water"]["TW-incident-WO-20261029-007"]
    assert water.geometry.actor_tags[:3] == [
        "twin:TW-incident-WO-20261029-007",
        "kind:water",
        "state:30cm",
    ]
    pump = by_kind["facilities"]["TW-pump_station-DF-PS-01"]
    assert "kind:pump_station" in pump.geometry.actor_tags
    gas = by_kind["pipelines"]["TW-gas-PL-GAS-101"]
    assert "kind:gas" in gas.geometry.actor_tags
    assert not any(tag.startswith("state:") for tag in gas.geometry.actor_tags), "布局管线没有事件状态"


def test_unreal_scripts_still_read_these_tags() -> None:
    """UE 侧取数契约没漂：按 `twin:` 找 Actor、按 `kind:water` 显隐水面。"""
    level = (UE_PYTHON / "build_urban_level.py").read_text(encoding="utf-8")
    state = (UE_PYTHON / "urban_twin_state.py").read_text(encoding="utf-8")
    for text in (level, state):
        assert 'f"twin:{' in text or "twin:" in text
    assert 'f"twin:{' in level
    assert "kind:water" in state
    assert "kind:road" in state


# --- 确定性与产物 --------------------------------------------------------


def test_projection_is_deterministic_and_layout_sensitive(projection) -> None:
    """同一真源逐位一致；换布局就换指纹（否则"渲染的是这份结论"没法核对）。"""
    again = build_projection()
    assert again.model_dump(mode="json") == projection.model_dump(mode="json")
    assert again.projection_hash == projection.projection_hash
    assert projection.projection_hash.startswith("proj-")

    layout = copy.deepcopy(dict(load_layout()))
    for road in layout["roads"]:
        if road["road_id"] == "R-101":
            road["state"] = "normal"
    mutated = build_projection(layout=layout)
    assert mutated.projection_hash != projection.projection_hash
    mutated_road = next(entity for entity in mutated.entities if entity.twin_id == "TW-road-R-101")
    assert mutated_road.layout_state == "normal"
    assert f"state:{mutated_road.layout_state}" in mutated_road.geometry.actor_tags


def test_tracked_artifact_matches_true_source(projection) -> None:
    """仓库里提交的实体表必须能用当前真源重新算出来，禁止留旧表。"""
    assert DEFAULT_OUT.is_file(), "缺少 samples/assets/urban/semantic_entities.json"
    on_disk = load_projection()
    assert on_disk.model_dump(mode="json") == projection.model_dump(mode="json")
    assert scene_binding.main(["--check"]) == 0


def test_legacy_namespace_is_retired_not_remapped(projection) -> None:
    """旧 RD-/TN-/FAC- 命名空间一律退役，不做伪映射。"""
    assert len(projection.legacy_osm) == 24
    assert all(item.twin_id is None for item in projection.legacy_osm)
    assert all(item.disposition == "retired" for item in projection.legacy_osm)
    legacy_ids = {item.legacy_id for item in projection.legacy_osm}
    current_ids = {entity.twin_id for entity in projection.entities}
    assert not (legacy_ids & current_ids)
    assert projection.base_layer.role == "backdrop"
    assert len(projection.base_layer.osm_sha256) == 64
    assert any("OSM" in text and "背景" in text for text in projection.disclaimers)

    markdown = scene_binding.render_markdown(projection)
    assert projection.projection_hash in markdown
    for entity in projection.entities:
        assert entity.twin_id in markdown
    for item in projection.unrepresented:
        assert item.twin_id in markdown
