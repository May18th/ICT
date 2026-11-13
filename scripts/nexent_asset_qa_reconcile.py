"""真机验证：台账问答的"跨表对账"——同一个问题，台账与预案各说各的。

为什么要这一组
--------------
多跳追问验证的是"能不能接上文"，这一组验证的是**"敢不敢说两边不一致"**。
台账问答最容易被模型糊过去的场景，正是两边口径不齐的时候：它会挑一方的说法
当成"综合结论"，读起来通顺，但把缺口抹平了。

一例真实缺口（就在本仓库的数据里）：

- `drainage_facilities.csv` 的 DF-PS-01：`owner_org` 为空，备注"运维主体未明确"；
- `department_duties.csv` 的 ORG-DRAIN：备注"泵站运维主体未在本文件界定"；
- `plan_flood_emergency_v2.md` §6 职责索引：明写"本预案未界定雨水泵站的日常运维与
  故障抢修责任主体，相关事项按现行设施移交协议执行"。

三方一致地指向"空白"，而"设施移交协议"这份文件**不在资产里**。所以合格的回答必须
说"未界定 / 见移交协议"，而不能替任何一方认领责任主体——认领了就是编。

第二例是数值一致但依据强弱不同：`underpasses.csv` 封控阈值 0.25m（备注自述为
假设值、待核实），预案 §3.1 写的是 ≥25cm 封路。数值口径一致，但一方自认是假设值，
回答要么并列引用两侧写法，要么明说依据强度不同。

判据全部是字符串与"出处条数"，不请模型当裁判。

跑法：

    NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    PYTHONPATH=src python scripts/nexent_asset_qa_reconcile.py

退出码：0 全过；1 任一不成立；2 缺配置。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings  # noqa: E402

SOURCE_MARKERS = ("出处：", "出处:")

TURNS = [
    {
        "name": "职责空白（三方都说没有）",
        "question": "DF-PS-01 泵站日常运维和故障抢修是谁负责？台账和预案的说法一致吗？",
        # 不能认领主体：必须落到"未界定/未明确"，并指出预案把责任推给了移交协议
        "must_contain": ["DF-PS-01"],
        "must_contain_any": [["未界定", "未明确", "未指定"], ["移交协议", "未在本文件界定"]],
        "min_sources": 2,
        "why": "三方都缺 → 必须答未界定并引移交协议，不许替谁认领",
    },
    {
        "name": "数值一致但依据强弱不同",
        "question": "兴华路下穿隧道的封控阈值，和预案里的封路阈值一致吗？",
        # 台账 0.25m / 预案 25cm，两侧写法都要出现，不换算成同一个
        "must_contain": ["0.25", "25", "假设值"],
        # 判"不一致"就是错：0.25m 与 25cm 是同一个数，写法不同不等于口径冲突。
        # （上一版把断言写成 must_contain ["一致"]，被"不一致"这个子串蒙过去了一次。）
        "must_not_contain": ["不一致"],
        "min_sources": 2,
        "why": "0.25m 与 25cm 数值一致（写法不同），只可注明一侧自述假设值，不许判不一致",
    },
    {
        "name": "数值对账（事件值 vs 预案阈值）",
        "question": "本次积水 30cm，按预案属于几级响应？台账里有没有记录这个判定阈值？",
        "must_contain": ["橙色"],
        "must_contain_any": [["25"], ["10"]],
        "min_sources": 1,
        "why": "预案 23-24 行：≥25cm 为橙色；回答要引预案阈值，不能自己推",
    },
]


def _count_sources(text: str) -> int:
    return sum(text.count(marker) for marker in SOURCE_MARKERS)


def run_turns(*, base: str, key: str, agent: str) -> dict:
    settings = NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)
    transport = NexentHttpTransport(
        settings=settings, query_builder=lambda payload: str(payload.get("question") or "")
    )
    conversation_id = None
    records: list[dict] = []
    for index, turn in enumerate(TURNS, start=1):
        payload = {
            "request_id": f"asset-qa-reconcile-{index}-{int(time.time())}",
            "question": turn["question"],
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        # 平台偶发把回答截在半句：判据是"短到不可能带出处"，命中就同会话重问一次。
        attempts = 0
        for attempts in range(1, 3):
            result = transport(payload)
            conversation_id = result.get("conversation_id") or conversation_id
            answer = str(result.get("answer") or "")
            if len(answer.strip()) >= 80 and _count_sources(answer) >= 1:
                break
            payload["request_id"] = f"{payload['request_id']}-retry{attempts}"

        missing = [token for token in turn.get("must_contain", []) if token not in answer]
        missing += [f"不应出现：{token}" for token in turn.get("must_not_contain", []) if token in answer]
        for group in turn.get("must_contain_any", []):
            if not any(token in answer for token in group):
                missing.append(" | ".join(group))
        sources = _count_sources(answer)
        if sources < int(turn.get("min_sources", 1)):
            missing.append(f"出处≥{turn['min_sources']}（实际 {sources}）")
        records.append(
            {
                "turn": index,
                "name": turn["name"],
                "question": turn["question"],
                "expectation": turn["why"],
                "answer": answer.strip(),
                "missing": missing,
                "sources": sources,
                "attempts": attempts,
                "passed": not missing,
                "conversation_id": result.get("conversation_id"),
            }
        )

    same_conversation = len({item["conversation_id"] for item in records}) == 1 and bool(
        records[0]["conversation_id"]
    )
    return {
        "agent": agent,
        "conversation_id": conversation_id,
        "turns": records,
        "same_conversation": same_conversation,
        "passed": same_conversation and all(item["passed"] for item in records),
        "note": (
            "对账模式的判据是'敢不敢说空'：三方都缺时必须答未界定并引移交协议，"
            "不许替任何一方认领主体；数值一致时两侧写法要并列，不换算成统一口径。"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机验证台账问答的跨表对账")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_ASSET_QA_AGENT", "urban_asset_qa"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/asset_qa_reconcile.json")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY", file=sys.stderr)
        return 2

    record = run_turns(base=args.base, key=args.key, agent=args.agent)
    target = ROOT / args.json_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for item in record["turns"]:
        flag = "PASS" if item["passed"] else "FAIL"
        print(f"[{flag}] 第{item['turn']}问 {item['name']}（出处 {item['sources']} 条）")
        if item["missing"]:
            print(f"       缺：{item['missing']}")
        print(f"       {item['answer'][:220].replace(chr(10), ' ')}")
    print(f"同一会话：{record['same_conversation']}  conversation_id={record['conversation_id']}")
    print(f"总判定：{'通过' if record['passed'] else '未通过'}")
    print(f"已写出 {target.relative_to(ROOT).as_posix()}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
