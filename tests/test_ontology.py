"""Tests for the semi-automatic ontology bootstrap (all offline)."""

import json
from pathlib import Path

import pytest

from decision_poc import ontology
from decision_poc.asset_manifest import build_catalog_manifest
from decision_poc.ontology import (
    SEED_ONTOLOGY,
    OntologySchema,
    RelationSchema,
    ReviewDecision,
    build_bundle,
    bundle_payload,
    bundle_stats,
    decide,
    merge_summary,
    normalize_name,
    parse_range,
    plan_merge,
    rule_matches,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    return build_bundle(build_catalog_manifest(ROOT), ROOT)


def _entities(bundle, type_name: str) -> list:
    return [item for item in bundle.entities if item.type == type_name]


def test_seed_schema_is_self_consistent():
    assert SEED_ONTOLOGY.type_names() >= {
        "Equipment",
        "Metric",
        "FaultMode",
        "ThresholdRule",
        "StandardClause",
    }
    for relation in SEED_ONTOLOGY.relations:
        assert set(relation.domain) <= SEED_ONTOLOGY.type_names()
        assert set(relation.range) <= SEED_ONTOLOGY.type_names()


def test_schema_rejects_unknown_endpoint_type():
    with pytest.raises(ValueError):
        OntologySchema(
            version="t",
            types=[ontology.OntologyType(name="A", description="a")],
            relations=[
                RelationSchema(
                    name="r", domain=["A"], range=["Missing"], description="bad"
                )
            ],
        )


def test_ledger_yields_equipment_line_and_class(bundle):
    equipment = _entities(bundle, "Equipment")
    assert any(item.name == "主传动减速机" for item in equipment)
    target = next(item for item in equipment if item.name == "主传动减速机")
    assert target.attributes["equipment_id"] == "A-101"
    assert target.attributes["rated_power_kw"] == 75.0
    assert target.attributes["criticality"] == "A"

    predicates = {(rel.subject, rel.predicate, rel.object) for rel in bundle.relations}
    assert ("主传动减速机", "located_in", "L2") in predicates
    assert ("主传动减速机", "classified_as", "减速机") in predicates


def test_observation_columns_become_metrics(bundle):
    metrics = {item.name: item for item in _entities(bundle, "Metric")}
    assert set(metrics) == {"振动", "轴承温度", "噪声"}
    assert metrics["振动"].attributes["unit"] == "mm/s"
    assert metrics["轴承温度"].attributes["unit"] == "C"


def test_cross_document_fault_mode_carries_two_asset_evidence(bundle):
    modes = {item.name: item for item in _entities(bundle, "FaultMode")}
    assert "振动D区超限" in modes
    mode = modes["振动D区超限"]
    assets = {ev.asset_id for ev in mode.evidence}
    assert len(assets) == 2, "多跳证据必须来自两份不同资产"
    assert mode.attributes["observed_value"] == 7.8
    assert mode.attributes["zone"] == "D"
    assert "危险" in mode.attributes["verdict"]
    assert any("#row:" in ev.locator for ev in mode.evidence)
    assert any("#line:" in ev.locator for ev in mode.evidence)


def test_normal_observation_does_not_become_fault_mode(bundle):
    # 2.1 mm/s sits in zone A ("新机正常") and must not be reported as a fault.
    modes = _entities(bundle, "FaultMode")
    assert all("A区" not in item.name for item in modes)
    assert not any(
        item.attributes.get("observed_value") == 2.1 for item in modes
    )


def test_threshold_rules_parsed_from_markdown_table(bundle):
    rules = {item.name: item for item in _entities(bundle, "ThresholdRule")}
    assert {"振动烈度A区判定", "振动烈度D区判定"} <= set(rules)
    zone_d = rules["振动烈度D区判定"]
    assert zone_d.attributes["lower"] == 7.1
    assert zone_d.attributes["lower_inclusive"] is False
    assert "停机" in zone_d.attributes["action"]

    defined_in = {
        (rel.subject, rel.object)
        for rel in bundle.relations
        if rel.predicate == "defined_in"
    }
    assert ("振动烈度D区判定", "ISO 10816-3:2009 §7.2") in defined_in


def test_prose_threshold_yields_alarm_rule(bundle):
    rules = {item.name: item for item in _entities(bundle, "ThresholdRule")}
    assert rules["轴承温度报警值"].attributes["threshold"] == 85.0
    assert rules["轴承温度停机值"].attributes["threshold"] == 90.0


def test_steps_are_ordered_and_cite_clauses(bundle):
    steps = _entities(bundle, "ProcedureStep")
    assert len(steps) == 5
    orders = sorted(item.attributes["order"] for item in steps)
    assert orders == [1, 2, 3, 4, 5]
    precedes = [rel for rel in bundle.relations if rel.predicate == "precedes"]
    assert len(precedes) == 4
    cites = [rel for rel in bundle.relations if rel.predicate == "cites"]
    assert any("Q/XX-2024-11 §5.3" == rel.object for rel in cites)


def test_every_candidate_has_provenance(bundle):
    stats = bundle_stats(bundle, asset_count=4)
    assert stats["provenance_rate"] == 1.0
    for item in [*bundle.entities, *bundle.relations]:
        assert item.evidence
        for ev in item.evidence:
            head, _, anchor = ev.locator.partition("#")
            assert "/v1" in head, f"定位符必须带资产版本：{ev.locator}"
            assert anchor, f"定位符必须带锚点：{ev.locator}"
            assert ev.quote


def test_ambiguous_name_is_downgraded_to_review(bundle):
    same_name = [item for item in bundle.entities if item.name == "干燥机"]
    assert len(same_name) == 2
    assert {item.type for item in same_name} == {"Equipment", "EquipmentClass"}
    assert all(item.decision is ReviewDecision.REVIEW for item in same_name)


def test_bundle_is_deterministic():
    first = bundle_payload(
        build_bundle(build_catalog_manifest(ROOT), ROOT), asset_count=4
    )
    second = bundle_payload(
        build_bundle(build_catalog_manifest(ROOT), ROOT), asset_count=4
    )
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_stats_counts_are_consistent(bundle):
    stats = bundle_stats(bundle, asset_count=4)
    assert sum(stats["decision_counts"].values()) == stats["candidate_count"]
    assert stats["candidate_count"] == stats["entity_count"] + stats["relation_count"]
    assert stats["type_coverage"] == 1.0
    assert stats["cross_asset_candidates"] >= 2


def test_relation_outside_schema_is_dropped():
    evidence = [ontology.Evidence(asset_id="A", locator="#row:1", quote="q")]
    entities, relations, warnings = ontology._freeze(
        [
            ontology._draft_entity(
                name="泵",
                type_name="Equipment",
                confidence=0.9,
                extractor="test",
                evidence=evidence,
            ),
            ontology._draft_entity(
                name="振动",
                type_name="Metric",
                confidence=0.9,
                extractor="test",
                evidence=evidence,
            ),
        ],
        [("泵", "unknown_predicate", "振动", 0.9, "test", evidence)],
        schema=SEED_ONTOLOGY,
    )
    assert len(entities) == 2
    assert relations[0].decision is ReviewDecision.DROP
    assert any("未声明的谓词" in item for item in warnings)


def test_self_referential_relation_is_skipped(bundle):
    loops = [
        rel
        for rel in bundle.relations
        if normalize_name(rel.subject) == normalize_name(rel.object)
    ]
    assert loops == []
    assert any("自指关系已跳过" in item for item in bundle.warnings)


def test_plan_merge_classifies_add_merge_conflict(bundle):
    snapshot = [
        {"name": "主传动减速机", "type": "Equipment", "attributes": {"criticality": "B"}},
        {"name": "L2", "type": "Line", "attributes": {}},
        {"name": "不存在的设备", "type": "Equipment", "attributes": {}},
    ]
    ops = plan_merge(bundle, snapshot)
    summary = merge_summary(ops)
    assert summary["add"] > 0
    assert summary["merge"] >= 1
    assert summary["conflict"] >= 1
    conflict = next(item for item in ops if item.op == "conflict")
    assert conflict.name == "主传动减速机"
    assert "criticality" in conflict.detail


def test_parse_range_forms():
    assert parse_range("≤ 2.8") == {"upper": 2.8, "upper_inclusive": True}
    assert parse_range("> 7.1") == {"lower": 7.1, "lower_inclusive": False}
    assert parse_range("2.8 ~ 4.5") == {
        "lower": 2.8,
        "upper": 4.5,
        "lower_inclusive": True,
        "upper_inclusive": True,
    }
    assert parse_range("无法判定") is None


def test_rule_matches_respects_open_bounds():
    zone_d = {"lower": 7.1, "lower_inclusive": False}
    assert rule_matches(zone_d, 7.8) is True
    assert rule_matches(zone_d, 7.1) is False
    assert rule_matches({"upper": 2.8, "upper_inclusive": True}, 2.8) is True


def test_normalize_and_decide_thresholds():
    assert normalize_name("振动 mm/s") == normalize_name("振动mm/s")
    assert decide(0.95) is ReviewDecision.AUTO_ACCEPT
    assert decide(0.6) is ReviewDecision.REVIEW
    assert decide(0.1) is ReviewDecision.DROP
