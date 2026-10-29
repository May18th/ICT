"""T5 city extraction tests, including the behaviours the demo depends on."""

from collections import Counter

from decision_poc.city_extract import (
    build_city_bundle,
    extract_city_candidates,
    extraction_stats,
)
from decision_poc.ontology import ReviewDecision

BUNDLE = build_city_bundle()


def _entities(type_name: str):
    return [entity for entity in BUNDLE.entities if entity.type == type_name]


def test_entity_counts_per_type() -> None:
    counts = Counter(entity.type for entity in BUNDLE.entities)
    assert counts["Road"] == 7
    assert counts["Pipeline"] == 8
    assert counts["DrainageFacility"] == 6
    assert counts["ConstructionProject"] == 3
    assert counts["Incident"] == 5
    assert counts["WeatherEvent"] == 3
    assert counts["Department"] == 8
    assert counts["Resource"] == 9
    assert counts["Area"] == 2
    assert counts["EmergencyPlan"] == 4
    assert counts["Action"] >= 6


def test_every_candidate_carries_evidence() -> None:
    for entity in BUNDLE.entities:
        assert entity.evidence
        assert all(item.locator for item in entity.evidence)
    for relation in BUNDLE.relations:
        assert relation.evidence


def test_no_schema_violations() -> None:
    assert [w for w in BUNDLE.warnings if "schema 校验" in w] == []


def test_pump_station_responsibility_stays_blank() -> None:
    """职责空白是演示的前提，抽取器不许自动补全。"""
    relations = [
        relation
        for relation in BUNDLE.relations
        if relation.object == "DF-PS-01" and relation.predicate == "responsible_for"
    ]
    assert relations == []


def test_closure_authority_overlap_is_detected() -> None:
    conflicts = [
        (relation.subject, relation.object)
        for relation in BUNDLE.relations
        if relation.predicate == "conflicts_with"
    ]
    assert ("ORG-ROAD", "ORG-TRAFFIC") in conflicts


def test_gas_pipeline_under_construction() -> None:
    pairs = {
        (relation.subject, relation.object)
        for relation in BUNDLE.relations
        if relation.predicate == "under_construction"
    }
    assert ("PL-GAS-101", "CP-2026-041") in pairs
    assert ("R-101", "CP-2026-041") in pairs


def test_deprecated_plan_goes_to_review_not_auto_accept() -> None:
    deprecated = [
        entity
        for entity in BUNDLE.entities
        if any("已废止" in str(entity.attributes.get("file_name", "")) for _ in [0])
    ]
    legacy_plan = next(
        entity for entity in BUNDLE.entities if "2021" in entity.name and entity.type == "EmergencyPlan"
    )
    assert legacy_plan.decision is ReviewDecision.REVIEW
    assert legacy_plan.confidence < 0.8
    legacy_actions = [
        entity
        for entity in BUNDLE.entities
        if entity.type == "Action" and "抽排" in entity.name and entity.decision is ReviewDecision.REVIEW
    ]
    assert legacy_actions
    assert deprecated == []


def test_procedure_steps_form_an_ordered_chain() -> None:
    precedes = [relation for relation in BUNDLE.relations if relation.predicate == "precedes"]
    assert len(precedes) >= 4
    first = next(
        relation
        for relation in precedes
        if relation.subject.startswith("先隔离风险源")
    )
    assert first.object.startswith("再控制水势")


def test_plan_entities_are_named_by_document_title() -> None:
    names = {entity.name for entity in _entities("EmergencyPlan")}
    assert "城区防汛排涝应急预案（2025 年修订）" in names
    assert "地下燃气管道施工保护规程" in names


def test_two_districts_exist_for_counterfactual() -> None:
    """CF2（资源不足时优先保障哪个区域）需要两个片区与两处库存。"""
    areas = {entity.name for entity in BUNDLE.entities if entity.type == "Area"}
    assert areas == {"A-03", "B-01"}
    pumps = [
        entity
        for entity in BUNDLE.entities
        if entity.type == "Resource" and "抽水泵" in str(entity.attributes.get("name", ""))
    ]
    assert len(pumps) == 2
    districts = {str(entity.attributes.get("district")) for entity in pumps}
    assert districts == {"A-03", "B-01"}


def test_extraction_is_deterministic() -> None:
    stats_a = extraction_stats(build_city_bundle())
    stats_b = extraction_stats(build_city_bundle())
    assert stats_a == stats_b
    assert stats_a.auto_accept + stats_a.review + stats_a.drop == stats_a.entities
