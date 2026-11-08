# -*- coding: utf-8 -*-
"""生成扩充版 allocation 用例集，标签由 decide() 在 v3 参数下确定性复算。

## 为什么需要这个脚本

冻结评测集 `samples/eval/allocation_cases.json` 只有 12 例（8 训练 + 4 留出），
而 24 步训练的失败结论是「瓶颈在数据规模与 schema 对齐」。要验证「数据量上去后
schema 对齐是否可学」，需要 100 条量级的训练样本。

但冻结集是命中口径的源头：`docs/competition/eval-dataset-spec.md` 写死「12 例」，
`policy_evolution.py` 拿它当冻结反馈信号，`build_eval_package.py` 把它打进提交物。
往里面追加用例会同时打断这四条链路，所以扩充数据**必须另存新文件**。

## 为什么标签敢自动化

手工造 100 条既慢又必然出现标签不一致（12 例逐例带人工依据条款，人写 100 条就会飘）。
标签的唯一可信批量来源是 `src/decision_poc/allocation.py` 的 `decide()`：
确定性策略，不调模型，同输入两次运行逐位一致。

参数取 `samples/eval/decision_memory.json` 里 flood-response-v1.2 进化出的 v3：
    weight_closure=1.0, weight_hazard=0.25, weight_depth=0.0,
    depth_scale_cm=25.0, transfer_penalty=0.1, allow_transfer_from_critical=False

## 伪标签闸门（本脚本的核心）

生成前先自检：用同一套参数跑冻结 12 例，`decide()` 必须**逐例复现**其 expect，
一条不符即中止，不写任何文件。这条闸门保证生成标签与冻结集语义一致——
否则就是"格式合法但语义不对"的伪标签，训练出来的模型学的是错的。

## 采样策略：按结果类型配额，不做盲采

盲采的实测结果是 120 条里只有 12 条含调拨、且 B-01 永远轮不到当优先方。
这样训练出来的模型只会学成「A-03 优先」。所以改成三种构造器 + 结果配额：

    transfer   含调拨（两种方向各半）      ~45%
    unmet      有缺口但不动用跨区（关键对手 / 无人可调）  ~40%
    clean      全部满足、无缺口            ~15%

`transfers` 与 `critical_unmet` 正是 24 步训练里 0% / 25% 的两个维度，
必须保证样本里足够多地出现，否则扩容实验测不出东西。

## 产物

    dist/qlora/allocation_cases_gen.json    默认 120 条（100 train + 20 holdout）

**不写** `samples/eval/allocation_cases.json`。

## 口径限制（写清楚，避免被当成真实数据）

- 信号是受控合成，非真实工单数据；
- 所有用例固定 2 个片区（A-03 / B-01），与冻结集同构，不引入结构变量；
- 标签 = decide() 的确定性输出，本质是在蒸馏一个已在仓库里的确定性算法。
  本实验只回答「模型能否学会 schema 与算术」，不回答「模型能否替代求解器」。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.allocation import (  # noqa: E402
    AllocationCase,
    AllocationPolicy,
    decide,
    load_cases,
)
from decision_poc.deduction import DistrictSignal  # noqa: E402

FROZEN = ROOT / "samples/eval/allocation_cases.json"
OUT = ROOT / "dist/qlora/allocation_cases_gen.json"

# v3 参数来源：samples/eval/decision_memory.json 条目 M-3979c3901de5
# （label=flood-response-v1.2，训练偏差 0.0）。改这里等于改 ground truth 口径。
V3_PARAMS: dict[str, object] = {
    "weight_closure": 1.0,
    "weight_hazard": 0.25,
    "weight_depth": 0.0,
    "depth_scale_cm": 25.0,
    "transfer_penalty": 0.1,
    "allow_transfer_from_critical": False,
}

DISTRICTS = ("A-03", "B-01")
CRIT_KINDS = ("closure+hazard", "closure", "hazard")

RULE_LINES = (
    "CF2：关键片区（已过封路阈值，或受影响范围含燃气/电力）优先保障，默认不可被调出",
    "跳 5：跨区调用需 40 分钟；本区库存先满足本区需求，仍有缺口才考虑跨区",
)


def policy() -> AllocationPolicy:
    return AllocationPolicy(policy_version="v3", **V3_PARAMS)  # type: ignore[arg-type]


def expect_from_decision(decision) -> dict:
    """AllocationDecision -> 用例 expect 落盘格式（与冻结 12 例逐字段同构）。"""
    return {
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


def canonical(signals: list[dict]) -> str:
    """信号规范化指纹，用于去重与「撞上冻结用例」检测。"""
    ordered = sorted(signals, key=lambda s: s["district"])
    return json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def self_check() -> int:
    """闸门：v3 下 decide() 必须复现冻结 12 例，否则中止。"""
    cases = load_cases(FROZEN)
    pol = policy()
    bad: list[str] = []
    for case in cases:
        got = expect_from_decision(decide(case.signals, pol))
        exp = case.expect.model_dump(mode="json")
        if json.loads(json.dumps(got, sort_keys=True)) != json.loads(
            json.dumps(exp, sort_keys=True)
        ):
            bad.append(case.case_id)
    if bad:
        raise SystemExit(
            f"[闸门失败] v3 参数无法复现冻结用例：{', '.join(bad)}\n"
            f"拒绝生成——否则产出的是语义不一致的伪标签。"
        )
    print(f"[闸门通过] v3 参数逐例复现冻结 {len(cases)}/{len(cases)} 例")
    return len(cases)


# --- 信号构造 ---------------------------------------------------------------


def signal(name: str, crit: str, demand: int, local: int, rng: random.Random) -> dict:
    """按给定关键状态与资源账造一个 DistrictSignal。

    crit ∈ {closure+hazard, closure, hazard, none}。
    小概率把深度置为 None：decide() 里有独立分支（「深度未知，未判定封路」），
    这个分支不覆盖的话模型永远见不到。
    """
    closure = "closure" in crit
    hazard = "hazard" in crit
    if rng.random() < 0.08:
        depth: float | None = None
        closure_required: bool | None = None
    elif closure:
        depth = float(rng.choice([30, 36]))
        closure_required = True
    else:
        depth = float(rng.choice([12, 18, 24]))
        closure_required = False
    return {
        "district": name,
        "depth_cm": depth,
        "closure_required": closure_required,
        "has_hazard": hazard,
        "demand": demand,
        "local_available": local,
        "world_required": 0,  # 占位，build_case 里按全市需求回填
    }


def _ordered(sigs: list[dict]) -> list[dict]:
    """按 DISTRICTS 固定顺序输出，与冻结集的 signals 排列保持一致。"""
    return sorted(sigs, key=lambda s: DISTRICTS.index(s["district"]))


def _transfer_pair(rng: random.Random, flip: bool) -> list[dict]:
    """构造必然产生调拨的一对信号：关键片区有缺口，非关键片区有可动用库存。

    flip 交换**角色**（谁是受益方），不是交换列表顺序——decide() 不看顺序，
    只交换顺序会让调拨方向永远只有一个。
    """
    crit = rng.choice(CRIT_KINDS)
    ben_name, don_name = (DISTRICTS[1], DISTRICTS[0]) if flip else (DISTRICTS[0], DISTRICTS[1])
    ben_demand = rng.randint(3, 6)
    ben_local = max(0, ben_demand - rng.randint(1, 3))  # 保 1~3 台缺口
    don_demand = rng.randint(1, 4)
    don_local = don_demand + rng.randint(0, 2)  # 可能富余，也可能刚好（靠让出已分配量）

    ben = signal(ben_name, crit, ben_demand, ben_local, rng)
    don = signal(don_name, "none", don_demand, don_local, rng)
    return _ordered([ben, don])


def _unmet_pair(rng: random.Random, flip: bool) -> list[dict]:
    """构造「有缺口但不跨区调拨」的一对信号，覆盖两条规则分支。"""
    left_name, right_name = (DISTRICTS[1], DISTRICTS[0]) if flip else (DISTRICTS[0], DISTRICTS[1])
    if rng.random() < 0.5:
        # 分支一：双方都关键 → 谁都不许被调出（T4 口径）
        d1 = rng.randint(3, 6)
        d2 = rng.randint(3, 6)
        left = signal(left_name, rng.choice(CRIT_KINDS), d1, max(0, d1 - rng.randint(1, 3)), rng)
        right = signal(right_name, rng.choice(CRIT_KINDS), d2, max(0, d2 - rng.randint(1, 3)), rng)
    else:
        # 分支二：关键片区有缺口，非关键片区库存为 0（T8 口径）
        d1 = rng.randint(3, 6)
        left = signal(left_name, rng.choice(CRIT_KINDS), d1, max(0, d1 - rng.randint(1, 3)), rng)
        right = signal(right_name, "none", rng.randint(1, 4), 0, rng)
    return _ordered([left, right])


def _plain_pair(rng: random.Random, flip: bool) -> list[dict]:
    """构造不产生调拨、也（多数）不产生关键缺口的一对信号。"""
    left_name, right_name = (DISTRICTS[1], DISTRICTS[0]) if flip else (DISTRICTS[0], DISTRICTS[1])
    if rng.random() < 0.5:
        # 双方均非关键且都够用
        d1 = rng.randint(2, 5)
        d2 = rng.randint(2, 5)
        left = signal(left_name, "none", d1, d1 + rng.randint(0, 2), rng)
        right = signal(right_name, "none", d2, d2 + rng.randint(0, 2), rng)
    else:
        # 双方均非关键但一方有缺口：无优先序依据就不动（T7 口径）
        d1 = rng.randint(3, 6)
        d2 = rng.randint(1, 4)
        left = signal(left_name, "none", d1, max(0, d1 - rng.randint(1, 2)), rng)
        right = signal(right_name, "none", d2, d2 + rng.randint(0, 2), rng)
    return _ordered([left, right])


BUILDERS = {
    "transfer": _transfer_pair,
    "unmet": _unmet_pair,
    "plain": _plain_pair,
}


def describe(sig: dict) -> str:
    if sig["depth_cm"] is None:
        head = f"{sig['district']} 积水深度未解析（不判定封路）"
    elif sig["closure_required"]:
        head = f"{sig['district']} 积水 {sig['depth_cm']:g}cm，已过封路阈值"
    else:
        head = f"{sig['district']} 积水 {sig['depth_cm']:g}cm，未过封路阈值"
    if sig["has_hazard"]:
        head += "，受影响范围含燃气/电力"
    return f"{head}；需求 {sig['demand']} 台、本区库存 {sig['local_available']} 台"


def build_case(index: int, signals: list[dict], split: str) -> dict:
    total = sum(s["demand"] for s in signals)
    for s in signals:
        s["world_required"] = total
    decision = decide([DistrictSignal(**s) for s in signals], policy())
    expect = expect_from_decision(decision)

    facts = "；".join(describe(s) for s in signals)
    crit_names = [s["district"] for s in signals if s["closure_required"] or s["has_hazard"]]
    crit_desc = f"关键片区 {'、'.join(crit_names)}" if crit_names else "两片区均非关键"
    return {
        "case_id": f"G{index:03d}",
        "title": f"生成用例 {index:03d}：{crit_desc}；需求 {total} 台",
        "split": split,
        "basis": [f"信号：{facts}", *RULE_LINES],
        "signals": signals,
        "expect": expect,
    }


def outcome_of(case: dict) -> str:
    """按 decide() 的产出归类，用于校验构造器没有悄悄退化。"""
    if case["expect"]["transfers"]:
        return "transfer"
    alloc = case["expect"]["allocation_by_district"]
    unmet_any = any(s["demand"] > alloc[s["district"]] for s in case["signals"])
    return "unmet" if unmet_any else "plain"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=int, default=100)
    parser.add_argument("--holdout", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261108)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    n_frozen = self_check()
    frozen_signatures = {
        canonical([s.model_dump(mode="json") for s in c.signals])
        for c in load_cases(FROZEN)
    }

    rng = random.Random(args.seed)
    wanted = args.train + args.holdout
    quota = {
        "transfer": round(wanted * 0.45),
        "unmet": round(wanted * 0.40),
    }
    quota["plain"] = wanted - quota["transfer"] - quota["unmet"]

    seen = set(frozen_signatures)
    raw: list[list[dict]] = []
    counts = {k: 0 for k in quota}
    guard = 0
    while any(counts[k] < quota[k] for k in quota):
        guard += 1
        if guard > wanted * 400:
            raise SystemExit("构造无法凑够配额，检查构造器是否与 decide() 语义脱节")
        # 按缺口最大的一类优先补，填满后转下一类
        kind = max(quota, key=lambda k: quota[k] - counts[k])
        if counts[kind] >= quota[kind]:
            continue
        flip = bool(counts[kind] % 2)  # 交替翻转受益方，保证两个方向都有
        sigs = BUILDERS[kind](rng, flip)
        sig = canonical(sigs)
        if sig in seen:
            # 撞车时强制翻转一次再试，仍撞则跳过
            flip = not flip
            sigs = BUILDERS[kind](rng, flip)
            sig = canonical(sigs)
            if sig in seen:
                continue
        # 构造结果必须落在目标类别，否则丢弃（防止构造器悄悄退化）
        probe = build_case(0, [dict(s) for s in sigs], "train")
        if outcome_of(probe) != kind:
            continue
        seen.add(sig)
        raw.append(sigs)
        counts[kind] += 1

    rng.shuffle(raw)
    cases = []
    for i, sigs in enumerate(raw, start=1):
        split = "train" if i <= args.train else "holdout"
        cases.append(build_case(i, sigs, split))

    # 结构校验：任何一条不合 schema 就整体失败，不写半成品
    for case in cases:
        AllocationCase.model_validate(case)

    payload = {
        "schema_version": "allocation-cases-1",
        "note": (
            "QLoRA 扩容实验用合成用例集，非真实工单数据，不进入竞赛评测口径。"
            f"冻结集仍为 samples/eval/allocation_cases.json（{n_frozen} 例）。"
        ),
        "generated_by": (
            "scripts/gen_qlora_cases.py；标签由 decide() 在 v3 参数下确定性复算"
            f"（自检复现冻结 {n_frozen}/{n_frozen} 例）"
        ),
        "policy": {"policy_version": "v3", **V3_PARAMS},
        "seed": args.seed,
        "outcome_quota": quota,
        "cases": cases,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 分布自检：下游一眼看出这批数据覆盖了什么，别只看条数
    n_transfer = sum(1 for c in cases if c["expect"]["transfers"])
    n_crit_unmet = sum(1 for c in cases if c["expect"]["critical_unmet"])
    n_unknown = sum(1 for c in cases for s in c["signals"] if s["depth_cm"] is None)
    dirs: dict[str, int] = {}
    for c in cases:
        for t in c["expect"]["transfers"]:
            key = f"{t['from_district']}→{t['to_district']}"
            dirs[key] = dirs.get(key, 0) + 1
    print(f"写入 {len(cases)} 条（train={args.train} holdout={args.holdout}）-> {args.out}")
    print(
        f"  含调拨 {n_transfer} 条 | 含关键缺口 {n_crit_unmet} 条 | 深度未知信号 {n_unknown} 个"
    )
    print(f"  调拨方向分布：{dirs}")
    print(f"  冻结集未改动（{(FROZEN.stat().st_size)} bytes）")


if __name__ == "__main__":
    main()
