"""先例 A/B：同一事件、两次真机对话，比"结论有没有被先例改变"。

为什么需要 A/B 而不是只看一次 8/8
----------------------------------
提示词放开了"可取同类先例"。单次 8/8 只能说明"这一轮结论对"，说明不了
"先例没有参与改结论"——模型可能恰好没检索，也可能检索了但没照抄。这两种都
不该算通过。

所以跑两轮同一事件：

- A 组：允许追问先例（期望：引用真实定级资产）；
- B 组：明确禁止检索先例（期望：不引用）。

判定通过需要三件事同时成立：
1. 两组各自与离线基线八项一致（说明事实层面都干净）；
2. 两组的结构化结论相同（six_hops 字段、冲突编号+类型、资源缺口、关键动作族覆盖）；
3. A 有先例引用、B 没有（说明"引用"这件事是由指令控制的，不是碰巧）。

**为什么不逐字比 action_order**：两轮由模型复述，措辞与标点必然有差异
（实测同一事件两轮 summary 都不完全一样）。逐字比会把"改写"误判成"结论变了"。
按 8 项校验里已有的"关键动作族"口径比覆盖，才是有意义的等价性。

跑法：

    NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    PYTHONPATH=src python scripts/nexent_precedent_ab.py

退出码：0 三项全过；1 任一不成立；2 缺配置。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings  # noqa: E402
from decision_poc.nexent_urban import (  # noqa: E402
    DEFAULT_AS_OF,
    DEFAULT_LABELS,
    offline_baseline,
    run,
)

EVENT_ID = "WO-20261029-007"
AS_OF = DEFAULT_AS_OF
LABELS = list(DEFAULT_LABELS)
PRECEDENT_PREFIX = "AS-CD-CITY-emergency_response_case"

COMMON = [
    f"城市内涝跨部门事件：{EVENT_ID}，时点 {AS_OF}，调用方身份 {','.join(LABELS)}。",
    "请使用已挂载的 MCP 工具完成六跳推演：",
    "1) event_deduce 一次返回六跳全部事实、三类冲突、资源缺口与处置顺序；",
    "2) 需要核对依据时用 evidence_locate 取原文，不得凭记忆编造。",
]

QUERY_WITH_PRECEDENT = "\n".join(
    COMMON
    + [
        "另外用户追问一句：同类情形以前政府是怎么定级的？库里有真实案例就用 doc_search "
        "查一次并把出处放进 evidence_refs，只作对照，不得改变本事件的判定。",
        "最后按约定字段输出 JSON，用 final_answer 交卷。",
    ]
)

QUERY_WITHOUT_PRECEDENT = "\n".join(
    COMMON
    + [
        "本轮**不要调用 doc_search**，也不要引用任何历史案例或先例——"
        "只按 event_deduce 返回的事实构造结论。",
        "最后按约定字段输出 JSON，用 final_answer 交卷。",
    ]
)


def _settings(base: str, key: str, agent: str) -> NexentSettings:
    return NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)


def _run(base: str, key: str, agent: str, query: str):
    settings = _settings(base, key, agent)
    transport = NexentHttpTransport(settings=settings, query_builder=lambda _payload: query)
    return run(
        settings=settings,
        transport=transport,
        event_id=EVENT_ID,
        as_of=AS_OF,
        labels=LABELS,
        attempts=1,
        out=io.StringIO(),
    )


def _core(answer, baseline: dict) -> dict:
    """可比的结论骨架：结构化字段 + 关键动作族覆盖，不含措辞。"""
    joined = " ".join(answer.action_order) if answer else ""
    keywords: dict[str, list[str]] = baseline.get("key_action_keywords") or {}
    covered = sorted(
        family
        for family in baseline["key_actions"]
        if any(keyword in joined for keyword in keywords.get(family, [family]))
    )
    return {
        "six_hops": list(answer.six_hops) if answer else [],
        "conflicts": sorted(
            (str(item.get("conflict_id")), str(item.get("kind")))
            for item in (answer.conflicts if answer else [])
        ),
        "resource_gap": answer.resource_gap if answer else None,
        "action_families": covered,
    }


def _group(report, baseline: dict) -> dict:
    answer = report.answer
    refs = list(answer.evidence_refs) if answer else []
    return {
        "conversation_id": report.conversation_id,
        "checks_passed": sum(1 for item in report.checks if item.passed),
        "checks_total": len(report.checks),
        "passed": report.passed,
        "errors": list(report.errors),
        "summary": answer.summary if answer else None,
        "evidence_refs": refs,
        "precedent_refs": [ref for ref in refs if ref.startswith(PRECEDENT_PREFIX)],
        "core": _core(answer, baseline),
    }


def compare(base: str, key: str, agent: str) -> dict:
    baseline = offline_baseline(EVENT_ID, AS_OF, labels=LABELS)
    with_precedent = _group(_run(base, key, agent, QUERY_WITH_PRECEDENT), baseline)
    without = _group(_run(base, key, agent, QUERY_WITHOUT_PRECEDENT), baseline)

    both_consistent = bool(with_precedent["passed"]) and bool(without["passed"])
    same_core = with_precedent["core"] == without["core"]
    citation_controlled = bool(with_precedent["precedent_refs"]) and not without["precedent_refs"]

    return {
        "event_id": EVENT_ID,
        "as_of": AS_OF,
        "groups": {"with_precedent": with_precedent, "without_precedent": without},
        "verdict": {
            "both_consistent_with_offline": both_consistent,
            "same_structured_core": same_core,
            "citation_follows_instruction": citation_controlled,
            "passed": both_consistent and same_core and citation_controlled,
        },
        "note": (
            "A/B 用同一事件、同一离线基线。结论骨架相同说明先例没有改事实；"
            "引用只出现在被允许的那一组，说明它由指令控制而非碰巧命中。"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="先例 A/B：两组真机对话对比结论骨架")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_AGENT_NAME", "urban_deduction_agent"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/precedent_ab.json")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY", file=sys.stderr)
        return 2

    record = compare(base=args.base, key=args.key, agent=args.agent)
    target = ROOT / args.json_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    a = record["groups"]["with_precedent"]
    b = record["groups"]["without_precedent"]
    print(f"A 组（允许先例） conv={a['conversation_id']} 八项={a['checks_passed']}/{a['checks_total']} "
          f"先例引用={len(a['precedent_refs'])}")
    print(f"B 组（禁止先例） conv={b['conversation_id']} 八项={b['checks_passed']}/{b['checks_total']} "
          f"先例引用={len(b['precedent_refs'])}")
    print(f"结论骨架相同：{record['verdict']['same_structured_core']}")
    print(f"引用随指令变化：{record['verdict']['citation_follows_instruction']}")
    print(f"总判定：{'通过' if record['verdict']['passed'] else '未通过'}")
    print(f"已写出 {target.relative_to(ROOT).as_posix()}")
    return 0 if record["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
