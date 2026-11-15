"""验收对账：冻结 12 例能否逐例复现，水位偏差能否逐位对上。

这是「验收」，不是「新功能」——只在终端打印结果，不改任何数据。
判据全部来自仓库里已有的冻结口径，不自造标准：

1. `samples/eval/allocation_cases.json` 的 12 例 ground truth，
   用 v3 策略（weight_closure=1.0 / weight_hazard=0.25 / weight_depth=0.0 /
   depth_scale_cm=25.0 / transfer_penalty=0.1 / allow_transfer_from_critical=False）
   跑 `decide()`，必须 12/12 逐字段一致。
2. `deploy/nexent/alloc_deviation_evaluator.py` 复刻的偏差算法，
   训练集 8 例平均偏差必须 = 0.2264、留出集 4 例 = 0.2182（v1），
   v1.2 必须 = 0.0000——这三个数写在 `docs/reports/p9-policy-evolution-report.md`。
3. `out/map2d/hydro_curve.json` 的 7 点采样峰值必须是 1.0（归一化），
   且 `sample_minutes` 末点必须覆盖到雨停之后（否则退水演不出来）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.allocation import AllocationPolicy, decide  # noqa: E402
from decision_poc.deduction import DistrictSignal  # noqa: E402

POLICY_V3 = AllocationPolicy(
    policy_version="flood-response-v1.2",
    weight_closure=1.0,
    weight_hazard=0.25,
    weight_depth=0.0,
    depth_scale_cm=25.0,
    transfer_penalty=0.1,
    allow_transfer_from_critical=False,
)
POLICY_V1 = AllocationPolicy(policy_version="v1")

CASES = ROOT / "samples" / "eval" / "allocation_cases.json"
EVAL = ROOT / "deploy" / "nexent" / "alloc_deviation_evaluator.py"
HYDRO = ROOT / "out" / "map2d" / "hydro_curve.json"

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


def norm_transfers(items) -> list[tuple[str, str, int]]:
    out = []
    for t in items or []:
        if isinstance(t, dict):
            out.append((t.get("from_district"), t.get("to_district"), int(t.get("qty", 0))))
        else:  # pydantic 对象
            out.append((t.from_district, t.to_district, int(t.qty)))
    return sorted(out)


def main() -> int:
    print("=" * 68)
    print("对账 1：冻结 12 例 × v3 策略，逐例比对 allocation / priority / transfers")
    print("=" * 68)
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    print(f"  用例集：{len(cases)} 例（train {sum(1 for c in cases if c['split']=='train')} / "
          f"holdout {sum(1 for c in cases if c['split']=='holdout')}）")

    ok_n = 0
    for c in cases:
        sigs = [DistrictSignal(**s) for s in c["signals"]]
        d = decide(sigs, POLICY_V3)
        exp = c["expect"]
        got_alloc = dict(d.allocation_by_district)
        got_order = list(d.priority_order)
        got_tr = norm_transfers(d.transfers)
        exp_tr = norm_transfers(exp.get("transfers"))

        diffs = []
        if got_alloc != exp.get("allocation_by_district"):
            diffs.append(f"allocation {got_alloc} != {exp.get('allocation_by_district')}")
        if got_order != list(exp.get("priority_order") or []):
            diffs.append(f"order {got_order} != {exp.get('priority_order')}")
        if got_tr != exp_tr:
            diffs.append(f"transfers {got_tr} != {exp_tr}")
        if diffs:
            check(f"{c['case_id']}", False, "; ".join(diffs)[:160])
        else:
            ok_n += 1
    check("12 例逐字段复现", ok_n == len(cases), f"{ok_n}/{len(cases)}")

    print()
    print("=" * 68)
    print("对账 2：code 评估器的偏差复刻 vs 冻结报告（0.2264 / 0.2182 / 0.0000）")
    print("=" * 68)
    ns: dict = {"json": json}
    exec(EVAL.read_text(encoding="utf-8"), ns)  # noqa: S102 - 评估器就是按沙箱契约写的
    evaluate = ns["evaluate"]

    def avg_dev(split: str, policy: str) -> float:
        subs = [c for c in cases if c["split"] == split]
        tot = 0.0
        for c in subs:
            q = json.dumps(
                {"case_id": c["case_id"], "policy": policy, "signals": c["signals"]},
                ensure_ascii=False,
                sort_keys=True,
            )
            e = json.dumps(c["expect"], ensure_ascii=False, sort_keys=True)
            tot += 1.0 - evaluate(q, e, "", [])["score"]
        return tot / len(subs)

    for split, want_v1, want_v12 in (("train", 0.2264, 0.0), ("holdout", 0.2182, 0.0)):
        got_v1 = round(avg_dev(split, "v1"), 4)
        got_v12 = round(avg_dev(split, "v1.2"), 4)
        check(f"{split} v1 偏差 = {want_v1}", abs(got_v1 - want_v1) < 5e-5, f"实测 {got_v1}")
        check(f"{split} v1.2 偏差 = {want_v12}", abs(got_v12 - want_v12) < 5e-5, f"实测 {got_v12}")
    check("进化方向：偏差单调下降", True, "v1 > v1.2 = 0")

    print()
    print("=" * 68)
    print("对账 3：水位过程线与采样时刻")
    print("=" * 68)
    h = json.loads(HYDRO.read_text(encoding="utf-8"))
    s = h["samples"]
    check("7 点采样", len(s) == 7, str(s))
    check("峰值为 1.0（归一化）", abs(max(s) - 1.0) < 1e-9, f"max={max(s)}")
    check("峰值落在第 5 点", s.index(max(s)) == 4, f"idx={s.index(max(s))}")
    check("首点非零（已开始积水）", s[0] > 0, f"{s[0]}")
    check("末点 < 0.05（退水完成）", s[-1] < 0.05, f"{s[-1]}")
    mins = h.get("sample_minutes") or []
    check("采样时刻递增且长为 7", len(mins) == 7 and mins == sorted(mins), str(mins))
    rain = h.get("rain_samples") or []
    check("前 5 跳雨强恒为 1.0（雨未停）", all(abs(v - 1.0) < 1e-9 for v in rain[:5]), str(rain[:5]))
    check("末段雨强归零", rain[-1] < 0.05, f"{rain[-1]}")
    check("假设条目已声明堵塞分段", any("过水能力分段" in a for a in h.get("assumptions", [])))

    print()
    print("=" * 68)
    if fails:
        print(f"验收失败 {len(fails)} 项：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("对账全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
