"""P4 semantic layer: temporal window, review gate, permission-first, hash stability.

The tests split into two groups:

- synthetic micro-graphs: one rule per test, so a failure names the rule;
- the real 15-asset dataset: proves the assembly is not just self-consistent with
  hand-built fixtures.

Numbers asserted against the dataset (62 objects / 100 triples) will move when
the dataset changes -- that is intended. The behavioural assertions (职责空白、
过期不参与、片区不传导) must keep holding regardless.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from decision_poc.semantic import (
    EmptyReason,
    LifecycleState,
    ObjectKind,
    Predicate,
    ReviewStatus,
    SemanticQuery,
    SemanticTriple,
    TwinObject,
    TwinSemanticStore,
    build_urban_semantic_store,
)

AS_OF = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
T2 = datetime(2027, 1, 1, tzinfo=timezone.utc)


def _obj(twin_id: str, kind: ObjectKind, *, asset: str = "AS-a") -> TwinObject:
    return TwinObject(
        twin_id=twin_id,
        kind=kind,
        name=twin_id,
        lifecycle_state=LifecycleState.IN_SERVICE,
        state_as_of=AS_OF,
        asset_ids=[asset],
        attributes={},
    )


def _triple(
    triple_id: str,
    subject: str,
    predicate: Predicate,
    object_: str,
    *,
    asset: str = "AS-a",
    locator: str = "/row:2",
    confidence: float = 0.9,
    valid_from: datetime = T0,
    valid_until: datetime | None = None,
    review_status: str | ReviewStatus = ReviewStatus.CONFIRMED,
) -> SemanticTriple:
    return SemanticTriple(
        triple_id=triple_id,
        subject_id=subject,
        predicate=predicate,
        object_id=object_,
        confidence=confidence,
        valid_from=valid_from,
        valid_until=valid_until,
        knowledge_version="kb-v1.0.0",
        evidence_refs=[f"{asset}#{locator}"],
        review_status=review_status,
    )


def _store(triples, *, objects=None, visibility=None) -> TwinSemanticStore:
    return TwinSemanticStore(
        objects or _objects(),
        triples,
        snapshot_version="kb-v1.0.0",
        asset_visibility=visibility or {},
    )


def _objects() -> list[TwinObject]:
    return [
        _obj("TW-road-r101", ObjectKind.ROAD, asset="AS-road"),
        _obj("TW-drainage-df01", ObjectKind.DRAINAGE, asset="AS-drain"),
        _obj("TW-region-a03", ObjectKind.REGION, asset="AS-road"),
        _obj("TW-incident-w1", ObjectKind.INCIDENT, asset="AS-order"),
    ]


def _q(**overrides) -> SemanticQuery:
    payload = {"query_id": "qry", "as_of": AS_OF, "seed_ids": ["TW-drainage-df01"], "max_hops": 2}
    payload.update(overrides)
    return SemanticQuery(**payload)


# --- temporal window -----------------------------------------------------


def test_window_is_half_open() -> None:
    triple = _triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03", valid_until=T1)
    assert triple.active_at(T0), "起点必须包含"
    assert not triple.active_at(T1), "终点必须排除，否则两个版本会同时有效"
    assert triple.active_at(datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc))


def test_expired_triple_is_not_returned_and_reason_is_out_of_window() -> None:
    store = _store(
        [
            _triple(
                "TR-old",
                "TW-drainage-df01",
                Predicate.LOCATED_IN,
                "TW-region-a03",
                valid_until=T1,
            )
        ]
    )
    result = store.query(_q())
    assert result.status == "partial"
    assert result.data["reason"] == EmptyReason.OUT_OF_WINDOW.value


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError):
        SemanticQuery(query_id="q", as_of=datetime(2026, 10, 29, 15, 0))


def test_valid_until_before_valid_from_is_rejected() -> None:
    with pytest.raises(ValueError):
        _triple(
            "TR-bad",
            "TW-drainage-df01",
            Predicate.LOCATED_IN,
            "TW-region-a03",
            valid_from=T2,
            valid_until=T1,
        )


# --- review gate ---------------------------------------------------------


def test_candidate_hidden_unless_requested() -> None:
    triples = [
        _triple("TR-ok", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03"),
        _triple(
            "TR-cand",
            "TW-drainage-df01",
            Predicate.MAINTAINED_BY,
            "TW-region-a03",
            review_status=ReviewStatus.CANDIDATE,
        ),
    ]
    store = _store(triples)
    assert [t.triple_id for t in store.neighbors(["TW-drainage-df01"], query=_q())] == ["TR-ok"]
    with_candidates = store.neighbors(["TW-drainage-df01"], query=_q(include_candidates=True))
    assert [t.triple_id for t in with_candidates] == ["TR-cand", "TR-ok"]


def test_deprecated_never_returned_even_when_requested() -> None:
    store = _store(
        [
            _triple(
                "TR-dep",
                "TW-drainage-df01",
                Predicate.LOCATED_IN,
                "TW-region-a03",
                review_status=ReviewStatus.DEPRECATED,
            )
        ]
    )
    result = store.query(_q(include_candidates=True))
    assert result.status == "partial"
    assert result.data["reason"] == EmptyReason.REVIEW_PENDING.value


def test_candidate_does_not_participate_in_reason_ranking() -> None:
    """只有 candidate 时，原因必须是 REVIEW_PENDING 而不是 NO_NEIGHBOR。"""
    store = _store(
        [
            _triple(
                "TR-cand",
                "TW-drainage-df01",
                Predicate.LOCATED_IN,
                "TW-region-a03",
                review_status=ReviewStatus.CANDIDATE,
            )
        ]
    )
    assert store.query(_q()).data["reason"] == EmptyReason.REVIEW_PENDING.value


# --- permission ----------------------------------------------------------


def test_permission_is_checked_before_confidence() -> None:
    """高置信度的受限关系不得因排名靠前而泄漏，且原因必须指向权限。"""
    store = _store(
        [
            _triple(
                "TR-secret",
                "TW-drainage-df01",
                Predicate.MAINTAINED_BY,
                "TW-region-a03",
                asset="AS-secret",
                confidence=0.99,
            )
        ],
        visibility={"AS-secret": ["ORG-GAS"]},
    )
    result = store.query(_q(caller_labels=["ORG-ROAD"], min_confidence=0.6))
    assert result.status == "partial"
    assert result.data["reason"] == EmptyReason.PERMISSION_FILTERED.value

    allowed = store.query(_q(query_id="qry-2", caller_labels=["ORG-GAS"]))
    assert allowed.status == "ok"
    assert [row["triple_id"] for row in allowed.data["triples"]] == ["TR-secret"]


def test_unlabelled_asset_is_public() -> None:
    store = _store([_triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03")])
    assert store.query(_q(caller_labels=[])).status == "ok"


def test_multi_asset_triple_is_visible_if_any_source_is_public() -> None:
    triple = SemanticTriple(
        triple_id="TR-mixed",
        subject_id="TW-drainage-df01",
        predicate=Predicate.LOCATED_IN,
        object_id="TW-region-a03",
        confidence=0.8,
        valid_from=T0,
        knowledge_version="kb-v1.0.0",
        evidence_refs=["AS-secret#/row:1", "AS-public#/row:2"],
        review_status=ReviewStatus.CONFIRMED,
    )
    store = _store([triple], visibility={"AS-secret": ["ORG-GAS"]})
    assert store.query(_q(caller_labels=["ORG-ROAD"])).status == "ok"


# --- traversal -----------------------------------------------------------


def test_hop_limit_is_honoured() -> None:
    store = _store(
        [
            _triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03"),
            _triple("TR-2", "TW-road-r101", Predicate.DEPENDS_ON, "TW-drainage-df01"),
        ]
    )
    assert len(store.neighbors(["TW-drainage-df01"], query=_q(max_hops=1))) == 2
    assert len(store.neighbors(["TW-road-r101"], query=_q(max_hops=1))) == 1
    assert len(store.neighbors(["TW-road-r101"], query=_q(max_hops=2))) == 2


def test_traversal_follows_edges_in_both_directions() -> None:
    store = _store([_triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03")])
    from_object = store.neighbors(["TW-region-a03"], query=_q(seed_ids=["TW-region-a03"]))
    assert [t.triple_id for t in from_object] == ["TR-1"]


def test_blocked_kind_is_reported_but_not_expanded() -> None:
    """片区节点仍可出现在结果里，但不作为下一跳起点。"""
    triples = [
        _triple("TR-1", "TW-incident-w1", Predicate.AFFECTS, "TW-road-r101"),
        _triple("TR-2", "TW-road-r101", Predicate.LOCATED_IN, "TW-region-a03"),
        _triple("TR-3", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03"),
    ]
    store = _store(triples)
    free = store.neighbors(["TW-incident-w1"], query=_q(seed_ids=["TW-incident-w1"], max_hops=3))
    assert {t.triple_id for t in free} == {"TR-1", "TR-2", "TR-3"}, "不限制时片区会扩散到同区所有对象"

    blocked = store.neighbors(
        ["TW-incident-w1"],
        query=_q(
            seed_ids=["TW-incident-w1"],
            max_hops=3,
            block_expansion_through=[ObjectKind.REGION],
        ),
    )
    assert {t.triple_id for t in blocked} == {"TR-1", "TR-2"}, "TR-3 只能经片区到达，必须被拦住"


def test_region_seed_still_works_when_traversal_is_blocked() -> None:
    triples = [
        _triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03"),
    ]
    store = _store(triples)
    query = SemanticQuery.impact(
        query_id="impact", as_of=AS_OF, seed_ids=["TW-region-a03"], max_hops=2
    )
    assert [t.triple_id for t in store.neighbors(["TW-region-a03"], query=query)] == ["TR-1"]


# --- errors and hashes ---------------------------------------------------


def test_unknown_seed_is_error_not_partial() -> None:
    store = _store([_triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03")])
    result = store.query(_q(seed_ids=["TW-missing"]))
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "NOT_FOUND"
    assert result.error.retryable is False
    assert result.data["missing_seeds"] == ["TW-missing"]


def test_isolated_seed_reports_no_neighbor() -> None:
    store = _store([_triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03")])
    result = store.query(_q(seed_ids=["TW-road-r101"]))
    assert result.status == "partial"
    assert result.data["reason"] == EmptyReason.NO_NEIGHBOR.value


def test_query_requires_seeds() -> None:
    store = _store([_triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03")])
    result = store.query(_q(seed_ids=[]))
    assert result.status == "partial"
    assert "seed_ids" in result.data["detail"]


def test_snapshot_hash_is_order_independent_and_stable() -> None:
    triples = [
        _triple("TR-2", "TW-road-r101", Predicate.DEPENDS_ON, "TW-drainage-df01"),
        _triple("TR-1", "TW-drainage-df01", Predicate.LOCATED_IN, "TW-region-a03"),
    ]
    forward = _store(triples)
    backward = _store(list(reversed(triples)))
    assert forward.snapshot_hash() == backward.snapshot_hash()
    assert forward.query(_q()).data["snapshot_hash"] == forward.snapshot_hash()


def test_duplicate_twin_id_is_rejected() -> None:
    twin = _obj("TW-road-r101", ObjectKind.ROAD)
    with pytest.raises(ValueError):
        TwinSemanticStore([twin, twin], [], snapshot_version="kb-v1.0.0")


def test_triple_requires_locatable_evidence() -> None:
    with pytest.raises(ValueError):
        SemanticTriple(
            triple_id="TR-bad",
            subject_id="a",
            predicate=Predicate.LOCATED_IN,
            object_id="b",
            confidence=0.9,
            valid_from=T0,
            knowledge_version="kb-v1.0.0",
            evidence_refs=["AS-a"],  # 缺少 #locator
        )


# --- assembly over the real dataset --------------------------------------


@pytest.fixture(scope="module")
def urban_store() -> TwinSemanticStore:
    return build_urban_semantic_store()


def test_all_extraction_predicates_survive_mapping(urban_store: TwinSemanticStore) -> None:
    from decision_poc.city_extract import build_city_bundle

    raw = {relation.predicate for relation in build_city_bundle().relations}
    mapped = {triple.predicate.value for triple in urban_store.triples()}
    assert {
        "responsible_for",
        "located_in",
        "connected_to",
        "affected_by",
        "covered_by_plan",
        "conflicts_with",
        "under_construction",
        "precedes",
    } <= raw
    assert {"maintained_by", "located_in", "depends_on", "affects", "regulated_by", "conflicts_with", "under_construction", "precedes"} <= mapped


def test_responsibility_gap_is_visible_in_the_graph(urban_store: TwinSemanticStore) -> None:
    """DF-PS-01 在 P2 是职责空白；语义层必须保持空白，不许被邻居补齐。"""
    pump = next(o for o in urban_store.objects() if o.name == "DF-PS-01")
    incoming = [
        triple
        for triple in urban_store.triples()
        if triple.predicate is Predicate.MAINTAINED_BY and triple.object_id == pump.twin_id
    ]
    assert incoming == []
    assert pump.kind is ObjectKind.PUMP_STATION
    assert pump.lifecycle_state is LifecycleState.UNDER_MAINTENANCE


def test_deprecated_plan_is_excluded_from_queries(urban_store: TwinSemanticStore) -> None:
    deprecated = [t for t in urban_store.triples() if t.review_status is ReviewStatus.DEPRECATED]
    assert deprecated, "数据集里有一份废止预案，语义层必须把它标成 deprecated"
    plan = next(o for o in urban_store.objects() if "已废止" in o.name)
    query = SemanticQuery(
        query_id="dep",
        as_of=datetime.fromisoformat("2026-10-29T15:00:00+08:00"),
        seed_ids=[plan.twin_id],
        max_hops=2,
        caller_labels=["ORG-DRAIN", "ORG-EMERG"],
        include_candidates=True,
    )
    assert urban_store.query(query).data["triple_count"] == 0


def test_weather_expires_after_alert_window(urban_store: TwinSemanticStore) -> None:
    """气象有效期是行级的：表级窗口为 None，若按资产算会永远有效。"""
    weather = next(o for o in urban_store.objects() if o.name == "WA-20261029-01")
    inside = datetime.fromisoformat("2026-10-29T12:00:00+08:00")   # 行内 08:00-14:00
    after = datetime.fromisoformat("2026-10-29T15:00:00+08:00")    # 已过期
    query = SemanticQuery.impact(
        query_id="wx", as_of=inside, seed_ids=[weather.twin_id], max_hops=2
    )
    assert urban_store.query(query).data["triple_count"] > 0
    expired = urban_store.query(query.model_copy(update={"query_id": "wx2", "as_of": after}))
    assert expired.data["triple_count"] == 0
    assert expired.data["reason"] == EmptyReason.OUT_OF_WINDOW.value


def test_impact_preset_cuts_region_noise(urban_store: TwinSemanticStore) -> None:
    incident = next(o for o in urban_store.objects() if o.name == "WO-20261029-007")
    as_of = datetime.fromisoformat("2026-10-29T12:00:00+08:00")
    plain = urban_store.query(
        SemanticQuery(
            query_id="plain",
            as_of=as_of,
            seed_ids=[incident.twin_id],
            max_hops=2,
            caller_labels=["ORG-EMERG"],
        )
    )
    impact = urban_store.query(
        SemanticQuery.impact(
            query_id="impact", as_of=as_of, seed_ids=[incident.twin_id], max_hops=2, caller_labels=["ORG-EMERG"]
        )
    )
    assert impact.data["triple_count"] < plain.data["triple_count"]
    reached = {row["object_id"] for row in impact.data["triples"]} | {
        row["subject_id"] for row in impact.data["triples"]
    }
    region = next(o for o in urban_store.objects() if o.kind is ObjectKind.REGION and o.name == "A-03")
    assert region.twin_id in reached, "片区本身仍应出现在影响范围里"
    assert len(reached) < len(
        {row["object_id"] for row in plain.data["triples"]} | {row["subject_id"] for row in plain.data["triples"]}
    ), "不穿越片区应显著收缩可达集合"


def test_restricted_assets_never_leak(urban_store: TwinSemanticStore) -> None:
    """标记了 visibility 的规程文件今天没有产出关系，因此不会泄漏。

    这条不是空转：一旦 P6 把规程条款抽成 DeductionRule 关系（source_ref 指向
    规程），这里会立刻对"新出现的关系"执行权限断言，挡住未加过滤的实现。
    """
    from decision_poc.urban_dataset import load_urban_assets

    restricted = {
        asset.asset_id: set(record.visibility)
        for asset, record in load_urban_assets()
        if record.visibility
    }
    assert restricted, "数据集必须至少有一个受限资产，否则这条测试没有意义"

    as_of = datetime.fromisoformat("2026-10-29T12:00:00+08:00")
    leaked = []
    for triple in urban_store.triples():
        if not set(triple.evidence_asset_ids) & set(restricted):
            continue
        result = urban_store.query(
            SemanticQuery(
                query_id="leak",
                as_of=as_of,
                seed_ids=[triple.subject_id],
                max_hops=1,
                caller_labels=["ORG-ROAD"],  # 不在任何规程的可见范围内
            )
        )
        returned = {row["triple_id"] for row in result.data["triples"]}
        if triple.triple_id in returned:
            leaked.append(triple.triple_id)
    assert leaked == [], f"受限资产的关系泄漏给越权调用方: {leaked}"
