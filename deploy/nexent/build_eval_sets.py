"""把冻结 12 例分配用例转成 Nexent 评测集 case 结构。

用途
----
`samples/eval/allocation_cases.json`（冻结 ground truth，12 例 = 8 train + 4 holdout）
是 P9 进化层的评测基准。本脚本把每例转成 Nexent 评测集的一条 case：

    inputs.query  = {"case_id": ..., "policy": "v1|v1.2", "signals": [...]}
    label.answer  = {"allocation_by_district": ..., "transfers": [...],
                     "priority_order": [...], "critical_unmet": {...}}

配套的 code 评估器（alloc_deviation_evaluator.py）从 query 里读 `policy` 决定用哪个
策略版本复刻 decide()，从 label.answer 里读 ground truth 算偏差。

产出两种评测集：
1. `allocation_v1`  —— 12 例全部 policy=v1   （进化前基线）
2. `allocation_v12` —— 12 例全部 policy=v1.2 （进化后）

两个评测集各跑一次 run，stats 对比即"进化前后对比"。

只读不写 samples/：本脚本不修改冻结资产，只在 dist/ 下产出转换结果。
"""

from __future__ import annotations

import json
from pathlib import Path

CASES_PATH = Path(__file__).resolve().parents[2] / "samples" / "eval" / "allocation_cases.json"
OUT_DIR = Path(__file__).resolve().parents[2] / "dist" / "nexent-eval"


def build_cases(policy: str) -> list[dict]:
    """把 12 例转成 Nexent 评测集 case 列表（指定策略版本）。"""
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    out: list[dict] = []
    for item in raw["cases"]:
        case_id = item["case_id"]
        expect = item["expect"]
        query_obj = {
            "case_id": case_id,
            "title": item["title"],
            "split": item["split"],
            "policy": policy,
            "signals": item["signals"],
        }
        label_obj = {
            "allocation_by_district": expect["allocation_by_district"],
            "transfers": expect["transfers"],
            "priority_order": expect["priority_order"],
            "critical_unmet": expect["critical_unmet"],
        }
        out.append(
            {
                "inputs": {"query": json.dumps(query_obj, ensure_ascii=False, sort_keys=True)},
                "label": {"answer": json.dumps(label_obj, ensure_ascii=False, sort_keys=True)},
            }
        )
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for policy, name in (("v1", "allocation_v1"), ("v1.2", "allocation_v12")):
        cases = build_cases(policy)
        path = OUT_DIR / f"{name}.json"
        path.write_text(
            json.dumps({"cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        train = sum(1 for c in cases if '"split": "train"' in c["inputs"]["query"])
        holdout = len(cases) - train
        print(f"[{name}] {len(cases)} 例（train {train} / holdout {holdout}）→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
