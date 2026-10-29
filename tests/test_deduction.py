"""P6 deduction: six hops against the documented ground truth.

The assertions mirror `docs/urban-scenario-ground-truth.md` §2/§5. If a number
here changes, either the dataset changed or the engine drifted -- both need a
deliberate decision, not a test edit.

Two structural claims get their own tests because they are what separates this
from "让模型写一段推演":
- rules are data whose `source_ref` is verified against the real file line;
- a rule that does not match produces `insufficient`, never an invented answer.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from decision_poc.deduction import (
    DEDUCTION_STAGES,
    build_rule_registry,
    RULE_STAGES,
    DeductionEngine,
    DeductionRule,
    RuleSet,
    RuleSourceError,
    StageStatus,
    load_rules,
    verify_rules,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

AS_OF = datetime.fromisoformat("2026-10-29T09:40:00+08:00")
EVENT = "WO-20261029-007"

RULES = load_rules()
SOURCE_REFS = dict(verify_rules(RULES, DEFAULT_DATASET_ROOT))


@pytest.fixture(scope="module")
def engine() -> DeductionEngine:
    return DeductionEngine.load()


@pytest.fixture(scope="module")
def result(engine: DeductionEngine):
    return engine.deduce(EVENT, as_of=AS_OF)


# --- rules are data, and the data is checked ----------------------------


def test_every_rule_points_at_a_real_line() -> None:
    assert len(SOURCE_REFS) == len(RULES.rules)
    for ref in SOURCE_REFS.values():
        asset_id, locator = ref.split("#", 1)
        assert asset_id.startswith("AS-")
        assert locator


def test_rule_with_wrong_keyword_is_refused() -> None:
    """规则原文里没有它声称的那句话，就不许加载。"""
    payload = json.loads((DEFAULT_DATASET_ROOT.parents[1] / "rules" / "urban_deduction_rules.json").read_text(encoding="utf-8"))
    payload["rules"][0]["keyword"] = "这句话原文里没有"
    with pytest.raises(RuleSourceError, match="正文不含"):
        verify_rules(RuleSet.model_validate(payload), DEFAULT_DATASET_ROOT)


def test_rule_with_out_of_range_locator_is_refused() -> None:
    payload = json.loads((DEFAULT_DATASET_ROOT.parents[1] / "rules" / "urban_deduction_rules.json").read_text(encoding="utf-8"))
    payload["rules"][0]["source_ref"] = "plan_flood_emergency_v2.md#A-03/emergency_plan/v2.0#line:9999"
    with pytest.raises(RuleSourceError, match="out of range|has"):
        verify_rules(RuleSet.model_validate(payload), DEFAULT_DATASET_ROOT)


def test_assumptions_are_declared_not_hidden() -> None:
    assumptions = {rule.rule_id for rule in RULES.assumptions}
    assert assumptions == {"RULE-RES-PIT-PUMPS", "RULE-RES-LOWSPOT-PUMPS"}
    for rule in RULES.assumptions:
        assert rule.assumption_note, "假设必须自己说明"


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError):
        DeductionRule(
            rule_id="R",
            stage="vibes",
            source_ref="a.md#A-03/x/v1#line:1",
            keyword="x",
        )


def test_stages_are_fixed_six_hops() -> None:
    assert DEDUCTION_STAGES == ("observed", "affected", "owner", "conflict", "resource", "action")
    assert set(RULE_STAGES) == set(DEDUCTION_STAGES) | {"residual_risk"}


# --- 跳 1 现象 ----------------------------------------------------------


def test_hop1_response_level_and_depth(result) -> None:
    assert result.root.response_level == "orange"
    assert result.root.depth_cm == 30.0
    assert result.root.district == "A-03"
    assert result.root.warning_level == "orange"
    step = result.stage("observed")[0]
    assert step.status is StageStatus.SUPPORTED
    assert "RULE-OBS-RESPONSE-ORANGE" in step.rule_ids
    assert step.evidence_refs


# --- 跳 2 受影响对象 ----------------------------------------------------


def test_hop2_contains_ground_truth_entities(result) -> None:
    affected = set(result.stage("affected")[0].conclusion["affected"].split(","))
    required = {
        "R-101",
        "PL-DRN-101",
        "DF-IN-101",
        "DF-PS-01",
        "PL-GAS-101",
        "CP-2026-041",
        "R-105",
        "R-103",
        "R-201",
    }
    assert required <= affected, f"缺少 ground truth 实体: {required - affected}"


def test_hop2_does_not_drag_in_unrelated_district(result) -> None:
    affected = set(result.stage("affected")[0].conclusion["affected"].split(","))
    assert "R-202" not in affected, "B-01 的无关道路不应进入 A-03 事件的影响范围"


# --- 跳 3 责任部门 ------------------------------------------------------


def test_hop3_coordinator_and_owner_map(result) -> None:
    step = result.stage("owner")[0]
    assert step.conclusion["coordinator"] == "ORG-EMERG"
    assert "DF-IN-101:ORG-DRAIN" in step.conclusion["owners"]
    assert step.conclusion["unassigned"] == "DF-PS-01"


def test_hop3_construction_site_is_not_a_maintenance_gap(result) -> None:
    """施工项目有施工方与许可方，不该被当成"没人管的资产"。"""
    unassigned = result.stage("owner")[0].conclusion.get("unassigned", "")
    assert "CP-2026-041" not in unassigned


# --- 跳 4 职责冲突 ------------------------------------------------------


def test_hop4_all_three_conflict_kinds_present(result) -> None:
    kinds = {conflict.kind.value for conflict in result.conflicts}
    assert kinds == {"overlap", "gap", "contention"}, "三类冲突必须全部出现，不得被综合掉"


def test_hop4_overlap_is_road_and_traffic(result) -> None:
    overlap = next(c for c in result.conflicts if c.kind.value == "overlap")
    assert set(overlap.orgs) == {"ORG-ROAD", "ORG-TRAFFIC"}
    assert "RULE-CONF-CLOSURE-JOINT" in overlap.rule_ids
    assert overlap.evidence_refs


def test_hop4_gap_is_the_pump_station(result) -> None:
    gaps = [c for c in result.conflicts if c.kind.value == "gap"]
    assert [c.subject for c in gaps] == ["DF-PS-01"]
    assert gaps[0].orgs == []


def test_hop4_conflict_ids_are_unique_and_ordered(result) -> None:
    ids = [conflict.conflict_id for conflict in result.conflicts]
    assert ids == sorted(set(ids))
    assert ids == ["C1", "C2", "C3"]


# --- 跳 5 资源需求 ------------------------------------------------------


def test_hop5_gap_is_exactly_one_pump(result) -> None:
    assert result.resource_gaps, "必须有资源缺口"
    gap = result.resource_gaps[0]
    assert (gap.required, gap.available, gap.gap) == (7, 6, 1)


def test_hop5_basis_shows_the_arithmetic(result) -> None:
    basis = " | ".join(result.resource_gaps[0].basis)
    assert "1200" in basis and "0.8" in basis and "960" in basis, "泵站折算必须留痕"
    assert "400" in basis, "移动泵单台能力必须留痕"
    assert "基坑" in basis
    assert "低洼点" in basis, "跨片区需求必须单列"


def test_hop5_required_pumps_are_enumerated_not_asserted(result) -> None:
    gap = result.resource_gaps[0]
    assert gap.rule_ids
    assert gap.evidence_refs


# --- 跳 6 处置顺序 ------------------------------------------------------


def test_hop6_isolate_before_pump(result) -> None:
    actions = result.action_order
    isolate = next(i for i, text in enumerate(actions) if "隔离风险源" in text)
    pump = next(i for i, text in enumerate(actions) if "启动泵站抽排" in text)
    assert isolate < pump, "先隔离风险源必须先于抽排；否则等于引用了废止预案"


def test_hop6_starts_with_stopping_excavation(result) -> None:
    assert "停止基坑开挖" in result.action_order[0]


def test_hop6_closure_uses_current_threshold(result) -> None:
    closure = [text for text in result.action_order if "封闭积水路段" in text]
    assert closure, "30cm 已过 25cm 阈值，必须给出封路动作"
    assert "25cm" in closure[0]


def test_hop6_joint_publication_is_required(result) -> None:
    assert any("联合发布" in text for text in result.action_order)


def test_hop6_every_action_cites_a_rule(engine: DeductionEngine, result) -> None:
    verified = set(SOURCE_REFS)
    for step in result.stage("action"):
        if step.status is StageStatus.INSUFFICIENT:
            continue
        assert step.rule_ids, f"{step.step_id} 没有规则依据"
        assert set(step.rule_ids) <= verified


# --- 二次风险 -----------------------------------------------------------


def test_residual_risks_cover_ground_truth_mechanisms(result) -> None:
    joined = " ".join(result.residual_risks)
    assert "管周土体流失" in joined
    assert "爆炸下限" in joined
    assert "绕行" in joined
    assert len(result.residual_risks) >= 3


# --- 结构性断言 ---------------------------------------------------------


def test_supported_steps_always_carry_evidence(result) -> None:
    for step in result.steps:
        if step.status is StageStatus.SUPPORTED:
            assert step.evidence_refs, f"{step.step_id} 有结论却没有证据"


def test_no_step_uses_the_deprecated_plan(engine: DeductionEngine, result) -> None:
    """废止预案不能出现在任何证据里（S1 用例）。"""
    deprecated = {
        asset_id
        for asset_id, record in _governance().items()
        if record.status.value == "deprecated"
    }
    for step in result.steps:
        cited = {ref.split("#", 1)[0] for ref in step.evidence_refs}
        assert not (cited & deprecated), f"{step.step_id} 引用了废止资产"


def test_deduction_is_deterministic(engine: DeductionEngine) -> None:
    first = engine.deduce(EVENT, as_of=AS_OF).model_dump(mode="json")
    second = engine.deduce(EVENT, as_of=AS_OF).model_dump(mode="json")
    assert first == second


def test_result_is_anchored_to_a_snapshot(engine: DeductionEngine, result) -> None:
    assert result.snapshot_hash == engine.store.snapshot_hash()
    assert result.root.snapshot_hash == result.snapshot_hash
    assert result.rules_version == RULES.version


def test_missing_observed_rule_degrades_to_insufficient(engine: DeductionEngine) -> None:
    """把分级规则摘掉，该跳必须标 insufficient，而不是编一个等级。"""
    stripped = RuleSet(
        version="stripped",
        rules=[rule for rule in RULES.rules if rule.stage != "observed"],
    )
    refs = {rule_id: ref for rule_id, ref in SOURCE_REFS.items() if any(
        rule.rule_id == rule_id for rule in stripped.rules
    )}
    degraded = DeductionEngine(engine.store, stripped, source_refs=refs)
    outcome = degraded.deduce(EVENT, as_of=AS_OF)
    assert outcome.stage("observed")[0].status is StageStatus.INSUFFICIENT
    assert outcome.root.response_level is None


def test_engine_refuses_to_build_without_required_rules(engine: DeductionEngine) -> None:
    stripped = RuleSet(
        version="stripped",
        rules=[rule for rule in RULES.rules if rule.rule_id != "RULE-RES-PUMP-DERATE"],
    )
    with pytest.raises(RuleSourceError, match="必需"):
        DeductionEngine(engine.store, stripped, source_refs=SOURCE_REFS)


def test_every_stage_appears_in_output(result) -> None:
    assert result.missing_stages() == []
    assert [step.stage for step in result.steps[:3]] == ["observed", "affected", "owner"]


def test_assumptions_surface_on_the_result(result) -> None:
    assert len(result.assumptions) == 2
    assert all("演示假设" in text or "假设" in text for text in result.assumptions)


def _governance():
    from decision_poc.urban_dataset import load_urban_assets

    return {asset.asset_id: record for asset, record in load_urban_assets()}


# --- 结论 -> 证据链 -----------------------------------------------------


def _ledger_for(rules: RuleSet, refs: dict[str, str]):
    from decision_poc.provenance import ProvenanceLedger
    from decision_poc.urban_dataset import load_urban_assets

    return ProvenanceLedger(
        asset_fingerprints={asset.asset_id: asset.checksum_sha256 for asset, _ in load_urban_assets()},
        knowledge_version="kb-v1.0.0",
        rules=build_rule_registry(rules, refs),
    )


def test_every_conclusion_becomes_a_supported_claim(result) -> None:
    from decision_poc.deduction import claims_from

    claims = claims_from(result)
    assert len(claims) >= 6
    ledger = _ledger_for(RULES, SOURCE_REFS)
    checks = ledger.register(claims)
    rejected = [(check.claim_id, check.reasons) for check in checks if not check.supported]
    assert rejected == [], f"推演结论不满足引用门槛: {rejected}"
    for claim in claims:
        assert claim.evidence_refs, f"{claim.claim_id} 无证据"


def test_multi_source_conclusions_are_inferences_not_rule_outputs(result) -> None:
    from decision_poc.deduction import claims_from

    kinds = [claim.kind.value for claim in claims_from(result)]
    assert kinds.count("inference") >= 6
    assert "rule_output" in kinds or "fact" in kinds


def test_recorded_deduction_verifies_and_traces(result) -> None:
    from decision_poc.deduction import build_rule_registry, claims_from, record  # noqa: F401
    from decision_poc.deduction import record as record_fn

    ledger = _ledger_for(RULES, SOURCE_REFS)
    rec = record_fn(result, ledger)
    assert rec.claim_ids
    assert rec.semantic_snapshot_hash == result.snapshot_hash
    assert ledger.verify().ok
    assert any(processor.startswith("deduction@") for processor in rec.processors)

    trace = ledger.trace_claim(rec.claim_ids[0])
    assert trace.knowledge_version == "kb-v1.0.0"
    assert trace.decision_ids == [f"DEC-{result.event_id}-{result.root.as_of.strftime('%Y%m%dT%H%M')}"]
    assert trace.assets and all(asset.fingerprint for asset in trace.assets)


def test_handoff_flags_gap_and_assumptions(result) -> None:
    assert result.handoff_required is True
    joined = " ".join(result.handoff_reasons)
    assert "DF-PS-01" in joined, "职责空白必须交给人工，不能默认指派"
    assert "演示假设" in joined


def test_cli_runs_the_whole_chain_and_records(tmp_path) -> None:
    """一条命令跑完 事件理解 → 推演 → 落账，且账本自洽。"""
    from decision_poc.deduction import main

    ledger_dir = tmp_path / "ledger"
    code = main(
        [
            "--event",
            EVENT,
            "--as-of",
            AS_OF.isoformat(),
            "--record",
            str(ledger_dir),
        ]
    )
    assert code == 0
    assert (ledger_dir / "records.jsonl").is_file()
    assert (ledger_dir / "claims.jsonl").is_file()

    from decision_poc.provenance import ProvenanceLedger
    from decision_poc.urban_dataset import load_urban_assets

    restored = ProvenanceLedger.load(
        ledger_dir,
        asset_fingerprints={a.asset_id: a.checksum_sha256 for a, _ in load_urban_assets()},
        knowledge_version="kb-v1.0.0",
        rules=build_rule_registry(RULES, SOURCE_REFS),
    )
    assert restored.verify().ok
    assert len(restored.records()) == 1
