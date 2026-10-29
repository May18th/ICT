"""P6 item 1: the five Skills, the orchestration, and the Nexent binding table.

The assertions are written against behaviour an orchestrator depends on, not
against internal structure: what a Skill returns when the input is unusable,
what it refuses to return when evidence is missing, and whether the published
binding table can name a tool that does not exist.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from decision_poc.governance import ErrorCode
from decision_poc.mcp_server import UrbanToolkit
from decision_poc.provenance import ProvenanceLedger, build_ledger
from decision_poc.skills import (
    ImpactRequest,
    SKILL_ORDER,
    SKILLS,
    SKILL_BY_NAME,
    PipelineReport,
    SkillPipeline,
    SkillRequest,
    SkillRuntime,
    render_report,
    skill_manifest,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

EVENT = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
LABELS = ["ORG-EMERG"]


@pytest.fixture(scope="module")
def runtime() -> SkillRuntime:
    return SkillRuntime()


@pytest.fixture(scope="module")
def report(runtime: SkillRuntime) -> PipelineReport:
    return SkillPipeline(runtime).run(EVENT, AS_OF, caller_labels=LABELS)


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "urban"
    if not target.exists():
        shutil.copytree(DEFAULT_DATASET_ROOT, target)
    return target


# --- manifest -----------------------------------------------------------


def test_manifest_lists_five_skills_in_pipeline_order() -> None:
    manifest = skill_manifest()
    assert manifest["order"] == list(SKILL_ORDER)
    assert [item["stage"] for item in manifest["skills"]] == list(SKILL_ORDER)
    assert len(manifest["skills"]) == 5


def test_manifest_only_names_tools_that_exist() -> None:
    """绑定表写错一个工具名，Nexent 侧要到运行时才会报错 —— 这里提前拦。"""
    available = {tool["name"] for tool in UrbanToolkit().tools()}
    declared = list(skill_manifest()["skills"]) + list(skill_manifest()["standalone_tools"])
    for item in declared:
        assert item["mcp_tools"], f"{item['name']} 未声明依赖的 MCP 工具"
        unknown = set(item["mcp_tools"]) - available
        assert not unknown, f"{item['name']} 绑定了不存在的工具: {unknown}"


def test_every_skill_declares_a_usable_request_schema() -> None:
    for skill in SKILLS:
        schema = skill.request_model.model_json_schema()
        assert set(schema["required"]) >= {"event_id", "as_of"}
        assert skill.description and skill.failure_mode


# --- happy path ---------------------------------------------------------


def test_pipeline_runs_all_five_stages(report: PipelineReport) -> None:
    assert [step.stage for step in report.steps] == list(SKILL_ORDER)
    assert report.status == "ok"
    assert report.publishable is True
    assert report.stop_reason is None


def test_event_is_understood_from_the_same_parse_as_deduction(report: PipelineReport) -> None:
    """深度来自工单描述文本，必须与引擎解析结果一致（这里 30cm → 橙色响应）。"""
    facts = report.facts
    assert facts is not None
    assert facts.depth_cm == 30.0
    assert facts.district == "A-03"
    assert facts.response_level == "orange"
    assert facts.warning_level == "orange"
    assert facts.data_gaps and any("面积" in gap for gap in facts.data_gaps)


def test_impact_scope_excludes_scope_labels_and_lists_real_objects(report: PipelineReport) -> None:
    scope = report.impact
    assert scope is not None
    names = {item.name for item in scope.affected}
    assert {"R-101", "DF-PS-01", "PL-GAS-101"} <= names
    assert scope.districts == ["A-03"], "片区是范围标签，不是受影响对象"
    assert "ORG-DRAIN" in scope.departments
    assert all(item.kind not in {"region", "department"} for item in scope.affected)
    assert scope.roads and "R-101" in scope.roads


def test_resource_gap_matches_ground_truth(report: PipelineReport) -> None:
    resources = report.resources
    assert resources is not None
    assert resources.unavailable_reason is None
    [line] = resources.lines
    assert (line.required, line.available, line.gap) == (7, 6, 1)
    assert any("960" in item for item in line.basis), "折算过程必须写在依据里"


def test_deduction_reports_three_kinds_of_conflict(report: PipelineReport) -> None:
    view = report.deduction
    assert view is not None
    kinds = {conflict.kind.value for conflict in view.conflicts}
    assert kinds == {"overlap", "gap", "contention"}
    order = view.action_order
    assert order.index(next(text for text in order if "隔离风险源" in text)) < order.index(
        next(text for text in order if "启动泵站抽排" in text)
    )
    assert len(view.residual_risks) >= 3
    assert any("DF-PS-01" in reason for reason in view.handoff_reasons)


def test_audit_passes_and_seals_the_ledger(report: PipelineReport) -> None:
    audit = report.audit
    assert audit is not None
    assert audit.rejected == 0
    assert audit.supported == len(audit.claims) >= 10
    assert audit.citation_completeness == 1.0
    assert audit.chain_ok is True
    assert audit.record_id and audit.record_hash
    assert audit.decision_id == f"DEC-{EVENT}-20261029T0940"
    assert all(claim.evidence_refs for claim in audit.claims)


def test_report_renders_the_five_sections(report: PipelineReport) -> None:
    text = render_report(report)
    for heading in ("[1 事件理解]", "[2 影响分析]", "[3 资源检查]", "[4 跨部门推演]", "[5 证据审计]"):
        assert heading in text
    assert "缺口 1 台" in text


# --- degradations and refusals -----------------------------------------


def test_unknown_event_stops_the_chain(runtime: SkillRuntime) -> None:
    report = SkillPipeline(runtime).run("WO-ghost", AS_OF, caller_labels=LABELS)
    assert report.status == "error"
    assert report.publishable is False
    assert [step.stage for step in report.steps] == ["understand"]
    assert report.steps[0].error_code == ErrorCode.NOT_FOUND.value
    assert report.impact is None and report.deduction is None and report.audit is None


def test_non_incident_cannot_start_an_event(runtime: SkillRuntime) -> None:
    report = SkillPipeline(runtime).run("R-101", AS_OF, caller_labels=LABELS)
    assert report.status == "error"
    assert report.steps[0].error_code == ErrorCode.VALIDATION_FAILED.value


def test_caller_without_gas_permission_gets_no_conclusion(runtime: SkillRuntime) -> None:
    """路政身份看不到燃气规程：可以看影响范围，但不能拿走基于它的推演结论。"""
    report = SkillPipeline(runtime).run(EVENT, AS_OF, caller_labels=["ORG-ROAD"])
    assert report.status == "partial"
    assert report.publishable is False
    assert report.impact is not None, "可见的影响范围仍应返回"
    assert report.resources is None
    assert report.deduction is None
    assert report.audit is None
    stages = {step.stage: step for step in report.steps}
    assert stages["deduce"].error_code == ErrorCode.PERMISSION_DENIED.value
    assert report.stop_reason and "不可见" in report.stop_reason


def test_expired_weather_is_excluded_not_reused(runtime: SkillRuntime) -> None:
    skill = SKILL_BY_NAME["event_understand"]
    inside = skill.invoke(runtime, {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS})
    later = skill.invoke(
        runtime,
        {"event_id": EVENT, "as_of": "2026-10-29T19:00:00+08:00", "caller_labels": LABELS},
    )
    assert inside.data["payload"]["warning_level"] == "orange"
    assert later.data["payload"]["warning_level"] is None, "14:00 之后的预警不得继续生效"
    assert later.data["payload"]["expired_evidence"] > inside.data["payload"]["expired_evidence"]


def test_missing_depth_marks_rules_undecided_instead_of_dropping_actions(tmp_path: Path) -> None:
    """深度缺失时，封路规则是"无法判定"，不是"不适用" —— 动作不能悄悄消失。"""
    root = _copy_dataset(tmp_path)
    orders = root / "work_orders.csv"
    text = orders.read_text(encoding="utf-8")
    orders.write_text(
        text.replace("积水深约 30cm，雨水口反涌", "路面积水，雨水口反涌，深度待勘"),
        encoding="utf-8",
    )

    report = SkillPipeline(SkillRuntime(root)).run(EVENT, AS_OF, caller_labels=LABELS)
    assert report.status == "partial"
    assert report.publishable is False
    assert report.facts is not None and report.facts.depth_cm is None
    assert "no_depth" in report.steps[0].degraded

    undecided = [
        stage
        for stage in (report.deduction.stages if report.deduction else [])
        if stage.stage == "action" and stage.status == "insufficient"
    ]
    assert undecided, "无法判定的处置规则必须显式出现"
    assert "RULE-ACT-CLOSE-ROAD" in undecided[0].conclusion["undecided"]
    assert "depth_cm" in undecided[0].conclusion["undecided"]
    assert not any(
        "封闭积水路段" in action
        for action in (report.deduction.action_order if report.deduction else [])
    ), "无法判定时不得虚构封路动作"


def test_audit_refuses_to_seal_when_a_rule_has_no_registry(runtime: SkillRuntime) -> None:
    """规则出处不可校验时，审计必须拒绝落账，而不是记一笔"看起来完整"的账。"""
    blind = SkillRuntime(ledger=build_ledger())
    result = SKILL_BY_NAME["evidence_audit"].invoke(
        blind, {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS}
    )
    assert result.status == "error"
    assert result.error is not None and result.error.code == ErrorCode.EVIDENCE_INSUFFICIENT.value
    assert result.data["rejected"], "被拒的 claim 必须点名"
    assert blind.ensure_ledger().records() == [], "审计不通过时不得写入账本"


def test_partial_never_carries_an_error_and_always_explains(tmp_path: Path) -> None:
    root = _copy_dataset(tmp_path)
    orders = root / "work_orders.csv"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace("积水深约 30cm，雨水口反涌", "积水，深度待勘"),
        encoding="utf-8",
    )
    result = SKILL_BY_NAME["event_understand"].invoke(
        SkillRuntime(root), {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS}
    )
    assert result.status == "partial"
    assert result.error is None, "信封规定 partial 不带 error"
    assert result.data["notice"] and result.data["degraded"]


# --- contract behaviour -------------------------------------------------


def test_invalid_request_is_rejected_before_any_work(runtime: SkillRuntime) -> None:
    missing = SKILL_BY_NAME["event_understand"].invoke(runtime, {"event_id": EVENT})
    assert missing.status == "error"
    assert missing.error is not None
    assert missing.error.code == ErrorCode.VALIDATION_FAILED.value

    naive = SKILL_BY_NAME["event_understand"].invoke(
        runtime, {"event_id": EVENT, "as_of": "2026-10-29T09:40:00"}
    )
    assert naive.status == "error"
    assert naive.error is not None and naive.error.code == ErrorCode.VALIDATION_FAILED.value


def test_impact_request_is_validated_from_a_base_request(runtime: SkillRuntime) -> None:
    """编排层只持有一个 SkillRequest；传给影响分析时要升格为带 max_hops 的请求。"""
    base = SkillRequest(event_id=EVENT, as_of=datetime.fromisoformat(AS_OF), caller_labels=LABELS)
    result = SKILL_BY_NAME["impact_analysis"].invoke(runtime, base)
    assert result.status == "ok"
    assert result.data["payload"]["max_hops"] == 3

    narrowed = SKILL_BY_NAME["impact_analysis"].invoke(runtime, ImpactRequest(**base.model_dump(), max_hops=1))
    assert narrowed.data["payload"]["max_hops"] == 1
    assert len(narrowed.data["payload"]["affected"]) < len(result.data["payload"]["affected"])


def test_deduction_is_computed_once_per_request(runtime: SkillRuntime) -> None:
    """三个 Skill 都问同一个推演，必须是同一个对象：否则"同一版本"无从谈起。"""
    first = runtime.deduce(EVENT, datetime.fromisoformat(AS_OF), LABELS)
    second = runtime.deduce(EVENT, datetime.fromisoformat(AS_OF), LABELS)
    assert first is second


def test_pipeline_is_deterministic(runtime: SkillRuntime) -> None:
    def flat(report: PipelineReport) -> dict:
        payload = report.model_dump(mode="json")
        for step in payload["steps"]:
            step.pop("elapsed_ms")
        return payload

    left = SkillPipeline(runtime).run(EVENT, AS_OF, caller_labels=LABELS, persist=False)
    right = SkillPipeline(SkillRuntime()).run(EVENT, AS_OF, caller_labels=LABELS, persist=False)
    assert flat(left) == flat(right)


def test_no_persist_still_audits_but_leaves_no_record(runtime: SkillRuntime) -> None:
    report = SkillPipeline(runtime).run(EVENT, AS_OF, caller_labels=LABELS, persist=False)
    assert report.audit is not None
    assert report.audit.rejected == 0
    assert report.audit.record_id is None
    assert report.audit.chain_ok is True


def test_skill_deduction_agrees_with_the_mcp_tool(runtime: SkillRuntime) -> None:
    """同一条链路，走 Skill 和走 MCP 必须给同一个结论；否则两个入口就是两个产品。"""
    toolkit = UrbanToolkit()
    served = toolkit.call(
        "event_deduce",
        {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS},
    )
    assert served.status == "ok"
    skill = SKILL_BY_NAME["cross_department_deduction"].invoke(
        runtime, {"event_id": EVENT, "as_of": AS_OF, "caller_labels": LABELS}
    )
    view = skill.data["payload"]
    assert view["snapshot_hash"] == served.data["snapshot_hash"]
    assert [item["conflict_id"] for item in view["conflicts"]] == [
        item["conflict_id"] for item in served.data["conflicts"]
    ]
    assert [item["gap"] for item in view["resource_gaps"]] == [
        item["gap"] for item in served.data["resource_gaps"]
    ]
    served_actions = sorted(
        (
            step
            for step in served.data["steps"]
            if step["stage"] == "action" and step["status"] == "supported"
        ),
        key=lambda step: (int(step["conclusion"].get("order", "99")), step["step_id"]),
    )
    assert view["action_order"] == [step["conclusion"]["action"] for step in served_actions]


def test_ledger_directory_round_trips(tmp_path: Path) -> None:
    root = _copy_dataset(tmp_path)
    ledger_dir = tmp_path / "ledger"
    report = SkillPipeline(SkillRuntime(root, ledger_dir=ledger_dir)).run(
        EVENT, AS_OF, caller_labels=LABELS
    )
    assert report.audit is not None and report.audit.record_id
    assert (ledger_dir / "records.jsonl").is_file()

    reloaded = ProvenanceLedger.load(
        ledger_dir,
        asset_fingerprints={
            asset.asset_id: asset.checksum_sha256
            for asset in SkillRuntime(root).assets.values()
        },
        knowledge_version="kb-v1.0.0",
    )
    assert reloaded.verify().ok
    assert [record.decision_id for record in reloaded.records()] == [report.audit.decision_id]


def test_manifest_is_stable_json() -> None:
    import json

    text = json.dumps(skill_manifest(), ensure_ascii=False, sort_keys=True)
    assert "event_understand" in text
    assert skill_manifest() == skill_manifest()
