"""Temporal semantic layer over the city twin (P4).

Two models, deliberately separate:

- `TwinObject` is the physical side: kind, lifecycle, observed-at, scalar attributes.
- `SemanticTriple` is the meaning side: duty, dependency, authority; versioned and
  reviewable, so a corrected relation never rewrites a physical fact.

They meet at `twin_id`; the physical side meets the asset ledger at `asset_ids`.
That three-way alignment is what lets provenance answer "which file, which line".

Rules enforced here, not merely documented:

1. Temporal window is half-open `[valid_from, valid_until)`; `valid_until=None`
   is open-ended. `as_of` is mandatory -- a "latest" default would silently mix
   facts observed at different times.
2. Only `confirmed` triples participate by default. `candidate` returns only when
   explicitly asked; `deprecated` never does (expired sources must not feed
   deduction).
3. Visibility comes from **the evidence asset's governance record**, not from a
   field on the triple. A label duplicated on the triple is a second source of
   truth that drifts; permission must bind to the asset.
4. Permission filtering runs before `min_confidence` and before ordering, so a
   high-confidence record cannot leak by ranking first.
5. Every result carries `snapshot_hash`; provenance records anchor to it.
6. Empty results return `partial` with a machine-readable reason code.

No third-party dependency: adjacency is a dict of lists. The demo dataset is 15
assets / 62 objects -- a graph library would add install risk for zero gain.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel, ToolContext, ToolError, ToolResult, ToolStats
from .governance import AssetStatus, ErrorCode, KnowledgeVersion
from .ontology import EntityCandidate, OntologyBundle, RelationCandidate, ReviewDecision

DEFAULT_VALID_FROM = datetime.fromisoformat("2000-01-01T00:00:00+08:00")


class ObjectKind(StrEnum):
    ROAD = "road"
    DRAINAGE = "drainage"
    WATER = "water"
    GAS = "gas"
    POWER = "power"
    TELECOM = "telecom"
    PUMP_STATION = "pump_station"
    REGION = "region"
    WORK_SITE = "work_site"
    INCIDENT = "incident"
    WEATHER = "weather"
    DEPARTMENT = "department"
    RESOURCE = "resource"
    PLAN = "plan"
    ACTION = "action"


class LifecycleState(StrEnum):
    PLANNED = "planned"
    IN_SERVICE = "in_service"
    UNDER_MAINTENANCE = "under_maintenance"
    DECOMMISSIONED = "decommissioned"
    UNKNOWN = "unknown"


class Predicate(StrEnum):
    SERVES = "serves"
    DEPENDS_ON = "depends_on"
    LOCATED_IN = "located_in"
    MAINTAINED_BY = "maintained_by"
    AFFECTS = "affects"
    REGULATED_BY = "regulated_by"
    CONFLICTS_WITH = "conflicts_with"
    UNDER_CONSTRUCTION = "under_construction"
    PRECEDES = "precedes"


class ReviewStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    DEPRECATED = "deprecated"


class EmptyReason(StrEnum):
    NO_NEIGHBOR = "NO_NEIGHBOR"
    PERMISSION_FILTERED = "PERMISSION_FILTERED"
    REVIEW_PENDING = "REVIEW_PENDING"
    BELOW_CONFIDENCE = "BELOW_CONFIDENCE"
    OUT_OF_WINDOW = "OUT_OF_WINDOW"


# --- extraction vocabulary -> twin vocabulary ----------------------------
#
# Kept as data so the extractor (P2) and the twin (P4) can evolve apart without
# either side hard-coding the other's strings.
#
# Two deliberate departures from the design doc's enum, both forced by the data:
# - extra kinds: the dataset carries weather/incident/department/resource/plan/
#   action nodes, and the six-stage deduction starts at an incident.
# - `under_construction` / `precedes`: they are the event driver and the处置顺序
#   backbone. Dropping them would delete the two edges deduction needs most.

ENTITY_KIND_MAP: dict[str, ObjectKind] = {
    "Road": ObjectKind.ROAD,
    "DrainageFacility": ObjectKind.DRAINAGE,  # refined to PUMP_STATION below
    "Area": ObjectKind.REGION,
    "ConstructionProject": ObjectKind.WORK_SITE,
    "Incident": ObjectKind.INCIDENT,
    "WeatherEvent": ObjectKind.WEATHER,
    "Department": ObjectKind.DEPARTMENT,
    "Resource": ObjectKind.RESOURCE,
    "EmergencyPlan": ObjectKind.PLAN,
    "Action": ObjectKind.ACTION,
}

PIPELINE_KIND_MAP: dict[str, ObjectKind] = {
    "drainage": ObjectKind.DRAINAGE,
    "water": ObjectKind.WATER,
    "gas": ObjectKind.GAS,
    "power": ObjectKind.POWER,
    "telecom": ObjectKind.TELECOM,
}

# `X affected_by Y` is restated as `Y affects X`: deduction starts from the
# event and walks outward, so the影响 source must be the subject.
PREDICATE_MAP: dict[str, Predicate] = {
    "responsible_for": Predicate.MAINTAINED_BY,
    "located_in": Predicate.LOCATED_IN,
    "connected_to": Predicate.DEPENDS_ON,
    "affected_by": Predicate.AFFECTS,
    "covered_by_plan": Predicate.REGULATED_BY,
    "conflicts_with": Predicate.CONFLICTS_WITH,
    "under_construction": Predicate.UNDER_CONSTRUCTION,
    "precedes": Predicate.PRECEDES,
}

REVERSED_PREDICATES = frozenset({Predicate.AFFECTS})

LIFECYCLE_BY_STATE: dict[str, LifecycleState] = {
    "in_service": LifecycleState.IN_SERVICE,
    "normal": LifecycleState.IN_SERVICE,
    "available": LifecycleState.IN_SERVICE,
    "active": LifecycleState.IN_SERVICE,
    "planned": LifecycleState.PLANNED,
    "approved": LifecycleState.PLANNED,
    "under_maintenance": LifecycleState.UNDER_MAINTENANCE,
    "maintenance": LifecycleState.UNDER_MAINTENANCE,
    "fault_pending_repair": LifecycleState.UNDER_MAINTENANCE,
    "decommissioned": LifecycleState.DECOMMISSIONED,
    "expired": LifecycleState.DECOMMISSIONED,
    "cancelled": LifecycleState.DECOMMISSIONED,
}


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware; naive datetimes make as_of ambiguous")
    return value


class TwinObject(StrictModel):
    twin_id: str = Field(min_length=1)
    kind: ObjectKind
    name: str = Field(min_length=1)
    region_id: str | None = None
    geometry_ref: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.UNKNOWN
    state_as_of: datetime
    asset_ids: list[str] = Field(min_length=1)
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("state_as_of")
    @classmethod
    def aware_state_as_of(cls, value: datetime) -> datetime:
        return _aware(value, "state_as_of")


class SemanticTriple(StrictModel):
    triple_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    predicate: Predicate
    object_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: datetime
    valid_until: datetime | None = None
    knowledge_version: str = Field(pattern=r"^kb-v\d+\.\d+\.\d+$")
    evidence_refs: list[str] = Field(min_length=1)
    review_status: ReviewStatus = ReviewStatus.CONFIRMED

    @field_validator("valid_from")
    @classmethod
    def aware_valid_from(cls, value: datetime) -> datetime:
        return _aware(value, "valid_from")

    @field_validator("evidence_refs")
    @classmethod
    def refs_look_locatable(cls, values: list[str]) -> list[str]:
        for ref in values:
            if "#" not in ref:
                raise ValueError(f"evidence ref must be asset_id#locator, got {ref!r}")
        return values

    @model_validator(mode="after")
    def window_is_ordered(self) -> "SemanticTriple":
        if self.valid_until is not None:
            _aware(self.valid_until, "valid_until")
            if self.valid_until <= self.valid_from:
                raise ValueError("valid_until must be after valid_from")
        return self

    def active_at(self, as_of: datetime) -> bool:
        if as_of < self.valid_from:
            return False
        return self.valid_until is None or as_of < self.valid_until

    def involves(self, twin_id: str) -> bool:
        return twin_id in (self.subject_id, self.object_id)

    def other_end(self, twin_id: str) -> str:
        return self.object_id if self.subject_id == twin_id else self.subject_id

    @property
    def evidence_asset_ids(self) -> list[str]:
        return sorted({ref.split("#", 1)[0] for ref in self.evidence_refs})


class SemanticQuery(StrictModel):
    query_id: str = Field(min_length=1)
    as_of: datetime
    seed_ids: list[str] = Field(default_factory=list)
    kind_filter: list[ObjectKind] = Field(default_factory=list)
    predicates: list[Predicate] = Field(default_factory=list)
    max_hops: int = Field(default=2, ge=1, le=6)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    caller_labels: list[str] = Field(default_factory=list)
    include_candidates: bool = False
    # 区域是归属标签，不是传导介质：允许穿越会让"影响范围"在一跳内退化为
    # 全片区清单（实测 2 跳从 3 条涨到 31 条，且全是 located_in）。
    block_expansion_through: list[ObjectKind] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        return _aware(value, "as_of")

    @classmethod
    def impact(
        cls,
        *,
        query_id: str,
        as_of: datetime,
        seed_ids: Sequence[str],
        max_hops: int = 3,
        min_confidence: float = 0.6,
        caller_labels: Sequence[str] = (),
    ) -> "SemanticQuery":
        """影响分析预设：不穿越片区节点，只沿真实传导关系扩散。"""
        return cls(
            query_id=query_id,
            as_of=as_of,
            seed_ids=list(seed_ids),
            max_hops=max_hops,
            min_confidence=min_confidence,
            caller_labels=list(caller_labels),
            block_expansion_through=[ObjectKind.REGION],
        )


class _Index:
    """Adjacency kept as plain dicts; rebuilt only on construction."""

    __slots__ = ("by_node",)

    def __init__(self, triples: Sequence[SemanticTriple]) -> None:
        self.by_node: dict[str, list[int]] = {}
        for position, triple in enumerate(triples):
            self.by_node.setdefault(triple.subject_id, []).append(position)
            self.by_node.setdefault(triple.object_id, []).append(position)


def _permissive(visibility: Sequence[str] | None) -> bool:
    """No labels means public: the dataset leaves most assets unlabelled."""
    return not visibility


class TwinSemanticStore:
    def __init__(
        self,
        objects: Iterable[TwinObject],
        triples: Iterable[SemanticTriple],
        *,
        snapshot_version: str = "kb-v1.0.0",
        asset_visibility: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        KnowledgeVersion(value=snapshot_version)
        self._objects: dict[str, TwinObject] = {}
        for item in objects:
            if item.twin_id in self._objects:
                raise ValueError(f"duplicate twin_id: {item.twin_id}")
            self._objects[item.twin_id] = item
        # deterministic order: id first, so the hash and the output never depend
        # on the order the caller happened to build the inputs in
        self._triples: list[SemanticTriple] = sorted(triples, key=lambda item: item.triple_id)
        self._index = _Index(self._triples)
        self._version = snapshot_version
        self._visibility: dict[str, frozenset[str]] = {
            asset_id: frozenset(labels) for asset_id, labels in (asset_visibility or {}).items()
        }

    # --- read ------------------------------------------------------------
    @property
    def snapshot_version(self) -> str:
        return self._version

    def objects(self) -> list[TwinObject]:
        return [self._objects[key] for key in sorted(self._objects)]

    def triples(self) -> list[SemanticTriple]:
        return list(self._triples)

    def get_object(self, twin_id: str) -> TwinObject:
        try:
            return self._objects[twin_id]
        except KeyError as exc:
            raise KeyError(f"unknown twin_id: {twin_id}") from exc

    def snapshot_hash(self) -> str:
        material = json.dumps(
            {
                "version": self._version,
                "objects": [self._objects[key].model_dump(mode="json") for key in sorted(self._objects)],
                "triples": [item.model_dump(mode="json") for item in self._triples],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # --- filters (order matters: permission first) -----------------------
    def _visible(self, triple: SemanticTriple, caller_labels: Sequence[str]) -> bool:
        labels = set(caller_labels)
        for asset_id in triple.evidence_asset_ids:
            visibility = self._visibility.get(asset_id)
            if _permissive(visibility) or (visibility & labels):
                return True
        return False

    def _status_ok(self, triple: SemanticTriple, include_candidates: bool) -> bool:
        if triple.review_status is ReviewStatus.CONFIRMED:
            return True
        if triple.review_status is ReviewStatus.DEPRECATED:
            return False  # 过期来源永不参与，即使显式要求
        return include_candidates

    def _selectable(self, triple: SemanticTriple, query: SemanticQuery) -> bool:
        return (
            triple.active_at(query.as_of)
            and self._visible(triple, query.caller_labels)
            and self._status_ok(triple, query.include_candidates)
            and triple.confidence >= query.min_confidence
            and (not query.predicates or triple.predicate in query.predicates)
        )

    def _kind_ok(self, triple: SemanticTriple, kinds: Sequence[ObjectKind]) -> bool:
        if not kinds:
            return True
        allowed = set(kinds)
        subject = self._objects.get(triple.subject_id)
        obj = self._objects.get(triple.object_id)
        return (subject is not None and subject.kind in allowed) or (
            obj is not None and obj.kind in allowed
        )

    # --- traversal -------------------------------------------------------
    def neighbors(self, seed_ids: Sequence[str], *, query: SemanticQuery) -> list[SemanticTriple]:
        """BFS over both edge directions; returns triples in id order with span <= max_hops."""
        if not seed_ids:
            return []
        blocked = set(query.block_expansion_through)
        seen_nodes: set[str] = set(seed_ids)
        frontier: set[str] = set(seed_ids)
        reached: dict[int, int] = {}
        for hop in range(1, query.max_hops + 1):
            next_frontier: set[str] = set()
            for node in sorted(frontier):
                for position in self._index.by_node.get(node, ()):
                    triple = self._triples[position]
                    if not self._selectable(triple, query):
                        continue
                    reached.setdefault(position, hop)
                    other = triple.other_end(node)
                    if other in seen_nodes:
                        continue
                    target = self._objects.get(other)
                    if target is not None and target.kind in blocked:
                        seen_nodes.add(other)  # 记录可见，但不作为下一跳起点
                        continue
                    next_frontier.add(other)
            seen_nodes |= next_frontier
            frontier = next_frontier
            if not frontier:
                break
        selected = [self._triples[position] for position in sorted(reached)]
        return [triple for triple in selected if self._kind_ok(triple, query.kind_filter)]

    def query(self, query: SemanticQuery) -> ToolResult:
        """Return triples and the objects they touch, reachable from `seed_ids`."""
        data: dict[str, Any] = {
            "query_id": query.query_id,
            "as_of": query.as_of.isoformat(),
            "knowledge_version": self._version,
            "snapshot_hash": self.snapshot_hash(),
            "seeds": list(query.seed_ids),
        }
        missing = [seed for seed in query.seed_ids if seed not in self._objects]
        if missing:
            data["missing_seeds"] = missing
            return ToolResult(
                status="error",
                data=data,
                error=ToolError(
                    code=ErrorCode.NOT_FOUND.value,
                    message=f"unknown seeds: {missing}",
                    retryable=False,
                ),
                stats=ToolStats(attempt=1, latency_ms=0),
                context=ToolContext(source="semantic", version=self._version),
            )

        if not query.seed_ids:
            data["reason"] = EmptyReason.NO_NEIGHBOR.value
            data["detail"] = "query requires seed_ids; 全表扫描不在此接口的职责内"
            data.update({"triples": [], "objects": [], "triple_count": 0})
            return ToolResult(
                status="partial",
                data=data,
                error=None,
                stats=ToolStats(attempt=1, latency_ms=0),
                context=ToolContext(source="semantic", version=self._version),
            )

        triples = self.neighbors(query.seed_ids, query=query)
        touched = sorted({t.subject_id for t in triples} | {t.object_id for t in triples})
        data["triples"] = [triple.model_dump(mode="json") for triple in triples]
        data["objects"] = [self._objects[key].model_dump(mode="json") for key in touched if key in self._objects]
        data["triple_count"] = len(triples)

        if triples:
            return ToolResult(
                status="ok",
                data=data,
                error=None,
                stats=ToolStats(attempt=1, latency_ms=0),
                context=ToolContext(source="semantic", version=self._version),
            )

        reason = self._empty_reason(query)
        data["reason"] = reason.value
        data["detail"] = EMPTY_REASON_TEXT[reason]
        return ToolResult(
            status="partial",
            data=data,
            error=None,
            stats=ToolStats(attempt=1, latency_ms=0),
            context=ToolContext(source="semantic", version=self._version),
        )

    def _empty_reason(self, query: SemanticQuery) -> EmptyReason:
        """Explain why nothing matched, applying the filters in their real order."""
        def reachable(predicate_fn) -> bool:
            # 只关心"和种子有关系"的候选，避免把无关关系的属性当原因
            for triple in self._triples:
                if not any(triple.involves(seed) for seed in query.seed_ids):
                    continue
                if predicate_fn(triple):
                    return True
            return False

        if not reachable(lambda t: True):
            return EmptyReason.NO_NEIGHBOR  # 种子在图上完全孤立
        if not reachable(lambda t: t.active_at(query.as_of)):
            return EmptyReason.OUT_OF_WINDOW
        if not reachable(lambda t: self._visible(t, query.caller_labels)):
            return EmptyReason.PERMISSION_FILTERED
        if not reachable(lambda t: self._status_ok(t, query.include_candidates)):
            return EmptyReason.REVIEW_PENDING
        if not reachable(lambda t: t.confidence >= query.min_confidence):
            return EmptyReason.BELOW_CONFIDENCE
        return EmptyReason.NO_NEIGHBOR


EMPTY_REASON_TEXT: dict[EmptyReason, str] = {
    EmptyReason.OUT_OF_WINDOW: "种子在该时点没有有效期内的关系",
    EmptyReason.PERMISSION_FILTERED: "命中的关系对当前调用方不可见",
    EmptyReason.REVIEW_PENDING: "命中的关系尚未人工确认，或来源已废止",
    EmptyReason.BELOW_CONFIDENCE: "命中关系的置信度低于阈值",
    EmptyReason.NO_NEIGHBOR: "种子在跳数范围内没有符合条件的关系",
}


# --- assembly ------------------------------------------------------------


def _lifecycle_for(kind: ObjectKind, attributes: Mapping[str, Any]) -> LifecycleState:
    raw = attributes.get("state") or attributes.get("status")
    if isinstance(raw, str):
        mapped = LIFECYCLE_BY_STATE.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    if kind in {ObjectKind.DEPARTMENT, ObjectKind.RESOURCE, ObjectKind.REGION}:
        return LifecycleState.IN_SERVICE
    return LifecycleState.UNKNOWN


def _row_window(entity: EntityCandidate, record) -> tuple[datetime, datetime | None]:
    """Validity is per row when the table says so, per asset otherwise.

    A weather alert table carries `valid_from`/`valid_to` per row: using the
    asset-level window would make every alert permanent (the asset has no
    `valid_to`), which is exactly the "stale fact used as live fact" failure the
    temporal layer exists to prevent.
    """
    start_raw = entity.attributes.get("valid_from")
    end_raw = entity.attributes.get("valid_to")
    if isinstance(start_raw, str):
        try:
            start = _aware(datetime.fromisoformat(start_raw), "valid_from")
            end = None
            if isinstance(end_raw, str):
                end = _aware(datetime.fromisoformat(end_raw), "valid_to")
            return start, end
        except ValueError:
            pass
    if record is not None:
        return record.valid_from, record.valid_to
    return DEFAULT_VALID_FROM, None


def _observed_at(entity: EntityCandidate, fallback: datetime) -> datetime:
    for key in ("reported_at", "issued_at", "last_inspection", "start_at"):
        raw = entity.attributes.get(key)
        if isinstance(raw, str):
            try:
                return _aware(datetime.fromisoformat(raw), key)
            except ValueError:
                continue
    return fallback


def _scalar_attributes(entity: EntityCandidate) -> dict[str, str]:
    """Flatten to str/str: the twin is for traversal, not for arithmetic."""
    result: dict[str, str] = {}
    for key, value in entity.attributes.items():
        if isinstance(value, (list, tuple)):
            result[key] = ",".join(str(item) for item in value)
        elif isinstance(value, dict):
            continue
        elif value is None:
            continue
        else:
            result[key] = str(value)
    return result


def kind_for_entity(entity: EntityCandidate) -> ObjectKind:
    if entity.type == "Pipeline":
        raw = str(entity.attributes.get("kind") or entity.attributes.get("pipeline_type") or "").lower()
        return PIPELINE_KIND_MAP.get(raw, ObjectKind.DRAINAGE)
    if entity.type == "DrainageFacility":
        if str(entity.attributes.get("facility_type") or "").lower() == "pump_station":
            return ObjectKind.PUMP_STATION
        return ObjectKind.DRAINAGE
    if entity.type not in ENTITY_KIND_MAP:
        raise ValueError(f"unmapped entity type: {entity.type}")
    return ENTITY_KIND_MAP[entity.type]


def twin_id_for(entity: EntityCandidate) -> str:
    """`EN-<Type>-<key>` maps to `TW-<kind>-<key>`; the key part is preserved."""
    suffix = entity.candidate_id.split("-", 2)[-1]
    return f"TW-{kind_for_entity(entity).value}-{suffix}"


def _review_status_for(decision: ReviewDecision, deprecated: bool) -> ReviewStatus:
    if deprecated:
        return ReviewStatus.DEPRECATED
    return ReviewStatus.CONFIRMED if decision is ReviewDecision.AUTO_ACCEPT else ReviewStatus.CANDIDATE


def build_semantic_store(
    bundle: OntologyBundle,
    *,
    governance: Mapping[str, Any] | None = None,
    knowledge_version: str = "kb-v1.0.0",
) -> TwinSemanticStore:
    """Assemble twins and triples from an extracted bundle plus asset governance.

    `governance` maps `asset_id -> AssetGovernance` and supplies the three things
    the extractor cannot know: validity window, visibility, deprecation status.
    """
    KnowledgeVersion(value=knowledge_version)
    gov = governance or {}

    id_by_name: dict[str, str] = {}
    for entity in bundle.entities:
        id_by_name.setdefault(entity.name, entity.candidate_id)
    entity_by_id = {entity.candidate_id: entity for entity in bundle.entities}
    twin_by_entity = {
        candidate_id: twin_id_for(entity) for candidate_id, entity in entity_by_id.items()
    }

    windows: dict[str, tuple[datetime, datetime | None]] = {}
    objects: list[TwinObject] = []
    for candidate_id in sorted(entity_by_id):
        entity = entity_by_id[candidate_id]
        asset_ids = sorted({item.asset_id for item in entity.evidence})
        record = gov.get(asset_ids[0]) if asset_ids else None
        deprecated = record is not None and record.status is AssetStatus.DEPRECATED
        validity_from, validity_until = _row_window(entity, record)
        windows[candidate_id] = (validity_from, validity_until)
        attributes = _scalar_attributes(entity)
        objects.append(
            TwinObject(
                twin_id=twin_by_entity[candidate_id],
                kind=kind_for_entity(entity),
                name=entity.name,
                # 只有行政片区进 region_id；排水分区（DZ-01）是另一套空间体系，
                # 混进来会让"按片区查设施"把整个 DZ 当成片区。
                region_id=attributes.get("district"),
                geometry_ref=None,
                lifecycle_state=_lifecycle_for(kind_for_entity(entity), entity.attributes),
                state_as_of=_observed_at(entity, validity_from),
                asset_ids=asset_ids,
                attributes=attributes,
            )
        )

    triples: list[SemanticTriple] = []
    for relation in bundle.relations:
        predicate = PREDICATE_MAP.get(relation.predicate)
        if predicate is None:
            continue
        subject = id_by_name.get(relation.subject)
        object_ = id_by_name.get(relation.object)
        if subject is None or object_ is None:
            continue
        subject_id = twin_by_entity[subject]
        object_id = twin_by_entity[object_]
        subject_key = subject  # 证据来自原始主语所在的行
        if predicate in REVERSED_PREDICATES:
            subject_id, object_id = object_id, subject_id

        refs = _evidence_refs(relation)
        if not refs:
            continue
        primary = refs[0].split("#", 1)[0]
        record = gov.get(primary)
        deprecated = record is not None and record.status is AssetStatus.DEPRECATED
        # 关系跟随主语行的有效期：行级窗口优先，缺失才退回资产治理窗口
        valid_from, valid_until = windows.get(subject_key, (DEFAULT_VALID_FROM, None))
        triples.append(
            SemanticTriple(
                triple_id=_triple_id(relation),
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                confidence=relation.confidence,
                valid_from=valid_from,
                valid_until=valid_until,
                knowledge_version=knowledge_version,
                evidence_refs=refs,
                review_status=_review_status_for(relation.decision, deprecated),
            )
        )

    return TwinSemanticStore(
        objects,
        triples,
        snapshot_version=knowledge_version,
        asset_visibility={
            asset_id: tuple(getattr(record, "visibility", ()) or ()) for asset_id, record in gov.items()
        },
    )


def _evidence_refs(relation: RelationCandidate) -> list[str]:
    return sorted({f"{item.asset_id}#{item.locator}" for item in relation.evidence})


def _triple_id(relation: RelationCandidate) -> str:
    candidate_id = relation.candidate_id
    if candidate_id.startswith("RL-"):
        return "TR-" + candidate_id[3:]
    return f"TR-{candidate_id}"


def build_urban_semantic_store(
    dataset_root: str | None = None, *, knowledge_version: str = "kb-v1.0.0"
) -> TwinSemanticStore:
    """Convenience wiring for the demo dataset: assets -> bundle -> twins."""
    from .city_extract import build_city_bundle
    from .urban_dataset import load_urban_assets

    rows = load_urban_assets(dataset_root)
    governance = {asset.asset_id: record for asset, record in rows}
    bundle = build_city_bundle(dataset_root)
    return build_semantic_store(bundle, governance=governance, knowledge_version=knowledge_version)


__all__ = [
    "EMPTY_REASON_TEXT",
    "EmptyReason",
    "LifecycleState",
    "ObjectKind",
    "Predicate",
    "ReviewStatus",
    "SemanticQuery",
    "SemanticTriple",
    "TwinObject",
    "TwinSemanticStore",
    "build_semantic_store",
    "build_urban_semantic_store",
    "twin_id_for",
]
