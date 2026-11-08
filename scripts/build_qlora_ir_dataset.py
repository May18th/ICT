# -*- coding: utf-8 -*-
"""把 allocation 用例转成 IR 训练集（模型只学离散判定，不学算术）。

与 `build_qlora_dataset.py` 的区别：标签不是完整 expect，而是
`qlora_ir.ir_from_decision()` 产出的中间表示——关键集、优先序、调拨方向，
**不含任何台数**。

用法：

    # 只用生成集（与 300 步实验保持同一训练数据，单独换任务形式）
    python scripts/build_qlora_ir_dataset.py --out dist/qlora/ir_gen

    # 生成集 + 冻结 8 例 train（补目标分布的措辞）
    python scripts/build_qlora_ir_dataset.py --frozen-train --out dist/qlora/ir_mix
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.deduction import DistrictSignal  # noqa: E402
from qlora_ir import FROZEN, ir_from_case, self_check  # noqa: E402

PROMPT_PREFIX = "请判断各片区关键状态与优先序，并给出跨区调拨方向（台数由系统计算）："


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def to_row(case: dict) -> dict:
    signals = [DistrictSignal(**s) for s in case["signals"]]
    ir = ir_from_case(signals)
    payload = {
        "title": case["title"],
        "signals": case["signals"],
        "basis": case["basis"],
    }
    return {
        "case_id": case["case_id"],
        "messages": [
            {"role": "user", "content": PROMPT_PREFIX + json.dumps(payload, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(ir, ensure_ascii=False, sort_keys=True)},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "dist/qlora/allocation_cases_gen.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--frozen-train",
        action="store_true",
        help="额外并入冻结集的 train split（8 例），补目标分布的措辞",
    )
    args = parser.parse_args()

    self_check(verbose=False)

    cases = load_cases(args.source)
    if args.frozen_train:
        cases = cases + [c for c in load_cases(FROZEN) if c.get("split") == "train"]

    args.out.mkdir(parents=True, exist_ok=True)
    handles: dict[str, object] = {}
    counts: dict[str, int] = {}
    try:
        for case in cases:
            split = case.get("split", "train")
            if split not in handles:
                handles[split] = (args.out / f"{split}.jsonl").open("w", encoding="utf-8")
            handles[split].write(json.dumps(to_row(case), ensure_ascii=False) + "\n")
            counts[split] = counts.get(split, 0) + 1
    finally:
        for handle in handles.values():
            handle.close()

    detail = "，".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"写入 {sum(counts.values())} 条（{detail}）-> {args.out}")


if __name__ == "__main__":
    main()
