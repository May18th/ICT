"""进化 A/B 外部可复现实验：同序列固定策略 vs 进化策略。

回答审计（docs/competition/winning-gap-audit.md §3-2）的提问：
"你的'可进化'怎么证明不是自己和自己比？"

一条命令跑完三件事，全部离线、确定性、无随机：

1. **A/B 对照**：同一分配请求序列（train + holdout 全部用例）分别交给
   固定策略（v1，全程不更新）与进化策略（同一门禁链路跑完后的最终版本），
   逐用例输出分配向量与偏差对照。
2. **快照哈希**：两个策略参数做 sha256（规范化 JSON），发布历史里每次
   采纳的参数增量入账——对照表不会被事后改写。
3. **拒绝/回滚反例**：从同一份进化报告里取门禁拒绝轮与回滚检查，
   证明"进化"带有刹车：候选不达标会被拒，误升级可逐位回滚。

用法：

    python scripts/evolution_ab.py                      # 结果打印到 stdout
    python scripts/evolution_ab.py --out out/evolution_ab.json

不写 samples/eval/（那里的冻结口径由 evidence_check 管），不注册新测试
（测试总数是冻结值），纯只读仓库数据 + 临时目录记忆。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from decision_poc.allocation import evaluate_policy, load_cases  # noqa: E402
from decision_poc.experience import load_reviews  # noqa: E402
from decision_poc.policy_evolution import run_showcase  # noqa: E402


def _params_hash(policy) -> str:
    """策略参数快照哈希：排除版本号字段，只哈希参数本体。"""
    payload = json.dumps(
        policy.model_dump(exclude={"policy_version"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run(*, max_rounds: int = 4) -> dict:
    cases = load_cases()
    reviews = load_reviews()
    # 记忆写到临时目录：A/B 必须从零开始，不沾上次演示的 warm start
    with tempfile.TemporaryDirectory(prefix="evolution-ab-") as tmp:
        showcase = run_showcase(
            cases,
            reviews,
            memory_path=Path(tmp) / "memory.json",
            reuse_memory=False,
            max_rounds=max_rounds,
            include_real_event=True,
        )
    report = showcase.first

    all_cases = list(cases)
    fixed_eval = evaluate_policy(report.initial_policy, all_cases)
    evolved_eval = evaluate_policy(report.final_policy, all_cases)

    fixed_by_id = {item.case_id: item for item in fixed_eval.case_results}
    rows = []
    for item in evolved_eval.case_results:
        base = fixed_by_id[item.case_id]
        rows.append(
            {
                "case_id": item.case_id,
                "split": item.split,
                "deviation_fixed": round(base.deviation, 6),
                "deviation_evolved": round(item.deviation, 6),
                "improved": item.deviation < base.deviation - 1e-9,
                "regressed": item.deviation > base.deviation + 1e-9,
                "allocation_fixed": base.allocation,
                "allocation_evolved": item.allocation,
                "expected_allocation": item.expected_allocation,
            }
        )

    rejected = [
        {
            "round": item.round,
            "candidate_version": item.candidate_version,
            "params_delta": item.params_delta,
            "reason": item.reason,
        }
        for item in report.rounds
        if item.action == "rejected"
    ]

    return {
        "schema_version": "evolution-ab-1",
        "cases_digest": report.case_digest,
        "sequence_size": len(all_cases),
        "arms": {
            "fixed": {
                "policy_version": report.initial_policy.policy_version,
                "params_hash": _params_hash(report.initial_policy),
                "deviation_total": round(fixed_eval.deviation, 6),
                "critical_unmet_units": fixed_eval.critical_unmet_units,
                "missed_transfers": fixed_eval.missed_transfers,
            },
            "evolved": {
                "policy_version": report.final_policy.policy_version,
                "label": report.final_label,
                "params_hash": _params_hash(report.final_policy),
                "deviation_total": round(evolved_eval.deviation, 6),
                "critical_unmet_units": evolved_eval.critical_unmet_units,
                "missed_transfers": evolved_eval.missed_transfers,
            },
        },
        "per_case": rows,
        "releases": [
            {
                "label": item.label,
                "version_id": item.version_id,
                "parent_label": item.parent_label,
                "trigger_feedback_id": item.trigger_feedback_id,
                "params_delta": item.params_delta,
                "train_deviation_after": item.train_deviation_after,
            }
            for item in report.releases
        ],
        "rejected_rounds": rejected,
        "rollback": report.rolled_back.model_dump() if report.rolled_back else None,
        "real_event": {
            "event_id": report.real_event_evolved.event_id if report.real_event_evolved else None,
            "allocation_fixed": (
                report.real_event_baseline.allocation_by_district
                if report.real_event_baseline
                else None
            ),
            "allocation_evolved": (
                report.real_event_evolved.allocation_by_district
                if report.real_event_evolved
                else None
            ),
        },
    }


def render(result: dict) -> str:
    arms = result["arms"]
    lines = [
        "# 进化 A/B 外部复现（同序列：固定策略 vs 进化策略）",
        "",
        f"请求序列：{result['sequence_size']} 条分配用例，指纹 {result['cases_digest'][:16]}…",
        "",
        "| 臂 | 版本 | 参数快照哈希 | 综合偏差 | 关键片区缺台 | 应调未调 |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| A 固定 | {arms['fixed']['policy_version']} | `{arms['fixed']['params_hash'][:16]}` "
        f"| {arms['fixed']['deviation_total']:.4f} | {arms['fixed']['critical_unmet_units']} "
        f"| {arms['fixed']['missed_transfers']} |",
        f"| B 进化 | {arms['evolved']['label']} | `{arms['evolved']['params_hash'][:16]}` "
        f"| {arms['evolved']['deviation_total']:.4f} | {arms['evolved']['critical_unmet_units']} "
        f"| {arms['evolved']['missed_transfers']} |",
        "",
        "| 用例 | 分区 | 偏差(固定) | 偏差(进化) | 判定 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in result["per_case"]:
        verdict = "改善" if row["improved"] else ("退化" if row["regressed"] else "持平")
        lines.append(
            f"| {row['case_id']} | {row['split']} "
            f"| {row['deviation_fixed']:.4f} | {row['deviation_evolved']:.4f} | {verdict} |"
        )
    lines += ["", "## 发布历史（每次采纳的参数增量）"]
    for item in result["releases"]:
        delta = json.dumps(item["params_delta"], ensure_ascii=False, sort_keys=True)
        lines.append(
            f"- {item['label']}（← {item['parent_label']}，触发 {item['trigger_feedback_id']}，"
            f"采纳后训练偏差 {item['train_deviation_after']:.4f}）：`{delta}`"
        )
    lines += ["", "## 拒绝/回滚反例"]
    for item in result["rejected_rounds"]:
        delta = json.dumps(item["params_delta"], ensure_ascii=False, sort_keys=True)
        lines.append(f"- 第 {item['round']} 轮候选被拒：`{delta}`——理由：{item['reason']}")
    rb = result["rollback"]
    if rb:
        lines.append(
            f"- 回滚检查：滚回 {rb['rolled_back_to']} 后偏差 {rb['deviation_after_rollback']:.4f}"
            f"（初始 {rb['initial_deviation']:.4f}，逐位一致：{rb['matches_initial']}）"
        )
    else:
        lines.append("- 本轮无回滚检查记录")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="机器可读结果 JSON 输出路径")
    parser.add_argument("--max-rounds", type=int, default=4)
    args = parser.parse_args(argv)

    result = run(max_rounds=args.max_rounds)
    text = render(result)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写出 {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
