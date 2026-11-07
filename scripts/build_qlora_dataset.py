"""Build a small supervised QLoRA dataset from allocation evaluation cases."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "samples/eval/allocation_cases.json"
OUT = ROOT / "dist/qlora"


def main() -> None:
    cases = json.loads(SOURCE.read_text(encoding="utf-8"))["cases"]
    OUT.mkdir(parents=True, exist_ok=True)
    handles: dict[str, object] = {}
    try:
        for case in cases:
            split = case.get("split", "train")
            handle = handles.setdefault(split, (OUT / f"{split}.jsonl").open("w", encoding="utf-8"))
            prompt = {"title": case["title"], "signals": case["signals"], "basis": case["basis"]}
            row = {
                "case_id": case["case_id"],
                "messages": [
                    {"role": "user", "content": "请根据城市内涝资源信号给出可审计的调度结论：" + json.dumps(prompt, ensure_ascii=False)},
                    {"role": "assistant", "content": json.dumps(case["expect"], ensure_ascii=False, sort_keys=True)},
                ],
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        for handle in handles.values():
            handle.close()
    print(f"写入 {len(cases)} 条，输出目录：{OUT}")


if __name__ == "__main__":
    main()
