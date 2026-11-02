from pathlib import Path

import pytest

from decision_poc.llm_ab import (
    build_task,
    render_markdown,
    rule_reference,
    score_decision,
    scripted_floor_output,
    evaluate_cases,
)
from decision_poc.skills import SkillPipeline, SkillRuntime
from decision_poc.urban_eval import load_cases

CASES = {case.case_id: case for case in load_cases()}


@pytest.fixture(scope="module")
def runtime() -> SkillRuntime:
    return SkillRuntime()


@pytest.fixture(scope="module")
def normal_task(runtime: SkillRuntime):
    case = CASES["N01"]
    report = SkillPipeline(runtime).run(
        case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
    )
    return case, report, build_task(case, report)


@pytest.fixture(scope="module")
def refused_task(runtime: SkillRuntime):
    case = CASES["I01"]
    report = SkillPipeline(runtime).run(
        case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
    )
    return case, report, build_task(case, report)


def test_task_options_are_deterministic_and_shuffled(normal_task):
    case, report, task = normal_task
    again = build_task(case, report)

    assert [item["option_id"] for item in task.options] == [
        item["option_id"] for item in again.options
    ]
    assert task.expected_option_id in task.option_ids()
    assert task.request.request_id == f"req-ab-{case.case_id}"
    assert len(task.options) >= 1
    assert task.evidence


def test_rule_reference_hits_its_own_expectation(normal_task):
    _, _, task = normal_task

    sample = score_decision(task, "rule", rule_reference(task), elapsed_ms=0.5)

    assert sample.schema_valid is True
    assert sample.request_id_match is True
    assert sample.option_id_valid is True
    assert sample.selected_match is True
    assert sample.evidence_valid == sample.evidence_total
    assert sample.evidence_total >= 1


def test_floor_baseline_is_schema_valid_but_has_no_reasoning(normal_task):
    _, _, task = normal_task

    sample = score_decision(task, "test", scripted_floor_output(task), elapsed_ms=0.1)

    assert sample.schema_valid is True
    assert sample.option_id_valid is True
    assert sample.selected_option_id == task.options[0]["option_id"]


def test_refused_case_expects_refusal_option(refused_task):
    _, report, task = refused_task

    assert task.expected_kind == "refuse"
    assert "拒答" in next(
        item["description"]
        for item in task.options
        if item["option_id"] == task.expected_option_id
    )


def test_score_detects_wrong_selection(normal_task):
    _, _, task = normal_task
    wrong = next(
        item["option_id"]
        for item in task.options
        if item["option_id"] != task.expected_option_id
    )
    decision = rule_reference(task).model_copy(update={"selected_option_id": wrong})

    sample = score_decision(task, "rule", decision, elapsed_ms=1.0)

    assert sample.selected_match is False
    assert sample.option_id_valid is True


def test_offline_evaluation_covers_all_cases():
    report = evaluate_cases("test", limit=3)

    metrics = report.metrics()
    assert metrics["cases_total"] == 3
    assert metrics["schema_valid"] == 3
    assert metrics["request_id_match"] == 3
    assert metrics["errors"] == 0


def test_markdown_report_has_summary_and_cases():
    rule = evaluate_cases("rule", limit=2)
    floor = evaluate_cases("test", limit=2)

    markdown = render_markdown([rule, floor])

    assert "## 汇总" in markdown
    assert "## 逐用例对照" in markdown
    assert "rule" in markdown and "test" in markdown
