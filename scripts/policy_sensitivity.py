#!/usr/bin/env python3
"""策略口径敏感性对照：同一批场景，换一套标注规则，看 v1.2 还成不成立。

**为什么需要这个**：仓库里已有的 120 条扩充集（`dist/qlora/allocation_cases_gen.json`）
标签由 `decide()` 在 v3 参数下生成，而 v3 与 `flood-response-v1.2` 的参数**完全相同**。
拿它当演化门禁等于"用 v1.2 的输出去评测 v1.2"，偏差恒为 0，一个字都学不到
（实测 0.000000，四个维度全 0）。这不是数据量不够，是循环论证。

**本脚本做什么**：保持 120 组**场景输入完全不变**（控制变量），只把标注规则换成
"连续水深敏感"口径（`weight_depth=0.5`），重新生成期望分配，然后：

1. 横向比 v1.0 / v1.1 / v1.2 / depth 口径四套参数在新口径下的偏差；
2. 在新口径上跑一次演化，看它会收敛到哪组参数；
3. 把那组参数放回**冻结 12 例**评测，看它是否退化。

结论指向一件事：**策略是口径的函数，不存在"越学越好"的单一真值**。
这不是缺陷，是必须写进材料的适用边界。

**唯一性约定（本脚本必须遵守）**：全系统**唯一生产策略是 `flood-response-v1.2`**。
本脚本产出的 `flood-response-v1.3` 是敏感性实验的副产品，**不提升为生产**，理由是：

- 12 例是唯一带依据条款的 ground truth；"水深敏感"口径是**我们自己设的对照**，没有任何业务依据，
  拿它当训练目标等于自定目标再自证达标；
- 如果 v1.3 成为默认，"当前策略是哪一版"就有两个答案（台账写 v1.2、参数里却有 depth=0.02）；
- v1.2 的每个参数都能追到 12 例上的具体偏差（R1/R2 两轮补丁），v1.3 的 `weight_depth=0.02`
  只能追到"让对照口径好看一点"，无法回答"这个数是谁定的"。

所以本脚本产物只写 `artifacts/`（已 gitignore），产物里带 `promotable: false`，
既不进 `samples/eval/`，也不进运行期记忆，也不进平台台账。

用法：
    PYTHONPATH=src python scripts/policy_sensitivity.py
"""

from __future__ import annotations

import json
from pathlib import Path

from decision_poc.allocation import (
    AllocationCase,
    AllocationPolicy,
    DistrictSignal,
    decide,
    evaluate_policy,
)
from decision_poc.policy_evolution import PRODUCTION_LABEL, evolve, load_cases

ROOT = Path(__file__).resolve().parents[1]
GEN_CASES = ROOT / "dist" / "qlora" / "allocation_cases_gen.json"
OUT_DIR = ROOT / "artifacts" / "policy" / "sensitivity"
#: 冻结记忆——生产策略参数的权威来源，只读。
FROZEN_MEMORY = ROOT / "samples" / "eval" / "decision_memory.json"


def assert_production_singleton() -> None:
    """确认全系统只有一个生产策略，且其参数与冻结记忆逐字一致。

    这条校验是"唯一性"的可执行形式：将来若有人把生产标签改成别的版本、
    或只改了标签没改参数，这里会直接失败，而不是等两个答案同时出现在材料里。
    """
    if PRODUCTION_LABEL != "flood-response-v1.2":
        raise SystemExit(
            f"生产策略标签被改动：{PRODUCTION_LABEL}。"
            "改之前必须同步更新 samples/eval/decision_memory.json、平台台账与全部材料。"
        )
    if not FROZEN_MEMORY.is_file():
        raise SystemExit(f"缺少冻结记忆：{FROZEN_MEMORY}")
    payload = json.loads(FROZEN_MEMORY.read_text(encoding="utf-8"))
    matched = [
        e for e in payload.get("entries", []) if e.get("label") == PRODUCTION_LABEL
    ]
    if not matched:
        raise SystemExit(f"冻结记忆里没有 {PRODUCTION_LABEL} 的条目，生产策略无参数依据")
    params = matched[0].get("params", {})
    expected = {**BASE, **POLICIES["v1.2"]}
    for key, value in expected.items():
        if params.get(key) != value:
            raise SystemExit(
                f"生产策略参数与冻结记忆不一致：{key} 记忆={params.get(key)} 期望={value}"
            )
    print(f"唯一性校验通过：生产策略 {PRODUCTION_LABEL}，参数与冻结记忆逐项一致\n")

BASE = {
    "weight_closure": 0.0,
    "weight_hazard": 0.0,
    "weight_depth": 0.0,
    "depth_scale_cm": 25.0,
    "transfer_penalty": 0.0,
    "allow_transfer_from_critical": False,
}

POLICIES = {
    "v1.0": {},
    "v1.1": {"weight_closure": 1.0, "transfer_penalty": 0.1},
    "v1.2": {"weight_closure": 1.0, "weight_hazard": 0.25, "transfer_penalty": 0.1},
    "depth-0.5": {
        "weight_closure": 1.0,
        "weight_hazard": 0.25,
        "weight_depth": 0.5,
        "transfer_penalty": 0.1,
    },
}


def policy_of(name: str) -> AllocationPolicy:
    return AllocationPolicy(policy_version=name, **{**BASE, **POLICIES[name]})


def relabel(case: dict, policy: AllocationPolicy) -> dict:
    """保持 signals 不变，只按新口径重算 expect。"""
    signals = [DistrictSignal(**s) for s in case["signals"]]
    decision = decide(signals, policy)
    # 与 scripts/gen_qlora_cases.py 的 expect_from_decision 逐字段同构：
    # critical_unmet 只取"关键片区"的未满足量，不是全量 unmet（全量在 unmet_by_district）。
    expect = {
        "allocation_by_district": dict(decision.allocation_by_district),
        "transfers": [
            {
                "from_district": t.from_district,
                "to_district": t.to_district,
                "qty": t.qty,
                "delay_min": t.delay_min,
            }
            for t in decision.transfers
        ],
        "priority_order": list(decision.priority_order),
        "critical_unmet": {
            line.district: line.unmet for line in decision.lines if line.critical
        },
    }
    return {
        "case_id": case["case_id"],
        "title": case["title"],
        "split": case["split"],
        "basis": [
            f"口径：{policy.policy_version}（weight_depth={policy.weight_depth}，"
            f"depth_scale_cm={policy.depth_scale_cm}）",
            *case.get("basis", []),
        ],
        "signals": case["signals"],
        "expect": expect,
    }


def main() -> int:
    assert_production_singleton()
    if not GEN_CASES.is_file():
        print(f"缺少扩充集：{GEN_CASES}")
        return 1
    raw = json.loads(GEN_CASES.read_text(encoding="utf-8"))
    source_note = raw.get("note", "")

    depth_policy = policy_of("depth-0.5")
    relabeled = [relabel(c, depth_policy) for c in raw["cases"]]
    cases = [AllocationCase.model_validate(c) for c in relabeled]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_cases = OUT_DIR / "depth_sensitive_cases.json"
    out_cases.write_text(
        json.dumps(
            {
                "schema_version": "policy-sensitivity-1",
                "note": (
                    "口径对照集：场景输入与 dist/qlora/allocation_cases_gen.json 完全一致，"
                    "仅把标注规则换成连续水深敏感口径。**不是竞赛评测口径，也不是生产策略依据**，"
                    "冻结基准仍是 samples/eval/allocation_cases.json。"
                ),
                "do_not_use_as_ground_truth": True,
                "production_default": PRODUCTION_LABEL,
                "promotable": False,
                "source_note": source_note,
                "labeler": {k: getattr(depth_policy, k) for k in POLICIES["depth-0.5"]},
                "cases": relabeled,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"对照集已生成：{out_cases.name}（{len(cases)} 例，场景与扩充集逐一对应）\n")

    print("一、同一批场景、不同标注口径下的偏差")
    print(f"{'策略':<12}{'新口径偏差':>12}{'冻结12例偏差':>14}")
    frozen = load_cases()
    rows = []
    for name in POLICIES:
        pol = policy_of(name)
        dev_new = evaluate_policy(pol, cases).deviation
        dev_frozen = evaluate_policy(pol, frozen).deviation
        rows.append(
            {"policy": name, "deviation_new": round(dev_new, 6),
             "deviation_frozen": round(dev_frozen, 6)}
        )
        print(f"{name:<12}{dev_new:>12.4f}{dev_frozen:>14.4f}")

    print("\n二、在『水深敏感』口径上跑演化（最多 6 轮）")
    report = evolve(cases=cases, max_rounds=6, include_real_event=False, reviews=())
    print(f"  起点偏差 {report.initial_train.deviation:.4f} → 终点 {report.final_train.deviation:.4f}")
    print(f"  最终标签 {report.final_label}｜采纳 {report.promoted_rounds} 轮 / 拒绝 {report.rejected_rounds} 轮")
    tuned = report.final_policy
    print("  收敛参数：")
    for key in POLICIES["depth-0.5"]:
        print(f"    {key} = {getattr(tuned, key)}")

    dev_frozen_tuned = evaluate_policy(tuned, frozen).deviation
    print(f"\n三、把这组参数放回冻结 12 例：偏差 {dev_frozen_tuned:.4f}")
    print(f"  对照：v1.2 在冻结 12 例上 {evaluate_policy(policy_of('v1.2'), frozen).deviation:.4f}")

    summary = {
        "schema_version": "policy-sensitivity-1",
        "scene_source": str(GEN_CASES.relative_to(ROOT)),
        "case_count": len(cases),
        # 唯一性：任何读这份产物的人第一眼就该知道它不能当生产策略
        "production_default": "flood-response-v1.2",
        "promotable": False,
        "why_not_promotable": (
            "第二口径（水深敏感）是为对照自设的目标，没有业务依据；"
            "把它当训练目标等于自定目标再自证达标。生产默认固定为 v1.2。"
        ),
        "cross_policy": rows,
        "evolved_on_new_caliber": {
            "label": report.final_label,
            "initial_deviation": round(report.initial_train.deviation, 6),
            "final_deviation": round(report.final_train.deviation, 6),
            "promoted_rounds": report.promoted_rounds,
            "rejected_rounds": report.rejected_rounds,
            "params": {
                key: getattr(tuned, key) for key in POLICIES["depth-0.5"]
            },
            "deviation_on_frozen": round(dev_frozen_tuned, 6),
        },
        "conclusion": (
            "同一批场景、换标注口径后最优参数随之改变：v1.2 在『阈值型』口径下偏差为 0，"
            "在『水深敏感』口径下不是最优。策略是口径的函数，不存在跨口径的单一真值。"
        ),
    }
    out_summary = OUT_DIR / "summary.json"
    out_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n结果已写：{out_summary}")
    print(
        "\n注意：`flood-response-v1.3` 是敏感性实验副产品，**不提升为生产策略**。"
        "全系统唯一生产默认是 `flood-response-v1.2`——"
        "第二口径没有业务依据，拿它当训练目标就是自定目标再自证达标。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
