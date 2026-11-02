"""真实用户评分采集：评分通道（只汇总）+ 修正通道（带建议分配才进门禁）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from decision_poc import review_collect as rc
from decision_poc import web_demo
from decision_poc.deduction import DistrictSignal

SIGNALS = [
    DistrictSignal(
        district="A-03",
        depth_cm=30.0,
        closure_required=True,
        has_hazard=True,
        demand=5,
        local_available=4,
    ),
    DistrictSignal(
        district="B-01",
        depth_cm=18.0,
        closure_required=False,
        has_hazard=False,
        demand=2,
        local_available=2,
    ),
]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "review_submissions.jsonl"
    monkeypatch.setenv(rc.SUBMISSIONS_ENV, str(path))
    return path


@pytest.fixture()
def gate_dir(tmp_path, monkeypatch):
    path = tmp_path / "evolution"
    monkeypatch.setenv(web_demo.EVOLUTION_DIR_ENV, str(path))
    return path


def submit(
    *,
    reviewer: str = "u1",
    phase: str = "before",
    scores: tuple[int, int, int, int] = (4, 3, 5, 4),
    suggested: dict[str, int] | None = None,
    comment: str = "",
    created_at: str = "2026-10-31T10:00:00+00:00",
):
    submission = rc.make_submission(
        reviewer_id=reviewer,
        reviewer_role="环境专业教师",
        phase=phase,
        scores=dict(zip(rc.SCORE_DIMENSIONS, scores)),
        policy_version="v1",
        comment=comment,
        suggested_allocation=suggested or {},
        signals=SIGNALS if suggested else [],
        current_allocation={"A-03": 4, "B-01": 2} if suggested else {},
        created_at=created_at,
    )
    rc.append_submission(submission)
    return submission


# --- 模型与落盘 ------------------------------------------------------------


def test_scores_mean_and_range() -> None:
    scores = rc.ReviewerScores(accuracy=4, timeliness=3, safety=5, explainability=4)
    assert scores.mean() == 4.0
    with pytest.raises(ValidationError):
        rc.ReviewerScores(accuracy=0, timeliness=3, safety=5, explainability=4)
    with pytest.raises(ValidationError):
        rc.ReviewerScores(accuracy=6, timeliness=3, safety=5, explainability=4)


def test_submission_id_is_deterministic() -> None:
    first = rc.make_submission(
        reviewer_id="u1", phase="before", scores={"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4}
    )
    again = rc.make_submission(
        reviewer_id="u1", phase="before", scores={"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4}
    )
    other = rc.make_submission(
        reviewer_id="u1", phase="after", scores={"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4}
    )
    assert first.submission_id == again.submission_id
    assert first.submission_id != other.submission_id
    assert first.submission_id.startswith("RV-")


def test_suggestion_requires_signals() -> None:
    with pytest.raises(ValidationError, match="signals"):
        rc.make_submission(
            reviewer_id="u1",
            phase="before",
            scores={"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4},
            suggested_allocation={"A-03": 5, "B-01": 1},
        )


def test_append_is_idempotent(store) -> None:
    first = submit()
    target, created = rc.append_submission(first)
    assert target == store
    assert created is False
    assert len(store.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_load_roundtrip_keeps_signals(store) -> None:
    submit(suggested={"A-03": 5, "B-01": 1})
    loaded = rc.load_submissions()
    assert len(loaded) == 1
    assert loaded[0].suggested_allocation == {"A-03": 5, "B-01": 1}
    assert loaded[0].signals and loaded[0].signals[0].district == "A-03"
    assert loaded[0].note == rc.REVIEWER_NOTE


# --- 汇总 ------------------------------------------------------------------


def test_summary_empty(store) -> None:
    summary = rc.summarize(rc.load_submissions())
    assert summary["total"] == 0
    assert summary["meets_min_reviewers"] is False
    assert summary["delta"] is None


def test_summary_before_after_and_min_reviewers(store) -> None:
    submit(reviewer="u1", phase="before", scores=(3, 3, 3, 3))
    submit(reviewer="u2", phase="after", scores=(4, 4, 4, 4))
    submit(reviewer="u3", phase="after", scores=(5, 5, 5, 5), suggested={"A-03": 5, "B-01": 1})
    summary = rc.summarize(rc.load_submissions())
    assert summary["total"] == 3
    assert summary["reviewer_count"] == 3
    assert summary["phases"]["before"]["count"] == 1
    assert summary["phases"]["after"]["count"] == 2
    assert summary["phases"]["before"]["overall"] == 3.0
    assert summary["phases"]["after"]["overall"] == 4.5
    assert summary["delta"] == 1.5
    assert summary["gate_candidates"] == 1
    assert summary["meets_min_reviewers"] is True
    assert summary["dimensions"]["accuracy"] == pytest.approx(4.0)


def test_summary_needs_both_phases(store) -> None:
    submit(reviewer="u1", phase="before")
    submit(reviewer="u2", phase="before")
    submit(reviewer="u3", phase="before")
    summary = rc.summarize(rc.load_submissions())
    assert summary["reviewer_count"] == 3
    assert summary["meets_min_reviewers"] is False  # 进化后没有样本


def test_render_markdown_contains_key_numbers(store) -> None:
    submit(reviewer="u1", phase="before", scores=(3, 3, 3, 3))
    submit(reviewer="u2", phase="after", scores=(4, 4, 4, 4))
    text = rc.render_markdown(rc.summarize(rc.load_submissions()))
    assert "真实用户评分汇总" in text
    assert "进化前" in text and "进化后" in text
    assert "+1.00" in text


# --- 转评审用例 ------------------------------------------------------------


def test_to_review_records_only_with_suggestion(store) -> None:
    submit(reviewer="u1", phase="before")  # 只评分，不进
    submit(reviewer="u2", phase="after", suggested={"A-03": 5, "B-01": 1}, comment="B-01 至少留 1 台")
    records = rc.to_review_records(rc.load_submissions())
    assert len(records) == 1
    record = records[0]
    assert record.decision.value == "modify"
    assert record.expected_allocation == {"A-03": 5, "B-01": 1}
    assert record.reviewer_note == rc.REVIEWER_NOTE
    assert "真实用户" in record.reviewer_role
    assert record.reason == "B-01 至少留 1 台"


def test_review_record_becomes_gate_case(store) -> None:
    submit(suggested={"A-03": 5, "B-01": 1})
    record = rc.to_review_records(rc.load_submissions())[0]
    case = record.to_case()
    assert case.split == "review"
    assert case.expect.allocation_by_district == {"A-03": 5, "B-01": 1}
    # A-03 是关键片区（封路 + 危险源）：期望 5 台全部满足，critical_unmet 为空
    assert case.expect.critical_unmet in ({}, {"A-03": 0})


def test_export_reviews(store, tmp_path) -> None:
    submit(suggested={"A-03": 5, "B-01": 1})
    target, count = rc.export_reviews(rc.load_submissions(), tmp_path / "gate_reviews.json")
    assert count == 1
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "decision-reviews-1"
    assert payload["reviews"][0]["review_id"].startswith("RVS-")


# --- CLI -------------------------------------------------------------------


def test_cli_add_summary_and_export(store, tmp_path, capsys) -> None:
    code = rc.main(
        [
            "add",
            "--reviewer",
            "u-cli",
            "--role",
            "市政从业者",
            "--phase",
            "before",
            "--scores",
            "3,3,4,3",
        ]
    )
    assert code == 0
    capsys.readouterr()
    assert rc.main(["add", "--reviewer", "u-cli", "--phase", "after", "--scores", "4,4,5,4"]) == 0
    capsys.readouterr()
    assert rc.main(["summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total"] == 2
    assert summary["delta"] == 1.0

    out = tmp_path / "no_gate.json"
    assert rc.main(["to-reviews", "--out", str(out)]) == 0
    capsys.readouterr()
    assert json.loads(out.read_text(encoding="utf-8"))["reviews"] == []


def test_cli_rejects_bad_scores(store) -> None:
    with pytest.raises(ValueError):
        rc.main(["add", "--reviewer", "u", "--phase", "before", "--scores", "6,3,3,3"])


# --- Web 路由 --------------------------------------------------------------


def _post(payload, path: str = "/api/reviews"):
    return web_demo.route(path, "POST", json.dumps(payload).encode("utf-8"))


def test_web_submit_and_summary(store) -> None:
    status, _ctype, body = _post(
        {
            "reviewer_id": "web-u1",
            "reviewer_role": "评委",
            "phase": "before",
            "scores": {"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4},
            "comment": "结论清楚",
        }
    )
    assert status == 201
    payload = json.loads(body)
    assert payload["created"] is True
    assert payload["gate_candidate"] is False
    assert payload["summary"]["total"] == 1

    status, _ctype, body = web_demo.route("/api/reviews")
    assert status == 200
    summary = json.loads(body)["summary"]
    assert summary["total"] == 1
    assert summary["reviewers"] == ["web-u1"]


def test_web_before_phase_does_not_run_evolution(store) -> None:
    status, _ctype, body = _post(
        {
            "reviewer_id": "web-before",
            "phase": "before",
            "scores": {"accuracy": 3, "timeliness": 3, "safety": 4, "explainability": 3},
        }
    )
    assert status == 201
    assert "evolution" not in json.loads(body)


def test_web_submit_with_suggestion_marks_gate_candidate(store, gate_dir) -> None:
    status, _ctype, body = _post(
        {
            "reviewer_id": "web-u2",
            "phase": "after",
            "scores": {"accuracy": 5, "timeliness": 4, "safety": 5, "explainability": 5},
            "suggested_allocation": "A-03=5,B-01=1",
        }
    )
    assert status == 201
    payload = json.loads(body)
    assert payload["gate_candidate"] is True
    assert payload["summary"]["gate_candidates"] == 1
    # 提交里带上决策输入快照，导出的评审用例才自包含
    loaded = rc.load_submissions()
    assert loaded[0].signals
    # 「进化后」评分会顺手重跑一次进化门禁，把候选采纳/拒绝结果带回页面
    evolution = payload["evolution"]
    assert evolution["eligible"] is True
    assert evolution["final_label"].startswith("flood-response-")
    assert evolution["review_ids"] and evolution["review_ids"][0].startswith("RVS-")
    assert evolution["rounds"] >= 1
    assert evolution["train_deviation"][1] <= evolution["train_deviation"][0]
    # 门禁结果落盘（latest.json + history.jsonl），并可通过 /api/evolution/gate 读回
    assert evolution["saved_to"] and Path(evolution["saved_to"]).is_file()
    assert (gate_dir / "latest.json").is_file()
    assert (gate_dir / "history.jsonl").is_file()
    status, _ctype, body = web_demo.route("/api/evolution/gate")
    assert status == 200
    gate = json.loads(body)
    assert gate["total_entries"] == 1
    assert gate["latest"]["final_label"] == evolution["final_label"]
    assert gate["latest"]["saved_at"]


def test_web_submit_rejects_bad_input(store) -> None:
    status, _ctype, _body = web_demo.route("/api/reviews", "POST", b"{not-json")
    assert status == 400

    status, _ctype, _body = _post({"phase": "before", "scores": {"accuracy": 4, "timeliness": 3, "safety": 5, "explainability": 4}})
    assert status == 400

    status, _ctype, _body = _post(
        {
            "reviewer_id": "u",
            "phase": "before",
            "scores": {"accuracy": 9, "timeliness": 3, "safety": 5, "explainability": 4},
        }
    )
    assert status == 400
