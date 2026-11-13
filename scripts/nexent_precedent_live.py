"""真机验证：同一轮对话里既做六跳推演，又引真实先例且不被先例污染。

为什么要单独一个脚本
--------------------
`doc_search` 里现在既有预案条文，也有真实政府的定级记录（2020-08-11 成都）。
这两类东西混在同一个工具里，风险很具体：模型可能把**另一个城市、另一场雨**的
等级/雨量搬过来当本事件的事实，那样六跳结论就脏了。

所以验收要同时看两件事，缺一不可：

1. 八项结论一致性仍全过（先例没有污染 six_hops / conflicts / resource_gap / action_order）；
2. `evidence_refs` 里出现真实定级资产的引用（说明它确实检索了，而不是嘴上说"有先例"）。

只验第 1 项，无法区分"没检索"和"检索了但没乱用"；只验第 2 项，则可能放过污染。

跑法（平台在线、Agent 已发布并挂好 MCP 工具）：

    NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    PYTHONPATH=src python scripts/nexent_precedent_live.py

退出码：0 两项都过；1 任一不成立；2 缺配置。
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
from decision_poc.nexent_urban import DEFAULT_AS_OF, DEFAULT_LABELS, run  # noqa: E402

EVENT_ID = "WO-20261029-007"
AS_OF = DEFAULT_AS_OF
LABELS = list(DEFAULT_LABELS)

#: 真实定级资产的 id 前缀。只认这个前缀，别用"成都"之类的词去猜命中——
#: 检索结果里本来就会带预案条文，按词猜会把"引了预案"误判成"引了先例"。
PRECEDENT_PREFIX = "AS-CD-CITY-emergency_response_case"

QUERY = "\n".join(
    [
        f"城市内涝跨部门事件：{EVENT_ID}，时点 {AS_OF}，调用方身份 {','.join(LABELS)}。",
        "请使用已挂载的 MCP 工具完成六跳推演：",
        "1) event_deduce 一次返回六跳全部事实、三类冲突、资源缺口与处置顺序；",
        "2) 需要核对依据时用 evidence_locate 取原文，不得凭记忆编造。",
        "另外用户追问一句：同类情形以前政府是怎么定级的？库里有真实案例就用 doc_search "
        "查一次并把出处放进 evidence_refs，只作对照，不得改变本事件的判定。",
        "最后按约定字段输出 JSON，用 final_answer 交卷。",
    ]
)


def _settings(base: str, key: str, agent: str) -> NexentSettings:
    return NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)


def verify(*, base: str, key: str, agent: str) -> dict:
    settings = _settings(base, key, agent)
    transport = NexentHttpTransport(settings=settings, query_builder=lambda _payload: QUERY)
    report = run(
        settings=settings,
        transport=transport,
        event_id=EVENT_ID,
        as_of=AS_OF,
        labels=LABELS,
        attempts=1,
        out=io.StringIO(),
    )
    answer = report.answer
    refs = list(answer.evidence_refs) if answer else []
    precedent_refs = [ref for ref in refs if ref.startswith(PRECEDENT_PREFIX)]
    return {
        "event_id": EVENT_ID,
        "as_of": AS_OF,
        "query": QUERY,
        "conversation_id": report.conversation_id,
        "checks": [item.model_dump() for item in report.checks],
        "checks_passed": sum(1 for item in report.checks if item.passed),
        "checks_total": len(report.checks),
        "answer_passed": report.passed,
        "summary": answer.summary if answer else None,
        "evidence_refs": refs,
        "precedent_refs": precedent_refs,
        "precedent_cited": bool(precedent_refs),
        "errors": list(report.errors),
        "note": (
            "同一轮对话里两件事同时成立：六跳与离线基线八项一致（先例未污染结论），"
            "且 evidence_refs 引用了真实定级资产（确实检索过，不是嘴上说'有先例'）。"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机验证：先例被引用且不污染六跳")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_AGENT_NAME", "urban_deduction_agent"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/precedent_live.json")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY", file=sys.stderr)
        return 2

    record = verify(base=args.base, key=args.key, agent=args.agent)
    target = ROOT / args.json_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"六跳八项：{record['checks_passed']}/{record['checks_total']}"
        f"；先例引用：{'有' if record['precedent_cited'] else '无'}"
        f"；conversation_id={record['conversation_id']}"
    )
    for ref in record["precedent_refs"]:
        print(f"  先例出处 {ref}")
    if record["summary"]:
        print(f"  一句话结论：{record['summary'][:160]}")
    ok = record["answer_passed"] and record["precedent_cited"]
    print(f"已写出 {target.relative_to(ROOT).as_posix()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
