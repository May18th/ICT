# -*- coding: utf-8 -*-
"""allocation IR: 把「谁关键 / 谁优先 / 谁调给谁」从「调多少」里切出来。

## 为什么切这一刀

24 步与 300 步两组实验都指向同一件事：1.5B 学不会跨区调拨的算术。
300 步在生成分布上 transfers 能到 60%，换成人手写的冻结 4 例直接归零，
且输出退化成 `{A-03:3, B-01:2}`（正好等于「只用本区库存」的 v1 口径）。

亏损点很明确：模型要做「缺口−可动用库存」的跨字段减法。这类算术代码里
已有确定性实现（`allocation.decide()`），不该让 1.5B 去学。

所以把一次决策拆成两段：

    模型：signals ──> IR（关键集 + 优先序 + 调拨方向）      ← 分类问题
    代码：IR + signals ──> 完整分配（台数、迟延、缺口）      ← 算术问题

模型只做离散判定，不碰数字。

## IR 契约

    {
      "critical_districts": ["A-03"],                    # 已过封路阈值或含燃气/电力
      "priority_order": ["A-03", "B-01"],                # 全部片区，按优先序排列
      "transfers": [{"from_district": "B-01", "to_district": "A-03"}]
    }

注意 `transfers` **不带数量**。数量由 `apply_ir()` 按 decide() 同一套算术补齐：
本区库存先满足本区，缺口按优先序找调出方，调出方动用「未用库存 + 已分配给自己的量」。

## 充分性闸门

IR 必须能无损表达正确答案，否则这个拆法就是错的。`self_check()` 把冻结 12 例
逐例从 decide() 的结果反推 IR，再用 `apply_ir()` 回放，必须与原 decision 逐字段一致。
一条不符即中止——这说明 IR 漏了信息，模型再准也还原不出正确分配。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.allocation import (  # noqa: E402
    AllocationPolicy,
    decide,
    default_policy,
    load_cases,
)
from decision_poc.deduction import DistrictSignal  # noqa: E402

FROZEN = ROOT / "samples/eval/allocation_cases.json"
TRANSFER_DELAY_MIN = 40

# 冻结 ground truth 的口径参数，来源 samples/eval/decision_memory.json
# 条目 M-3979c3901de5（label=flood-response-v1.2）
V3_PARAMS: dict[str, Any] = {
    "weight_closure": 1.0,
    "weight_hazard": 0.25,
    "weight_depth": 0.0,
    "depth_scale_cm": 25.0,
    "transfer_penalty": 0.1,
    "allow_transfer_from_critical": False,
}


def policy() -> AllocationPolicy:
    return AllocationPolicy(policy_version="v3", **V3_PARAMS)


def ir_from_decision(decision) -> dict:
    """从完整决策反推 IR（只保留离散判定，丢掉所有数量）。"""
    return {
        "critical_districts": [line.district for line in decision.lines if line.critical],
        "priority_order": list(decision.priority_order),
        "transfers": [
            {"from_district": t.from_district, "to_district": t.to_district}
            for t in decision.transfers
        ],
    }


def ir_from_case(case_signals: Sequence[DistrictSignal]) -> dict:
    return ir_from_decision(decide(case_signals, policy()))


def apply_ir(ir: dict, signals: Iterable[DistrictSignal]) -> dict:
    """按 IR 的离散判定 + decide() 同款算术，还原成完整 expect。

    对 IR 的容错：片区名缺失、顺序缺失都不崩，按名称补齐；但**数量一律由这里算**，
    模型给什么数字都不采信（IR 里本来就没有数字字段）。
    """
    by = {s.district: s for s in signals}
    districts = sorted(by)
    allocated = {n: min(by[n].demand, by[n].local_available) for n in districts}
    stock = {n: max(0, by[n].local_available - allocated[n]) for n in districts}

    order = [d for d in ir.get("priority_order", []) if d in by]
    order += [d for d in districts if d not in order]

    transfers: list[dict] = []
    for beneficiary in order:
        need = by[beneficiary].demand - allocated[beneficiary]
        if need <= 0:
            continue
        for item in ir.get("transfers", []):
            if need <= 0:
                break
            if not isinstance(item, dict):
                continue
            if item.get("to_district") != beneficiary:
                continue
            donor = item.get("from_district")
            if donor not in by or donor == beneficiary:
                continue
            movable = stock[donor] + allocated[donor]
            if movable <= 0:
                continue
            qty = min(need, movable)
            from_stock = min(qty, stock[donor])
            stock[donor] -= from_stock
            allocated[donor] -= qty - from_stock
            allocated[beneficiary] += qty
            need -= qty
            transfers.append(
                {
                    "from_district": donor,
                    "to_district": beneficiary,
                    "qty": qty,
                    "delay_min": TRANSFER_DELAY_MIN,
                }
            )

    critical = [d for d in ir.get("critical_districts", []) if d in by]
    return {
        "allocation_by_district": {n: allocated[n] for n in districts},
        "transfers": transfers,
        "priority_order": order,
        "critical_unmet": {d: by[d].demand - allocated[d] for d in critical},
    }


def norm(value: Any) -> Any:
    """比较用的规范化：只为消除键序影响，值本身不动。"""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


def self_check(verbose: bool = True) -> int:
    """闸门：IR 必须无损表达冻结 12 例的正确答案。"""
    cases = load_cases(FROZEN)
    bad: list[str] = []
    for case in cases:
        decision = decide(case.signals, policy())
        expect = case.expect.model_dump(mode="json")
        replayed = apply_ir(ir_from_decision(decision), case.signals)
        # 回放结果只含 4 个评测字段，与 expect 同构
        if norm(replayed) != norm(expect):
            bad.append(case.case_id)
            if verbose:
                print(f"  [差异] {case.case_id}")
                print(f"    replay={json.dumps(replayed, ensure_ascii=False, sort_keys=True)}")
                print(f"    expect={json.dumps(expect, ensure_ascii=False, sort_keys=True)}")
    if bad:
        raise SystemExit(f"[闸门失败] IR 无法还原 {', '.join(bad)} —— IR 契约漏了信息")
    if verbose:
        print(f"[闸门通过] IR 无损还原冻结 {len(cases)}/{len(cases)} 例（含调拨数量）")
    return len(cases)


if __name__ == "__main__":
    self_check()
