"""事件实体重投影：把决策层的 twin 投影成 3D 场景可绑定的实体与绑定表。

## 为什么需要这一层

P10 的渲染契约是：

    scene_render → RenderJob(scene.steps[i].highlights) → UE urban_twin_state.apply_job()
                 → 按 `twin:<twin_id>` tag 找 Actor 换材质 → 写回 ack

`highlights` 里是决策层真源的 `TW-*`，UE 侧只认 `twin:<id>`。但 3D 生产侧历史留下的
`semantic_entities.json` 用的是 `RD-` / `TN-` / `FAC-` 另一套命名空间，与决策层零交集
（见 `docs/competition/technical-breadth-and-presentation-boundary.md` §4.1）。结果是
"作业落盘、引擎在跑、画面却没变"。

本模块不新增任何推理，只把已经在算的东西对齐：

1. **实体重投影**：`scene.json`（63 个 twin 的真源）+ `scene_layout.json`（几何）
   + 推演回放（hop）→ 生成 `semantic_entities.json`。`twin_id` 全部来自真源，
   禁止手写；`hop` 全部来自回放的 `steps[i].stage`。
2. **绑定表**：`twin_id ↔ 布局对象 ↔ Actor tag`。**没有几何的对象显式列进
   `unrepresented`**，不静默丢——它们正是"高亮了但画面上没有"的来源。
3. **坐标口径写死**：事件层用布局本地米制坐标（示意图，非测绘）；OSM 真实几何降级为
   背景底图，不再充当事件层几何。旧 24 个 OSM 实体的处理写进 `legacy_osm`
   （一律 `twin_id: null`，不伪造映射）。

## 规则（可被测试核对，不靠口头约定）

- `primary_hop` = 该对象在回放里出现过的**最后一跳**。回放把 `affected` / `owner` /
  `conflict` 三跳组装成同一批高亮，只按首跳取名会让所有道路都算"受影响"；
  取最后一跳才是真正把它和别的对象区分开的那一跳（道路→处置顺序、泵站/雨水口→资源、
  燃气管线→职责冲突、积水水面→现象）。
- `drawable` 当且仅当有几何。没有几何却在 `highlights` 里的对象进 `unrepresented`，
  `summary.highlight_ids = summary.highlight_ids_drawable + len(unrepresented)` 恒成立。
- Actor tag 与 `visualization/unreal/Content/Python/build_urban_level.py` 逐字对齐；
  两边任何一侧改了 tag 格式，测试会挂。

命令行：

    python -m decision_poc.scene_binding                 # 重建并覆盖 samples 里的实体表
    python -m decision_poc.scene_binding --three-d        # 同时同步到 3D 生产侧场景目录
    python -m decision_poc.scene_binding --check         # 只比对，不一致退出码 1
    python -m decision_poc.scene_binding --markdown docs/reports/scene-binding-report.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import Field, model_validator

from .contracts import StrictModel

SCHEMA_VERSION = "urban-twin-semantic-entities-v1"
HOP_ORDER: tuple[str, ...] = ("observed", "affected", "owner", "conflict", "resource", "action")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENE_PATH = REPO_ROOT / "samples" / "assets" / "urban" / "scene.json"
LAYOUT_PATH = REPO_ROOT / "samples" / "assets" / "urban" / "scene_layout.json"
REPLAY_PATH = REPO_ROOT / "samples" / "replay" / "core_run.json"
LEGACY_PATH = REPO_ROOT / "samples" / "assets" / "urban" / "semantic_entities_legacy_osm.json"
DEFAULT_OUT = REPO_ROOT / "samples" / "assets" / "urban" / "semantic_entities.json"

# 3D 生产侧（另一仓库目录）：OSM 底图与其历史实体表，缺失时整段降级为空，不报错。
THREE_D_SCENE_DIR = REPO_ROOT.parent / "blender-ue-scene" / "scenes" / "WO-20261029-007"
OSM_BACKDROP_PATH = REPO_ROOT.parent / "blender-ue-scene" / "out" / "osm_chengdu_centre.json"

# 布局里能落成 Actor 的四段；districts 落成地面 Actor（只有 district: 标签，没有 twin:）。
GEOMETRY_SECTIONS: tuple[str, ...] = ("roads", "pipelines", "facilities", "water")


# --- 模型 ----------------------------------------------------------------


class Point(StrictModel):
    x: float
    y: float


class EntityGeometry(StrictModel):
    """布局里的一段几何 + 关卡会挂在 Actor 上的 tag。

    P3-D：几何与语义解耦。`geometry_ref` 是几何真源里的稳定 ID（如 `pipeline:ST-TRUNK`），
    `twin_id` 是决策语义层的官方 ID，**可以为空**（管线在决策层还没有官方 ID 时就是空）。
    这三个字段 `exclude=True`：不参与序列化与投影指纹，保证既有产物逐位不变。
    """

    layout_ref: str = Field(min_length=1)
    layout_section: str = Field(min_length=1)
    shape: str = Field(min_length=1)  # polyline / polygon / point
    points: list[Point] = Field(min_length=1)
    z: float
    depth_cm: float | None = None
    evidence: str = ""
    actor_tags: list[str] = Field(min_length=1)
    # P3-D 新增（默认值 + exclude，老数据 dump 结果不变）
    geometry_ref: str = Field(default="", exclude=True)
    twin_id: str | None = Field(default=None, exclude=True)
    pipeline_id: str | None = Field(default=None, exclude=True)
    kind: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def shape_matches_points(self) -> "EntityGeometry":
        if self.shape == "point":
            if len(self.points) != 1:
                raise ValueError("point geometry must have exactly one point")
        elif self.shape in {"polyline", "polygon"}:
            if len(self.points) < 2:
                raise ValueError(f"{self.shape} geometry needs at least two points")
        else:
            raise ValueError(f"unknown geometry shape {self.shape!r}")
        return self


class GeometryBinding(StrictModel):
    """只有几何、没有官方 twin_id 的绑定项（P3-D 解耦产物）。

    `geometry_ref` 恒等于 `pipeline:<pipeline_id>` 这类几何真源 ID；
    以后决策层给出官方 ID 时，只需把 `twin_id` 填上，几何不用动。
    """

    geometry_ref: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    twin_id: str | None = None
    kind: str = ""
    layout_section: str = Field(min_length=1)
    points: list[Point] = Field(min_length=1)
    z: float
    evidence: str = ""
    actor_tags: list[str] = Field(default_factory=list)


class SemanticEntity(StrictModel):
    twin_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    region_id: str | None = None
    # 两个 state 不能互相替代：语义层是物理状态，布局层是画面上真正换材质的事件状态
    scene_state: str = ""
    layout_state: str = ""
    primary_hop: str | None = None
    hops: list[str] = Field(default_factory=list)
    highlight_steps: list[int] = Field(default_factory=list)
    drawable: bool = True
    geometry: EntityGeometry | None = None
    note: str = ""

    @model_validator(mode="after")
    def drawable_means_geometry(self) -> "SemanticEntity":
        if self.drawable != (self.geometry is not None):
            raise ValueError(f"{self.twin_id}: drawable 必须与 geometry 是否存在一致")
        if self.hops and self.primary_hop is None:
            raise ValueError(f"{self.twin_id}: 有 hop 就必须有 primary_hop")
        if self.primary_hop is not None and self.primary_hop not in HOP_ORDER:
            raise ValueError(f"{self.twin_id}: 未知 hop {self.primary_hop!r}")
        return self


class Unrepresented(StrictModel):
    """在 highlights 里、但场景里没有几何的对象。不静默丢，逐个给原因。"""

    twin_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    highlight_steps: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class LegacyBinding(StrictModel):
    """旧 3D 命名空间的实体：记录处置结论，不给伪映射。"""

    legacy_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    osm_ref: str = ""
    twin_id: str | None = None
    disposition: str = "retired"

    @model_validator(mode="after")
    def retired_has_no_mapping(self) -> "LegacyBinding":
        if self.disposition == "retired" and self.twin_id is not None:
            raise ValueError(f"{self.legacy_id}: 已退役的旧实体不能再指向 twin")
        return self


class BaseLayer(StrictModel):
    osm_file: str = ""
    osm_sha256: str = ""
    role: str = "backdrop"
    note: str = ""


class CoordinateFrame(StrictModel):
    name: str = "layout_local_meters"
    units: str = "m"
    origin: str = ""
    note: str = ""


class ProjectionSummary(StrictModel):
    entities: int = 0
    drawable: int = 0
    unrepresented: int = 0
    excluded: int = 0
    event_entities: int = 0
    highlight_ids: int = 0
    highlight_ids_drawable: int = 0
    by_hop: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    excluded_by_kind: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def highlight_ids_add_up(self) -> "ProjectionSummary":
        if self.highlight_ids != self.highlight_ids_drawable + self.unrepresented:
            raise ValueError("highlight_ids 必须等于可画 + 不可画之和，不允许静默丢对象")
        return self


class SemanticProjection(StrictModel):
    schema_version: str = SCHEMA_VERSION
    projection_hash: str = ""
    event_id: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    district: str | None = None
    scene_snapshot_hash: str = ""
    layout_snapshot_hash: str = ""
    replay_snapshot_hash: str = ""
    hop_order: list[str] = Field(default_factory=list)
    frame: CoordinateFrame = Field(default_factory=CoordinateFrame)
    base_layer: BaseLayer = Field(default_factory=BaseLayer)
    entities: list[SemanticEntity] = Field(default_factory=list)
    # P3-D：只有几何、没有官方 twin_id 的布局对象（当前是 VD02 的 122 条管线）。
    # exclude=True 让它不进入 JSON 产物与指纹（既有产物保持逐位一致），
    # 需要落盘时用 write_geometry_index() 单独导出。
    geometry_index: list["GeometryBinding"] = Field(default_factory=list, exclude=True)
    unrepresented: list[Unrepresented] = Field(default_factory=list)
    excluded_twin_ids: list[str] = Field(default_factory=list)
    legacy_osm: list[LegacyBinding] = Field(default_factory=list)
    summary: ProjectionSummary = Field(default_factory=ProjectionSummary)
    disclaimers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def entities_are_sorted_and_unique(self) -> "SemanticProjection":
        ids = [entity.twin_id for entity in self.entities]
        if ids != sorted(ids):
            raise ValueError("entities 必须按 twin_id 排序，避免 diff 噪声")
        if len(set(ids)) != len(ids):
            raise ValueError("entities 里出现重复 twin_id")
        if self.hop_order != list(HOP_ORDER):
            raise ValueError("hop_order 必须等于六跳顺序")
        return self


# --- 读取 ----------------------------------------------------------------


def load_json(path: Path | str) -> Mapping[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_scene(path: Path | str | None = None) -> Mapping[str, Any]:
    return load_json(path or SCENE_PATH)


def load_layout(path: Path | str | None = None) -> Mapping[str, Any]:
    return load_json(path or LAYOUT_PATH)


def load_replay(path: Path | str | None = None) -> Mapping[str, Any]:
    return load_json(path or REPLAY_PATH)


# --- 组装 ----------------------------------------------------------------


def _point(payload: Mapping[str, Any]) -> Point:
    return Point(x=float(payload["x"]), y=float(payload["y"]))


def _tag_list(*tags: str) -> list[str]:
    return [tag for tag in tags if tag]


def layout_geometry_index(layout: Mapping[str, Any]) -> dict[str, EntityGeometry]:
    """布局四段 → `geometry_ref` 索引（P3-D）。

    与 `layout_geometry()` 的区别：**允许没有 twin_id 的对象**（当前只有管线会这样）。
    管线的 `geometry_ref = pipeline:<pipeline_id>`；`twin_id` 有就带、没有就 None。
    其余三段（道路/设施/水面）仍要求 twin_id，缺了照旧报错 —— 不静默补 ID。
    """
    index: dict[str, EntityGeometry] = {}

    def register(geometry: EntityGeometry) -> None:
        ref = geometry.geometry_ref or geometry.layout_ref
        if ref in index:
            raise ValueError(f"{ref}: 布局里出现两次几何")
        index[ref] = geometry

    for road in layout.get("roads", []):
        twin_id = str(road["twin_id"])
        if not twin_id:
            raise ValueError(f"roads:{road['road_id']}: 布局对象没有 twin_id")
        evidence = str(road.get("evidence", ""))
        register(EntityGeometry(
            layout_ref=f"roads:{road['road_id']}",
            layout_section="roads",
            shape="polyline",
            points=[_point(item) for item in road["polyline"]],
            z=float(road["z"]),
            evidence=evidence,
            actor_tags=_tag_list(
                f"twin:{twin_id}",
                "kind:road",
                f"state:{road['state']}",
                f"evidence:{evidence}",
            ),
            geometry_ref=f"road:{road['road_id']}",
            twin_id=twin_id,
            kind="road",
        ))

    for pipe in layout.get("pipelines", []):
        pipeline_id = str(pipe.get("pipeline_id") or "")
        if not pipeline_id:
            raise ValueError("布局管线缺 pipeline_id（几何真源 ID 不能为空）")
        twin_id = pipe.get("twin_id")
        twin_id = str(twin_id) if twin_id else None
        kind = str(pipe.get("kind") or "pipeline")
        evidence = str(pipe.get("evidence", ""))
        actor_tags = _tag_list(f"twin:{twin_id}" if twin_id else "",
                               f"kind:{kind}", f"evidence:{evidence}")
        if not actor_tags:  # 无 twin_id 时至少保留几何 ID，便于 UE 侧选 Actor
            actor_tags = [f"geometry:pipeline:{pipeline_id}"]
        register(EntityGeometry(
            layout_ref=f"pipelines:{pipeline_id}",
            layout_section="pipelines",
            shape="polyline",
            points=[_point(item) for item in pipe["polyline"]],
            z=float(pipe["z"]),
            evidence=evidence,
            actor_tags=actor_tags,
            geometry_ref=f"pipeline:{pipeline_id}",
            twin_id=twin_id,
            pipeline_id=pipeline_id,
            kind=kind,
        ))

    for facility in layout.get("facilities", []):
        twin_id = str(facility["twin_id"])
        if not twin_id:
            raise ValueError(f"facilities:{facility['facility_id']}: 布局对象没有 twin_id")
        evidence = str(facility.get("evidence", ""))
        register(EntityGeometry(
            layout_ref=f"facilities:{facility['facility_id']}",
            layout_section="facilities",
            shape="point",
            points=[_point(facility["xy"])],
            z=float(facility["z"]),
            evidence=evidence,
            actor_tags=_tag_list(
                f"twin:{twin_id}",
                f"kind:{facility['facility_type']}",
                f"state:{facility['state']}",
                f"evidence:{evidence}",
            ),
            geometry_ref=f"facility:{facility['facility_id']}",
            twin_id=twin_id,
            kind=str(facility["facility_type"]),
        ))

    for patch in layout.get("water", []):
        twin_id = str(patch["twin_id"])
        if not twin_id:
            raise ValueError(f"water:{patch['incident_id']}: 布局对象没有 twin_id")
        evidence = str(patch.get("evidence", ""))
        depth = patch.get("depth_cm")
        register(EntityGeometry(
            layout_ref=f"water:{patch['incident_id']}",
            layout_section="water",
            shape="polygon",
            points=[_point(item) for item in patch["polygon"]],
            z=float(patch["z"]),
            depth_cm=float(depth) if depth is not None else None,
            evidence=evidence,
            actor_tags=_tag_list(
                f"twin:{twin_id}",
                "kind:water",
                f"state:{depth:g}cm" if isinstance(depth, (int, float)) else "",
                f"evidence:{evidence}",
            ),
            geometry_ref=f"water:{patch['incident_id']}",
            twin_id=twin_id,
            kind="water",
        ))

    return index


def layout_geometry(layout: Mapping[str, Any]) -> dict[str, EntityGeometry]:
    """布局四段 → twin_id 索引（P3-D 起只返回**有官方 twin_id** 的对象）。

    没有 twin_id 的管线不再抛错，而是进 `layout_geometry_index()` 的几何索引，
    由 `build_projection()` 收进 `projection.geometry_index`。
    """
    index: dict[str, EntityGeometry] = {}
    for geometry in layout_geometry_index(layout).values():
        if not geometry.twin_id:
            continue
        if geometry.twin_id in index:
            raise ValueError(f"{geometry.twin_id}: 布局里出现两次几何")
        index[geometry.twin_id] = geometry
    return index


def highlight_index(replay: Mapping[str, Any]) -> tuple[dict[str, list[int]], dict[str, list[str]]]:
    """回放 → (每个 twin 出现在第几步, 每个 twin 被哪几跳用到)。"""
    steps_by_twin: dict[str, list[int]] = {}
    hops_by_twin: dict[str, list[str]] = {}
    for step in replay.get("steps", []):
        index = int(step["index"])
        stage = str(step["stage"])
        for twin_id in step.get("highlights", []):
            steps_by_twin.setdefault(str(twin_id), []).append(index)
            hops_by_twin.setdefault(str(twin_id), [])
            if stage not in hops_by_twin[str(twin_id)]:
                hops_by_twin[str(twin_id)].append(stage)
    return dict(sorted(steps_by_twin.items())), dict(sorted(hops_by_twin.items()))


def primary_hop(hops: Sequence[str]) -> str | None:
    """最后一跳：回放把 affected/owner/conflict 组装成同一批高亮，取首跳会全都算"受影响"。"""
    ordered = [hop for hop in HOP_ORDER if hop in set(hops)]
    return ordered[-1] if ordered else None


def _layout_state(geometry: EntityGeometry) -> str:
    for tag in geometry.actor_tags:
        if tag.startswith("state:"):
            return tag.split(":", 1)[1]
    return ""


def _excluded_reason(kind: str) -> str:
    if kind == "work_site":
        return "scene_layout.json 没有 work_sites 段，施工面没有几何可绑"
    if kind == "incident":
        return "scene_layout.json 的 water 段只导出本事件水面，历史事件无几何可绑"
    return f"scene_layout.json 未导出 {kind} 几何"


def _osm_backdrop() -> BaseLayer:
    if not OSM_BACKDROP_PATH.is_file():
        return BaseLayer(
            osm_file="",
            osm_sha256="",
            role="backdrop",
            note="OSM 底图不在本机；事件层几何只依赖 scene_layout.json，不依赖底图",
        )
    digest = hashlib.sha256(OSM_BACKDROP_PATH.read_bytes()).hexdigest()
    return BaseLayer(
        osm_file=OSM_BACKDROP_PATH.name,
        osm_sha256=digest,
        role="backdrop",
        note=(
            "OSM 真实几何只作背景底图：不声称某条真实道路发生了本事件，"
            "事件实体的坐标一律来自 scene_layout.json（示意图布局，非测绘坐标）"
        ),
    )


def _legacy_bindings() -> list[LegacyBinding]:
    if not LEGACY_PATH.is_file():
        return []
    payload = load_json(LEGACY_PATH)
    bindings: list[LegacyBinding] = []
    for item in payload.get("entities", []):
        osm_type = item.get("osm_type")
        osm_id = item.get("osm_id")
        osm_ref = f"osm:{osm_type}/{osm_id}" if osm_type and osm_id else ""
        bindings.append(
            LegacyBinding(
                legacy_id=str(item["twin_id"]),
                kind=str(item.get("kind", "")),
                osm_ref=osm_ref,
                twin_id=None,
                disposition="retired",
            )
        )
    return sorted(bindings, key=lambda item: item.legacy_id)


def _projection_payload(projection: SemanticProjection) -> dict[str, Any]:
    payload = projection.model_dump(mode="json")
    payload["projection_hash"] = ""
    return payload


def projection_hash(projection: SemanticProjection) -> str:
    encoded = json.dumps(
        _projection_payload(projection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "proj-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def build_projection(
    *,
    scene: Mapping[str, Any] | None = None,
    layout: Mapping[str, Any] | None = None,
    replay: Mapping[str, Any] | None = None,
) -> SemanticProjection:
    """把三份真源投影成实体表。任何一侧对不上就抛错，不猜。"""
    scene = scene if scene is not None else load_scene()
    layout = layout if layout is not None else load_layout()
    replay = replay if replay is not None else load_replay()

    scene_objects = {str(item["twin_id"]): item for item in scene.get("objects", [])}
    if not scene_objects:
        raise ValueError("scene.json 里没有 objects，无法投影")

    geometries = layout_geometry(layout)
    # P3-D：没有官方 twin_id 的几何（当前是 VD02 的 122 条管线）单独收进几何索引。
    # 它们不进 entities（entities 必须有 twin_id），也不影响 summary 与指纹。
    geometry_index = [
        GeometryBinding(
            geometry_ref=geometry.geometry_ref or geometry.layout_ref,
            pipeline_id=str(geometry.pipeline_id),
            twin_id=None,
            kind=geometry.kind,
            layout_section=geometry.layout_section,
            points=geometry.points,
            z=geometry.z,
            evidence=geometry.evidence,
            actor_tags=geometry.actor_tags,
        )
        for geometry in layout_geometry_index(layout).values()
        if not geometry.twin_id and geometry.layout_section == "pipelines"
    ]
    steps_by_twin, hops_by_twin = highlight_index(replay)
    highlight_ids = set(steps_by_twin)

    unknown_highlights = sorted(highlight_ids - set(scene_objects))
    if unknown_highlights:
        raise ValueError(f"回放高亮了语义真源里不存在的对象: {unknown_highlights}")

    entities: list[SemanticEntity] = []
    for twin_id in sorted(geometries):
        if twin_id not in scene_objects:
            raise ValueError(f"布局对象 {twin_id} 在 scene.json 里不存在（真源分叉）")
        item = scene_objects[twin_id]
        geometry = geometries[twin_id]
        hops = hops_by_twin.get(twin_id, [])
        note = ""
        if geometry.layout_section == "water":
            note = "水面 Actor 的渲染 tag 是 kind:water；语义 kind 仍是 incident"
        elif geometry.layout_section == "pipelines":
            note = "布局管线没有事件状态字段，state 只存在于语义层"
        entities.append(
            SemanticEntity(
                twin_id=twin_id,
                kind=str(item["kind"]),
                name=str(item["name"]),
                region_id=item.get("region_id"),
                scene_state=str(item.get("state", "")),
                layout_state=_layout_state(geometry),
                primary_hop=primary_hop(hops),
                hops=[hop for hop in HOP_ORDER if hop in hops],
                highlight_steps=steps_by_twin.get(twin_id, []),
                drawable=True,
                geometry=geometry,
                note=note,
            )
        )

    unrepresented = [
        Unrepresented(
            twin_id=twin_id,
            kind=str(scene_objects[twin_id]["kind"]),
            name=str(scene_objects[twin_id]["name"]),
            highlight_steps=steps_by_twin[twin_id],
            reason=_excluded_reason(str(scene_objects[twin_id]["kind"])),
        )
        for twin_id in sorted(highlight_ids - set(geometries))
    ]

    excluded = sorted(set(scene_objects) - set(geometries) - highlight_ids)
    event_entities = [entity for entity in entities if entity.hops]
    by_hop = {hop: 0 for hop in HOP_ORDER}
    for entity in event_entities:
        by_hop[str(entity.primary_hop)] += 1
    by_kind: dict[str, int] = {}
    for entity in entities:
        by_kind[entity.kind] = by_kind.get(entity.kind, 0) + 1
    excluded_by_kind: dict[str, int] = {}
    for twin_id in excluded:
        kind = str(scene_objects[twin_id]["kind"])
        excluded_by_kind[kind] = excluded_by_kind.get(kind, 0) + 1

    summary = ProjectionSummary(
        entities=len(entities),
        drawable=sum(1 for entity in entities if entity.drawable),
        unrepresented=len(unrepresented),
        excluded=len(excluded),
        event_entities=len(event_entities),
        highlight_ids=len(highlight_ids),
        highlight_ids_drawable=len(highlight_ids) - len(unrepresented),
        by_hop={hop: count for hop, count in by_hop.items() if count},
        by_kind=dict(sorted(by_kind.items())),
        excluded_by_kind=dict(sorted(excluded_by_kind.items())),
    )

    projection = SemanticProjection(
        schema_version=SCHEMA_VERSION,
        projection_hash="",
        event_id=str(replay.get("event_id", "")),
        as_of=str(replay.get("as_of", "")),
        district=replay.get("district"),
        scene_snapshot_hash=str(scene.get("snapshot_hash", "")),
        layout_snapshot_hash=str(layout.get("snapshot_hash", "")),
        replay_snapshot_hash=str(replay.get("snapshot_hash", "")),
        hop_order=list(HOP_ORDER),
        geometry_index=geometry_index,
        frame=CoordinateFrame(
            name="layout_local_meters",
            units="m",
            origin="scene_layout.json 的布局原点（示意图布局，非测绘坐标）",
            note=(
                "事件层与关卡都在布局本地米制坐标里；OSM 投影（lat0/lon0/k_lat/k_lon）"
                "属于底图，不参与事件实体定位"
            ),
        ),
        base_layer=_osm_backdrop(),
        entities=entities,
        unrepresented=unrepresented,
        excluded_twin_ids=excluded,
        legacy_osm=_legacy_bindings(),
        summary=summary,
        disclaimers=[
            "底图为真实 OSM 几何，只作背景；事件层（积水、封路、施工、设施状态）为合成内容。",
            "场景布局是示意图坐标，不是测绘坐标：实体位置只能用于本场景的可视化，不能用于距离测量。",
            "被高亮但无几何的对象列在 unrepresented 里，渲染时应当显示为'画面上没有'，而不是假装亮过。",
            "片区（region）在关卡里是地面 Actor，只有 district: 标签、没有 twin: 标签，不参与高亮。",
            "本表不含时间戳：同一份 scene/layout/replay 重跑逐位一致，projection_hash 可当场核对。",
        ],
    )
    return projection.model_copy(update={"projection_hash": projection_hash(projection)})


# --- 输出 ----------------------------------------------------------------


def write_projection(path: Path | str, projection: SemanticProjection) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(projection.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def write_geometry_index(path: Path | str, projection: SemanticProjection) -> Path:
    """把"只有几何、没有官方 twin_id"的绑定项单独落盘（P3-D）。

    与 `write_projection` 分开写，是为了让主产物（语义实体表）的格式与指纹
    在补齐管线几何之前保持逐位一致，避免任何下游 diff 噪声。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": f"{SCHEMA_VERSION}+geometry-index",
        "projection_hash": projection.projection_hash,
        "event_id": projection.event_id,
        "as_of": projection.as_of,
        "note": "geometry_ref 是几何真源 ID；twin_id 为 null 表示决策层尚未给出官方 ID。",
        "count": len(projection.geometry_index),
        "items": [item.model_dump(mode="json") for item in projection.geometry_index],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def render_markdown(projection: SemanticProjection) -> str:
    """生成对外的绑定表报告。标题写"由 ... 生成，不要手改"，与其它报告一致。"""
    summary = projection.summary
    lines = [
        "# 事件实体重投影与绑定表",
        "",
        "由 `python -m decision_poc.scene_binding --markdown <path>` 生成，**不要手改**。",
        "",
        f"- 事件：`{projection.event_id}` @ `{projection.as_of}`（片区 {projection.district or '-'}）",
        f"- 投影指纹：`{projection.projection_hash}`",
        f"- 真源哈希：scene `{projection.scene_snapshot_hash[:12]}` / "
        f"layout `{projection.layout_snapshot_hash[:12]}` / "
        f"replay `{projection.replay_snapshot_hash[:12]}`",
        f"- 实体 {summary.entities} 个（全部可画）/ 未落到几何 {summary.unrepresented} 个 / "
        f"本事件会被高亮 {summary.event_entities} 个",
        f"- 回放高亮 id {summary.highlight_ids} 个：可画 {summary.highlight_ids_drawable} + "
        f"不可画 {summary.unrepresented}（不允许静默丢）",
        "",
        "## 1 主跳分布（primary_hop = 回放里出现过的最后一跳）",
        "",
        "| hop | 对象数 |",
        "| --- | --- |",
    ]
    for hop, count in summary.by_hop.items():
        lines.append(f"| `{hop}` | {count} |")
    lines += [
        "",
        "## 2 实体 ↔ 几何 ↔ Actor tag",
        "",
        "| twin_id | kind | primary_hop | 几何 | Actor tag（`twin:`/`kind:`/`state:`） | 证据行 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entity in projection.entities:
        geometry = entity.geometry
        assert geometry is not None  # drawable 的实体一定有几何
        tags = " / ".join(geometry.actor_tags)
        hop = entity.primary_hop or "-"
        lines.append(
            f"| `{entity.twin_id}` | {entity.kind} | `{hop}` | {geometry.layout_ref} "
            f"| {tags} | {geometry.evidence or '-'} |"
        )
    lines += [
        "",
        "## 3 高亮了但场景里没有几何（`unrepresented`）",
        "",
        "| twin_id | kind | 出现步 | 原因 |",
        "| --- | --- | --- | --- |",
    ]
    if projection.unrepresented:
        for item in projection.unrepresented:
            steps = ",".join(str(index) for index in item.highlight_steps)
            lines.append(f"| `{item.twin_id}` | {item.kind} | {steps} | {item.reason} |")
    else:
        lines.append("| - | - | - | 无 |")
    lines += [
        "",
        f"## 4 旧 3D 命名空间处置（{len(projection.legacy_osm)} 个）",
        "",
        "旧实体一律 `twin_id: null`（不伪造映射）；`osm_ref` 只记录它当初绑定的真实 OSM 元素。",
        "",
        "| legacy_id | kind | osm_ref | 处置 |",
        "| --- | --- | --- | --- |",
    ]
    for item in projection.legacy_osm:
        lines.append(f"| `{item.legacy_id}` | {item.kind} | {item.osm_ref or '-'} | {item.disposition} |")
    lines += [
        "",
        "## 5 口径声明",
        "",
    ]
    lines += [f"- {text}" for text in projection.disclaimers]
    lines.append("")
    return "\n".join(lines)


def load_projection(path: Path | str | None = None) -> SemanticProjection:
    return SemanticProjection.model_validate(load_json(path or DEFAULT_OUT))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="project decision-layer twins onto 3D scene entities")
    parser.add_argument("--out", default=None, help="写入路径（默认 samples/assets/urban/semantic_entities.json）")
    parser.add_argument(
        "--three-d",
        action="store_true",
        help="同时写入 3D 生产侧场景目录（blender-ue-scene/scenes/<scene>/semantic_entities.json）",
    )
    parser.add_argument("--check", action="store_true", help="只比对磁盘上的实体表，不一致退出码 1")
    parser.add_argument("--markdown", default=None, help="另外生成绑定表报告（markdown）")
    parser.add_argument("--scene", default=None)
    parser.add_argument("--layout", default=None)
    parser.add_argument("--replay", default=None)
    args = parser.parse_args(argv)

    projection = build_projection(
        scene=load_scene(args.scene) if args.scene else None,
        layout=load_layout(args.layout) if args.layout else None,
        replay=load_replay(args.replay) if args.replay else None,
    )
    summary = projection.summary
    print(
        f"实体 {summary.entities}（可画 {summary.drawable}）/ 本事件高亮 {summary.event_entities} / "
        f"无几何 {summary.unrepresented} / 排除非空间对象 {summary.excluded} / "
        f"指纹 {projection.projection_hash}"
    )
    if summary.unrepresented:
        for item in projection.unrepresented:
            print(f"  无几何: {item.twin_id} —— {item.reason}")

    if args.check:
        target = Path(args.out) if args.out else DEFAULT_OUT
        if not target.is_file():
            print(f"FAIL: {target} 不存在，先跑一次不带 --check 的生成")
            return 1
        on_disk = SemanticProjection.model_validate(load_json(target))
        if on_disk.model_dump(mode="json") != projection.model_dump(mode="json"):
            print(f"FAIL: {target.name} 与真源不一致，重新生成后再提交")
            return 1
        print(f"OK: {target.name} 与真源一致")
        return 0

    target = write_projection(args.out or DEFAULT_OUT, projection)
    print(f"写入 {target}")
    if args.three_d:
        if not THREE_D_SCENE_DIR.parent.is_dir():
            print(f"跳过 3D 侧：{THREE_D_SCENE_DIR.parent} 不存在")
        else:
            print(f"写入 {write_projection(THREE_D_SCENE_DIR / 'semantic_entities.json', projection)}")
    if args.markdown:
        report = Path(args.markdown)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown(projection), encoding="utf-8")
        print(f"写入 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOP_ORDER",
    "SCHEMA_VERSION",
    "DEFAULT_OUT",
    "SemanticProjection",
    "SemanticEntity",
    "Unrepresented",
    "build_projection",
    "layout_geometry",
    "layout_geometry_index",
    "write_geometry_index",
    "highlight_index",
    "primary_hop",
    "projection_hash",
    "render_markdown",
    "write_projection",
    "load_projection",
]
