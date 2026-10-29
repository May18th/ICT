"""生成"改进前"评测基线：临时关掉时点闸门，再跑同一套用例。

为什么需要它：P8 的验收要求里有一条是"输出失败案例和改进前后对比"。如果把闸门
修好之后再跑一遍，只能得到一份 22/22 的报告，读者无法判断这些用例当初到底拦住了
什么。所以这里显式地把 `_temporal_refusal` 换成空实现，复现"修之前"的行为，把结果
存成 baseline，正式报告再用 `--baseline` 跟它比。

这个脚本只产出对比用的基线，不参与发布：

    PYTHONPATH=src python samples/eval/make_pre_guard_baseline.py

它不会改任何仓库文件（除了它写出的那个 JSON）。
"""

from __future__ import annotations

import json
from pathlib import Path

from decision_poc import deduction
from decision_poc.urban_eval import evaluate, load_cases

OUT = Path(__file__).with_name("baseline_before_temporal_guard.json")


def main() -> int:
    # 关掉闸门：让"as_of 早于上报时间"和"已闭环工单"照旧被推演
    deduction.DeductionEngine._temporal_refusal = lambda self, incident, as_of: None  # type: ignore[assignment]
    report = evaluate(load_cases())
    OUT.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    metrics = report.metrics
    print(
        f"baseline: {metrics.cases_passed}/{metrics.cases_total} 通过，"
        f"失败 {metrics.cases_failed} → {OUT.name}"
    )
    for item in report.failures:
        print(f"  ! {item.case_id} {item.title}: {'；'.join(item.failures)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
