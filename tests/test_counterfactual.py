"""Counterfactuals must be replayed, not narrated.

Each test pins one property a reviewer will actually check: does the branch keep
the baseline comparable, does it change only what the intervention should change,
does it refuse to state what the data cannot support, and does it leave the
sample dataset untouched.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from decision_poc.counterfactual import (
    CASES,
    CounterfactualUnavailable,
    Intervention,
    InterventionKind,
    _patch_rules,
    _patch_table,
    run_counterfactual,
)
from decision_poc.governance import ErrorCode
from decision_poc.provenance import build_ledger
from decision_poc.skills import SkillRuntime
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

EVENT = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
LABELS = ["ORG-EMERG"]


@pytest.fixture(scope="module")
def runtime() -> SkillRuntime:
    return SkillRuntime()


@pytest.fixture(scope="module")
def cf1(runtime: SkillRuntime):
    return run_counterfactual("CF1", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)


@pytest.fixture(scope="module")
def cf2(runtime: SkillRuntime):
    return run_counterfactual("CF2", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)


@pytest.fixture(scope="module")
def cf3(runtime: SkillRuntime):
    return run_counterfactual("CF3", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- CF1 -----------------------------------------------------------------


def test_cf1_moves_the_closure_ahead_without_inventing_actions(cf1) -> None:
    assert cf1.diff.changed is True
    moves = {item.action: (item.before, item.after) for item in cf1.diff.actions_moved}
    closure = next(key for key in moves if "封闭积水路段" in key)
    isolate = next(key for key in moves if "隔离风险源" in key)
    assert moves[closure] == (2, 1), "封路应从第 3 位（0 基 2）提前到第 2 位"
    assert moves[isolate] == (1, 3), "隔离风险源应后移到第 4 位（0 基 3）"
    assert not cf1.diff.actions_added and not cf1.diff.actions_removed, "顺序干预不该增减动作"
    assert "第 3 位提前到第 2 位" in cf1.verdict


def test_cf1_reports_the_road_set_after_the_closure(cf1) -> None:
    states = {road.road_id: road for road in cf1.road_states}
    assert states["R-101"].closed and not states["R-101"].passable
    assert states["R-103"].under_construction, "民生街被指定为绕行，但本身在占道施工"
    assert states["R-105"].under_construction
    assert states["R-104"].passable
    passable = [road.road_id for road in cf1.road_states if road.passable]
    assert passable == ["R-102", "R-104"]
    assert "可通行道路为 建设大道、环城北路" in cf1.verdict


def test_cf1_says_what_it_cannot_compute(cf1) -> None:
    """没有路网拓扑就算不得到场时间，宁可写"算不出来"也不写一个像样的数字。"""
    joined = " ".join(cf1.uncomputable)
    assert "到场时间" in joined
    assert "路网拓扑" in joined
    assert "里程" in joined
    assert all(not any(ch.isdigit() for ch in line.split("：")[0]) for line in cf1.uncomputable)


# --- CF2 -----------------------------------------------------------------


def test_cf2_shrinks_inventory_and_widens_the_gap(cf2) -> None:
    assert cf2.baseline.inventory == 6
    assert cf2.intervened.inventory == 2
    assert cf2.baseline.gaps["RES-PUMP-M"] == 1
    assert cf2.intervened.gaps["RES-PUMP-M"] == 5
    assert cf2.diff.gaps_delta["RES-PUMP-M"] == 4
    assert cf2.diff.changed is True
    assert "6 台 → 2 台" in cf2.verdict


def test_cf2_prioritises_the_district_that_crosses_the_threshold(cf2) -> None:
    assert cf2.priority is not None
    ranked = {case.district: case for case in cf2.priority.ranked}
    assert set(ranked) == {"A-03", "B-01"}, "两个片区都必须参与比较，不能只报调用方所在片区"
    assert ranked["A-03"].depth_cm == 30
    assert ranked["A-03"].closure_required is True
    assert ranked["A-03"].has_hazard is True
    assert ranked["B-01"].depth_cm == 18
    assert ranked["B-01"].closure_required is False
    assert cf2.priority.recommended == "A-03"
    assert "已过封路阈值" in cf2.priority.reasons[0]


def test_cf2_quotes_the_cross_district_cost_from_the_data(cf2) -> None:
    assert cf2.priority is not None
    assert "40 分钟" in cf2.priority.cross_district_note
    assert cf2.priority.evidence_refs, "跨区代价必须有证据定位，不能是一句经验"
    assert "40 分钟" in cf2.verdict


# --- CF3 -----------------------------------------------------------------


def test_cf3_upgrades_exposure_level_instead_of_adding_a_number(cf3) -> None:
    """工期顺延后开挖面暴露期与更强的预警窗口重叠，风险等级上调——这是算出来的，不是写的。"""
    assert cf3.baseline.exposed_warning_level == "orange"
    assert cf3.intervened.exposed_warning_level == "red"
    assert cf3.intervened.exposed_hours > cf3.baseline.exposed_hours
    assert cf3.baseline.actions == cf3.intervened.actions, "工期变化不该改动处置动作"
    assert cf3.baseline.gaps == cf3.intervened.gaps
    assert cf3.diff.risks_added and cf3.diff.risks_removed
    assert "orange" in cf3.verdict and "red" in cf3.verdict


def test_cf3_still_states_the_blind_spot(cf3) -> None:
    """有规则能答的部分答了，超出预警时效的部分仍然声明算不出来。"""
    joined = " ".join(cf3.uncomputable)
    assert "超出预警发布时效" in joined
    assert not any("缺“暴露期×预警重叠”规则" in item for item in cf3.uncomputable)


def test_future_warning_never_leaks_into_todays_observation(runtime: SkillRuntime) -> None:
    """10-30 的红色预警在今天 09:40 只是"未来窗口"，不能被当作现行预警。"""
    from decision_poc.skills import SKILL_BY_NAME

    result = SKILL_BY_NAME["event_understand"].invoke(
        runtime, {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS}
    )
    assert result.data["payload"]["warning_level"] == "orange"


# --- invariants ----------------------------------------------------------


def test_branches_never_touch_the_sample_dataset(runtime: SkillRuntime) -> None:
    watched = ["resource_inventory.csv", "construction_plans.csv"]
    before = {name: _digest(DEFAULT_DATASET_ROOT / name) for name in watched}
    run_counterfactual("CF2", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)
    run_counterfactual("CF3", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)
    after = {name: _digest(DEFAULT_DATASET_ROOT / name) for name in watched}
    assert before == after, "反事实必须改副本，样本数据一个字节都不能动"


def test_counterfactual_is_deterministic(runtime: SkillRuntime) -> None:
    left = run_counterfactual("CF2", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)
    right = run_counterfactual("CF2", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=runtime)
    assert left.model_dump(mode="json") == right.model_dump(mode="json")


def test_branch_runs_do_not_write_the_ledger() -> None:
    from decision_poc.deduction import build_rule_registry, load_rules, verify_rules
    from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

    rules = load_rules()
    ledger = build_ledger(
        rules=build_rule_registry(rules, dict(verify_rules(rules, DEFAULT_DATASET_ROOT)))
    )
    guarded = SkillRuntime(ledger=ledger)
    run_counterfactual("CF1", event_id=EVENT, as_of=AS_OF, caller_labels=LABELS, runtime=guarded)
    assert guarded.ensure_ledger().records() == [], "反事实是推演，不是对外结论，不进账本"


def test_permission_boundaries_hold_inside_the_branch(runtime: SkillRuntime) -> None:
    """越权时不是回一份"空差异"，而是拒绝比较 —— 空差异会被读成"没有影响"。"""
    with pytest.raises(CounterfactualUnavailable) as caught:
        run_counterfactual(
            "CF1", event_id=EVENT, as_of=AS_OF, caller_labels=["ORG-ROAD"], runtime=runtime
        )
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    assert "resource_check" in caught.value.message or "deduce" in caught.value.message


# --- intervention plumbing ----------------------------------------------


def test_patch_must_hit_exactly_one_row(tmp_path: Path) -> None:
    table = tmp_path / "resource_inventory.csv"
    table.write_text(
        "resource_id,qty_available\nRES-PUMP-M,4\nRES-PUMP-M-B01,2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="唯一命中"):
        _patch_table(
            tmp_path,
            [
                Intervention(
                    kind=InterventionKind.DATA_PATCH,
                    target="resource_inventory.csv",
                    match={"resource_id": "RES-NOPE"},
                    field="qty_available",
                    value="0",
                )
            ],
        )


def test_patch_rejects_unknown_field_or_rule() -> None:
    table_dir = Path(DEFAULT_DATASET_ROOT)
    with pytest.raises(KeyError):
        _patch_table(
            table_dir,
            [
                Intervention(
                    kind=InterventionKind.DATA_PATCH,
                    target="resource_inventory.csv",
                    match={"resource_id": "RES-PUMP-M"},
                    field="no_such_column",
                    value="1",
                )
            ],
        )

    payload = {"rules": [{"rule_id": "RULE-A", "then": {"order": "1"}}]}
    with pytest.raises(KeyError):
        _patch_rules(
            payload,
            [Intervention(kind=InterventionKind.RULE_PATCH, target="RULE-NOPE", field="then.order", value="9")],
        )
    with pytest.raises(KeyError):
        _patch_rules(
            payload,
            [Intervention(kind=InterventionKind.RULE_PATCH, target="RULE-A", field="then.missing", value="9")],
        )


def test_every_case_states_question_and_intervention() -> None:
    for case_id, case in CASES.items():
        assert case.case_id == case_id
        assert case.question.endswith(("？", "?"))
        assert case.interventions
        for item in case.interventions:
            assert "#" not in item.value
            if item.kind is InterventionKind.DATA_PATCH:
                assert item.match, f"{case_id} 的数据补丁必须能定位到行"


def test_result_carries_evidence_and_iso_time(cf1) -> None:
    assert cf1.as_of == datetime.fromisoformat(AS_OF)
    assert cf1.evidence_refs
    assert all("#" in ref for ref in cf1.evidence_refs)


def test_exposure_rules_point_at_real_plan_lines() -> None:
    """补规则不能只写条件，出处必须仍能在预案里逐字对上。"""
    from decision_poc.deduction import load_rules, verify_rules

    rules = load_rules()
    exposure = {r.rule_id for r in rules.rules if "pit_exposed_warning_level" in r.when}
    assert exposure == {"RULE-RISK-PIT-EXPOSED-ORANGE", "RULE-RISK-PIT-EXPOSED-RED"}
    refs = dict(verify_rules(rules, DEFAULT_DATASET_ROOT))
    assert exposure <= set(refs)
    assert all("emergency_plan" in refs[rule_id] for rule_id in exposure)
