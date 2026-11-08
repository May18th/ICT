"""Build a small supervised QLoRA dataset from allocation evaluation cases.

默认读冻结评测集 `samples/eval/allocation_cases.json`（12 例），输出到 `dist/qlora/`。

扩容实验用 `--source dist/qlora/allocation_cases_gen.json --out dist/qlora/gen`：
输出目录必须换掉，否则会覆盖 24 步训练用的 `train.jsonl` / `holdout.jsonl`，
那不是"重新生成"，是把已冻结的对照实验数据抹掉。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "samples/eval/allocation_cases.json"
OUT = ROOT / "dist/qlora"

PROMPT_PREFIX = "请根据城市内涝资源信号给出可审计的调度结论："


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    if args.source.resolve() != SOURCE.resolve() and args.out.resolve() == OUT.resolve():
        raise SystemExit(
            "换数据源时 --out 必须换目录（例如 dist/qlora/gen），"
            "否则会覆盖冻结对照实验的 train.jsonl / holdout.jsonl"
        )

    cases = json.loads(args.source.read_text(encoding="utf-8"))["cases"]
    args.out.mkdir(parents=True, exist_ok=True)
    handles: dict[str, object] = {}
    counts: dict[str, int] = {}
    try:
        for case in cases:
            split = case.get("split", "train")
            # 注意：不能用 dict.setdefault(split, ((out/f).open("w")))——
            # setdefault 的默认值参数每次调用都会求值，等于每条用例都用 "w"
            # 重新打开并截断同一个文件。8 条时靠缓冲区侥幸没炸，100 条直接写坏。
            if split not in handles:
                handles[split] = (args.out / f"{split}.jsonl").open("w", encoding="utf-8")
            handle = handles[split]
            prompt = {
                "title": case["title"],
                "signals": case["signals"],
                "basis": case["basis"],
            }
            row = {
                "case_id": case["case_id"],
                "messages": [
                    {"role": "user", "content": PROMPT_PREFIX + json.dumps(prompt, ensure_ascii=False)},
                    {"role": "assistant", "content": json.dumps(case["expect"], ensure_ascii=False, sort_keys=True)},
                ],
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[split] = counts.get(split, 0) + 1
    finally:
        for handle in handles.values():
            handle.close()
    detail = "，".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"写入 {len(cases)} 条（{detail}），来源：{args.source.name}，输出目录：{args.out}")


if __name__ == "__main__":
    main()
