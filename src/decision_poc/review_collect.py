"""真实用户评分采集：把「角色扮演评审」升级为可采集的真实反馈通道。

为什么单独一个模块
------------------
P9.1 的评审是**角色扮演**（`decision_reviews.json` 里明确标注），它能证明"第二反馈
通道"的机制成立，但不能回答"谁评的、样本多少、评分分布"。赛题把"用户评分"列为
合法反馈来源之一，缺的是**可采集、可汇总、可复核**的真实评分记录。

本模块给两条通道，口径严格分开：

1. **评分通道**：4 维（准确性/时效性/安全性/可解释性）1–5 分 + 意见。
   只做汇总统计（样本数、均值、进化前后差值、分布），**不参与门禁**——
   分数不能自动复算成"候选策略对不对"，不能拿来当硬约束。
2. **修正通道**：用户在评分时可选给出"建议分配"（哪个片区几台）。给出修正时，
   服务器会把当时的决策输入快照（`DistrictSignal`）一起存进提交记录，转成
   `ReviewRecord(MODIFY)` → 评审用例 → 门禁硬约束（与 P9.1 同一条路，可断言）。

诚实边界：
- 真实用户评分是**个人意见**，不是部门批复；提交记录里 `note` 固定写口径。
- 评分本身是主观信号，不能宣称"策略变得更优是因为评分"；只能说"候选策略不得
  违反带修正建议的用户评分"。
- 数据落盘为 JSONL（追加式，不覆盖历史），可直接被 `to-reviews` 导出成
  `decision_reviews.json` 同构文件，交给 P9 的评测门禁消费。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import Field, model_validator

from .contracts import StrictModel
from .deduction import DistrictSignal
from .experience import REVIEW_SCHEMA_VERSION, ReviewDecision, ReviewRecord

SUBMISSION_SCHEMA_VERSION = "decision-review-submissions-1"
DEFAULT_EVENT_ID = "WO-20261029-007"
DEFAULT_AS_OF = "2026-10-29T09:40:00+08:00"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBMISSIONS_PATH = REPO_ROOT / "samples" / "eval" / "review_submissions.jsonl"
SUBMISSIONS_ENV = "URBAN_TWIN_REVIEWS"

SCORE_DIMENSIONS: tuple[str, ...] = ("accuracy", "timeliness", "safety", "explainability")
DIMENSION_LABELS: dict[str, str] = {
    "accuracy": "结论准确性",
    "timeliness": "时效性",
    "safety": "安全性/风险提示",
    "explainability": "可解释性/可溯源",
}
PHASES: tuple[str, ...] = ("before", "after")
REVIEWER_NOTE = "真实用户评分（个人意见，非部门批复）"
MIN_REVIEWERS = 3


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# --- 模型 -----------------------------------------------------------------


class ReviewerScores(StrictModel):
    """四维 1–5 分。整数分，避免"4.7 分"这种不可复核的小数。"""

    accuracy: int = Field(ge=1, le=5)
    timeliness: int = Field(ge=1, le=5)
    safety: int = Field(ge=1, le=5)
    explainability: int = Field(ge=1, le=5)

    def mean(self) -> float:
        return round(sum(getattr(self, name) for name in SCORE_DIMENSIONS) / len(SCORE_DIMENSIONS), 6)

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in SCORE_DIMENSIONS}


class ReviewSubmission(StrictModel):
    """一次真实用户评分提交（自包含：含评分、意见、当时的决策输入快照）。"""

    schema_version: str = SUBMISSION_SCHEMA_VERSION
    submission_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1, max_length=64)
    reviewer_role: str = Field(default="未填写", max_length=64)
    phase: Literal["before", "after"]
    event_id: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    scores: ReviewerScores
    comment: str = Field(default="", max_length=500)
    # 可选：用户给出的修正分配（谁拿几台）。给修正必须带 signals，否则评审用例无法自包含。
    suggested_allocation: dict[str, int] = Field(default_factory=dict)
    signals: list[DistrictSignal] = Field(default_factory=list)
    # 提交时看到的分配方案，便于事后核对"用户是在评哪一版"
    current_allocation: dict[str, int] = Field(default_factory=dict)
    source: Literal["web", "cli"] = "web"
    note: str = REVIEWER_NOTE

    @model_validator(mode="after")
    def _validate_suggestion(self) -> "ReviewSubmission":
        for district, qty in self.suggested_allocation.items():
            if not district.strip():
                raise ValueError("建议分配的片区名不能为空")
            if qty < 0:
                raise ValueError("建议分配的台数不能为负")
        if self.suggested_allocation and not self.signals:
            raise ValueError("给出修正分配时必须带决策输入快照 signals（评审用例要自包含）")
        return self

    @property
    def overall(self) -> float:
        return self.scores.mean()


def make_submission(
    *,
    reviewer_id: str,
    phase: str,
    scores: ReviewerScores | Mapping[str, int],
    reviewer_role: str = "未填写",
    policy_version: str = "v1",
    event_id: str = DEFAULT_EVENT_ID,
    as_of: str = DEFAULT_AS_OF,
    comment: str = "",
    suggested_allocation: Mapping[str, int] | None = None,
    signals: Sequence[DistrictSignal] | None = None,
    current_allocation: Mapping[str, int] | None = None,
    source: str = "web",
    created_at: str | None = None,
) -> ReviewSubmission:
    """构造提交。submission_id 由内容哈希得出：同内容重复提交幂等。"""
    parsed_scores = scores if isinstance(scores, ReviewerScores) else ReviewerScores.model_validate(scores)
    payload = {
        "reviewer_id": reviewer_id,
        "reviewer_role": reviewer_role,
        "phase": phase,
        "policy_version": policy_version,
        "event_id": event_id,
        "scores": parsed_scores.as_dict(),
        "comment": comment,
        "suggested_allocation": dict(suggested_allocation or {}),
    }
    return ReviewSubmission(
        submission_id="RV-" + _digest(payload)[:12],
        created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
        phase=phase,  # type: ignore[arg-type]
        event_id=event_id,
        as_of=as_of,
        policy_version=policy_version,
        scores=parsed_scores,
        comment=comment,
        suggested_allocation=dict(suggested_allocation or {}),
        signals=list(signals or []),
        current_allocation=dict(current_allocation or {}),
        source=source,  # type: ignore[arg-type]
    )


# --- 存取（JSONL，追加式） -------------------------------------------------


def submissions_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(SUBMISSIONS_ENV)
    return Path(override) if override else DEFAULT_SUBMISSIONS_PATH


def load_submissions(path: Path | str | None = None) -> list[ReviewSubmission]:
    target = submissions_path(path)
    if not target.is_file():
        return []
    records: list[ReviewSubmission] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(ReviewSubmission.model_validate(json.loads(line)))
    return records


def append_submission(
    submission: ReviewSubmission, path: Path | str | None = None
) -> tuple[Path, bool]:
    """追加一条提交；同 submission_id 视为重复提交，不重复写。"""
    target = submissions_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = {item.submission_id for item in load_submissions(target)}
    if submission.submission_id in existing:
        return target, False
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(submission.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return target, True


# --- 汇总 -----------------------------------------------------------------


def _phase_stats(items: Sequence[ReviewSubmission]) -> dict[str, Any]:
    if not items:
        return {"count": 0, "overall": None, "dimensions": {}}
    dimensions = {
        name: round(sum(item.scores.as_dict()[name] for item in items) / len(items), 4)
        for name in SCORE_DIMENSIONS
    }
    return {
        "count": len(items),
        "overall": round(sum(item.overall for item in items) / len(items), 4),
        "dimensions": dimensions,
    }


def summarize(submissions: Sequence[ReviewSubmission]) -> dict[str, Any]:
    """汇总评分。不参与门禁；只回答"样本多少、谁评的、前后差多少"。"""
    total = len(submissions)
    if total == 0:
        return {
            "total": 0,
            "reviewers": [],
            "reviewer_count": 0,
            "phases": {"before": _phase_stats([]), "after": _phase_stats([])},
            "dimensions": {},
            "delta": None,
            "distribution": {},
            "comment_count": 0,
            "gate_candidates": 0,
            "meets_min_reviewers": False,
            "latest_at": None,
            "path": str(submissions_path()),
            "dimension_labels": dict(DIMENSION_LABELS),
            "note": REVIEWER_NOTE,
        }
    reviewers = sorted({item.reviewer_id for item in submissions})
    before = [item for item in submissions if item.phase == "before"]
    after = [item for item in submissions if item.phase == "after"]
    before_stats, after_stats = _phase_stats(before), _phase_stats(after)
    delta = None
    if before_stats["overall"] is not None and after_stats["overall"] is not None:
        delta = round(after_stats["overall"] - before_stats["overall"], 4)
    distribution: dict[str, int] = {}
    for item in submissions:
        bucket = str(int(round(item.overall)))
        distribution[bucket] = distribution.get(bucket, 0) + 1
    all_dims = {
        name: round(sum(item.scores.as_dict()[name] for item in submissions) / total, 4)
        for name in SCORE_DIMENSIONS
    }
    return {
        "total": total,
        "reviewers": reviewers,
        "reviewer_count": len(reviewers),
        "phases": {"before": before_stats, "after": after_stats},
        "dimensions": all_dims,
        "delta": delta,
        "distribution": dict(sorted(distribution.items())),
        "comment_count": sum(1 for item in submissions if item.comment.strip()),
        "gate_candidates": sum(1 for item in submissions if item.suggested_allocation),
        "meets_min_reviewers": len(reviewers) >= MIN_REVIEWERS and bool(before) and bool(after),
        "latest_at": max(item.created_at for item in submissions),
        "path": str(submissions_path()),
        "dimension_labels": dict(DIMENSION_LABELS),
        "note": REVIEWER_NOTE,
    }


# --- 转评审用例（只有带修正建议的提交才进这道门） --------------------------


def to_review_records(submissions: Iterable[ReviewSubmission]) -> list[ReviewRecord]:
    records: list[ReviewRecord] = []
    for item in submissions:
        if not item.suggested_allocation or not item.signals:
            continue
        dims = "、".join(
            f"{DIMENSION_LABELS[name]} {value}" for name, value in item.scores.as_dict().items()
        )
        records.append(
            ReviewRecord(
                review_id=f"RVS-{item.submission_id[3:]}",
                reviewer_role=f"真实用户（{item.reviewer_role}）",
                reviewer_note=REVIEWER_NOTE,
                event_id=item.event_id,
                as_of=item.as_of,
                policy_version_reviewed=item.policy_version,
                decision=ReviewDecision.MODIFY,
                reason=item.comment.strip() or "用户建议调整分配",
                signals=list(item.signals),
                expected_allocation=dict(item.suggested_allocation),
                basis=[
                    f"真实用户评分 {item.overall}/5（{dims}）",
                    f"提交 {item.submission_id}（{item.reviewer_id}，{item.phase}）",
                    f"提交时分配 {json.dumps(item.current_allocation, ensure_ascii=False)}",
                ],
            )
        )
    return records


def export_reviews(
    submissions: Iterable[ReviewSubmission], path: Path | str
) -> tuple[Path, int]:
    """导出成 decision_reviews.json 同构文件，可直接交给 P9 门禁消费。"""
    records = to_review_records(submissions)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "note": "由真实用户评分提交导出；只有带修正建议的提交会进入门禁",
                "reviews": [record.model_dump(mode="json") for record in records],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target, len(records)


def render_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# 真实用户评分汇总",
        "",
        f"- 样本数：{summary.get('total', 0)}（评审人 {summary.get('reviewer_count', 0)} 位）",
        f"- 满足最低样本（≥{MIN_REVIEWERS} 人且进化前后各有样本）："
        f"{'是' if summary.get('meets_min_reviewers') else '否'}",
        f"- 口径：{summary.get('note')}",
        "",
    ]
    before = (summary.get("phases") or {}).get("before") or {}
    after = (summary.get("phases") or {}).get("after") or {}
    lines.append("| 阶段 | 样本 | 综合均分 |")
    lines.append("| --- | --- | --- |")
    for label, stats in (("进化前", before), ("进化后", after)):
        lines.append(f"| {label} | {stats.get('count', 0)} | {stats.get('overall') or '-'} |")
    delta = summary.get("delta")
    if delta is not None:
        lines.append(f"\n- 进化前后差值：**{delta:+.2f}**")
    lines.append(f"- 带修正建议（可进门的候选）：{summary.get('gate_candidates', 0)} 条")
    if summary.get("comment_count"):
        lines.append(f"- 有文字意见：{summary.get('comment_count')} 条")
    dimension_labels = summary.get("dimension_labels") or {}
    if summary.get("dimensions"):
        lines.append("\n| 维度 | 均分 |")
        lines.append("| --- | --- |")
        for name, value in summary["dimensions"].items():
            lines.append(f"| {dimension_labels.get(name, name)} | {value} |")
    return "\n".join(lines) + "\n"


# --- CLI ------------------------------------------------------------------


def parse_scores(text: str) -> ReviewerScores:
    parts = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
    if len(parts) != len(SCORE_DIMENSIONS):
        raise ValueError(f"评分需要 {len(SCORE_DIMENSIONS)} 个整数（准确性,时效性,安全性,可解释性）")
    values = [int(part) for part in parts]
    return ReviewerScores.model_validate(dict(zip(SCORE_DIMENSIONS, values)))


def parse_allocation(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in text.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"分配格式应为 片区=台数，收到 {chunk!r}")
        district, qty = chunk.split("=", 1)
        result[district.strip()] = int(qty.strip())
    return result


def _snapshot_facts(policy_version: str) -> tuple[list[DistrictSignal], dict[str, int]]:
    """从当前决策跑一遍，取决策输入快照与当前分配（只读，不落作业）。"""
    from . import visualize

    policy = None
    if policy_version.lower() not in {"", "v1"}:
        policy = {"policy_version": policy_version}
    snapshot = visualize.build_visual_snapshot(DEFAULT_EVENT_ID, DEFAULT_AS_OF, policy=policy)
    lines = (snapshot.allocation or {}).get("lines", [])
    signals = [
        DistrictSignal(
            district=str(item["district"]),
            depth_cm=item.get("depth_cm"),
            closure_required=item.get("closure_required"),
            has_hazard=bool(item.get("has_hazard")),
            demand=int(item.get("demand", 0)),
            local_available=int(item.get("local_available", 0)),
        )
        for item in lines
    ]
    current = {str(item["district"]): int(item.get("allocated", 0)) for item in lines}
    return signals, current


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实用户评分采集（评分通道 + 修正通道）")
    parser.add_argument("--path", default=None, help="提交文件路径（默认 samples/eval/review_submissions.jsonl）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="追加一条真实评分")
    add.add_argument("--reviewer", required=True)
    add.add_argument("--role", default="未填写")
    add.add_argument("--phase", required=True, choices=list(PHASES))
    add.add_argument("--scores", required=True, help="准确性,时效性,安全性,可解释性（各 1-5）")
    add.add_argument("--comment", default="")
    add.add_argument("--suggested", default="", help="可选修正分配，如 A-03=5,B-01=1")
    add.add_argument("--policy", default="v1")

    lst = sub.add_parser("list", help="列出已采集的评分")
    lst.add_argument("--limit", type=int, default=20)

    sm = sub.add_parser("summary", help="汇总评分")
    sm.add_argument("--markdown", action="store_true")

    ex = sub.add_parser("to-reviews", help="导出成 decision_reviews.json 同构文件（仅供门禁消费）")
    ex.add_argument("--out", default=str(REPO_ROOT / "samples" / "eval" / "review_gate_reviews.json"))

    args = parser.parse_args(argv)

    if args.cmd == "add":
        suggested = parse_allocation(args.suggested) if args.suggested else {}
        signals: list[DistrictSignal] = []
        current: dict[str, int] = {}
        if suggested:
            signals, current = _snapshot_facts(args.policy)
        submission = make_submission(
            reviewer_id=args.reviewer,
            reviewer_role=args.role,
            phase=args.phase,
            scores=parse_scores(args.scores),
            policy_version=args.policy,
            comment=args.comment,
            suggested_allocation=suggested,
            signals=signals,
            current_allocation=current,
            source="cli",
        )
        target, created = append_submission(submission, args.path)
        print(
            json.dumps(
                {
                    "status": "created" if created else "duplicate",
                    "submission_id": submission.submission_id,
                    "path": str(target),
                    "overall": submission.overall,
                    "gate_candidate": bool(suggested),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "list":
        items = load_submissions(args.path)
        payload = [
            {
                "submission_id": item.submission_id,
                "reviewer": item.reviewer_id,
                "phase": item.phase,
                "overall": item.overall,
                "policy": item.policy_version,
                "comment": item.comment,
                "suggested": item.suggested_allocation,
                "created_at": item.created_at,
            }
            for item in items[-args.limit :]
        ]
        print(json.dumps({"total": len(items), "items": payload}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "summary":
        summary = summarize(load_submissions(args.path))
        print(render_markdown(summary) if args.markdown else json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "to-reviews":
        target, count = export_reviews(load_submissions(args.path), args.out)
        print(json.dumps({"out": str(target), "reviews": count}, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
