# -*- coding: utf-8 -*-
"""评测 IR 版 adapter：模型只出离散判定，台数由 apply_ir() 算。

六个维度，前四个考模型，后两个考整条链路（含代码补算术）：

    ir_valid            输出是合法 JSON 且三键齐全
    critical_districts  关键片区集合完全一致
    priority_order      优先序完全一致
    transfer_direction  调拨方向（from→to 列表）完全一致
    allocation_by_district   IR + 算术还原后与 expect 一致
    critical_unmet            IR + 算术还原后与 expect 一致

后两项才是真正要看的：它能区分「模型判对了方向但算错数」与「模型判错方向」。
因为在 IR 方案里台数由代码算，只要方向对，后两项必然对——对不上就是模型判错了。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.deduction import DistrictSignal  # noqa: E402
from qlora_ir import FROZEN, apply_ir, ir_from_case, norm  # noqa: E402

MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
OUT = ROOT / "dist/qlora"
PROMPT_PREFIX = "请判断各片区关键状态与优先序，并给出跨区调拨方向（台数由系统计算）："

IR_KEYS = ("critical_districts", "priority_order", "transfers")


def build_prompt(case: dict) -> str:
    payload = {"title": case["title"], "signals": case["signals"], "basis": case["basis"]}
    return PROMPT_PREFIX + json.dumps(payload, ensure_ascii=False)


def extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def generate(model, tokenizer, prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=192, do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    return tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def directions(transfers) -> list[tuple[str, str]]:
    """容错：模型可能给 null / 字符串 / 缺字段，一律当空列表处理。"""
    if not isinstance(transfers, list):
        return []
    result = []
    for item in transfers:
        if isinstance(item, dict) and item.get("from_district") and item.get("to_district"):
            result.append((str(item["from_district"]), str(item["to_district"])))
    return sorted(result)


def score(parsed: dict | None, case: dict, expected_ir: dict) -> dict:
    result = {
        "ir_valid": parsed is not None and all(k in parsed for k in IR_KEYS),
        "critical_districts": False,
        "priority_order": False,
        "transfer_direction": False,
        "allocation_by_district": False,
        "critical_unmet": False,
    }
    if parsed is None:
        return result
    result["critical_districts"] = sorted(
        str(x) for x in parsed.get("critical_districts", []) or []
    ) == sorted(expected_ir["critical_districts"])
    result["priority_order"] = list(parsed.get("priority_order", []) or []) == expected_ir["priority_order"]
    result["transfer_direction"] = directions(parsed.get("transfers")) == directions(
        expected_ir["transfers"]
    )

    replayed = apply_ir(parsed, [DistrictSignal(**s) for s in case["signals"]])
    expect = case["expect"]
    result["allocation_by_district"] = norm(replayed["allocation_by_district"]) == norm(
        expect["allocation_by_district"]
    )
    result["critical_unmet"] = norm(replayed["critical_unmet"]) == norm(expect["critical_unmet"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=FROZEN)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--out-stem", default=None)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")
    cases = [
        c for c in json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
        if c.get("split") == "holdout"
    ]
    if not cases:
        raise SystemExit(f"{args.cases} 里没有 holdout 用例")
    stem = args.out_stem or f"ir_{args.adapter.name}_{args.cases.stem}"

    quant = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(MODEL)

    results: dict[str, list] = {}
    for label, adapter in (("base", None), ("tuned", args.adapter)):
        if adapter is not None and not adapter.is_dir():
            raise SystemExit(f"adapter 不存在：{adapter}")
        model = AutoModelForCausalLM.from_pretrained(
            MODEL, quantization_config=quant, device_map="auto"
        )
        if adapter is not None:
            model = PeftModel.from_pretrained(model, str(adapter))
        model.eval()
        rows = []
        for case in cases:
            text = generate(model, tokenizer, build_prompt(case))
            parsed = extract_json(text)
            expected_ir = ir_from_case([DistrictSignal(**s) for s in case["signals"]])
            rows.append({
                "case_id": case["case_id"],
                "raw": text[:400],
                "expected_ir": expected_ir,
                **score(parsed, case, expected_ir),
            })
            print(f"[{label}] {case['case_id']} ir_valid={rows[-1]['ir_valid']}", flush=True)
        results[label] = rows
        del model
        torch.cuda.empty_cache()

    keys = ["ir_valid", "critical_districts", "priority_order", "transfer_direction",
            "allocation_by_district", "critical_unmet"]
    summary = {
        label: {k: sum(r[k] for r in rows) / len(rows) for k in keys}
        for label, rows in results.items()
    }

    lines = [
        f"# IR 版对照（base vs tuned，{args.cases.name} holdout {len(cases)} 例）",
        "",
        f"- 模型：{MODEL}（4bit NF4）；tuned = base + `{args.adapter.name}`",
        "- 任务：模型只输出关键集/优先序/调拨方向，台数由 `apply_ir()` 计算",
        "",
        "| 案例 | 维度 | base | tuned |",
        "| --- | --- | --- | --- |",
    ]
    for case, b, t in zip(cases, results["base"], results["tuned"]):
        for k in keys:
            lines.append(f"| {case['case_id']} | {k} | {'✅' if b[k] else '❌'} | {'✅' if t[k] else '❌'} |")
    lines += ["", f"| 汇总（{len(cases)} 例） | base | tuned |", "| --- | --- | --- |"]
    for k in keys:
        lines.append(f"| {k} | {summary['base'][k]:.0%} | {summary['tuned'][k]:.0%} |")
    lines += ["", "## tuned 原始输出", ""]
    for r in results["tuned"]:
        lines.append(f"- `{r['case_id']}` → `{r['raw'].strip()[:200]}`")
    (OUT / f"{stem}_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / f"{stem}_results.json").write_text(
        json.dumps({"summary": summary, "detail": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("report:", OUT / f"{stem}_report.md")
    print("summary:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
