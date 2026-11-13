"""真机验证：台账问答 Agent 的同会话多跳追问。

为什么这个验收要写成"三问一串"而不是三个独立问题
--------------------------------------------------
追问能力和单问能力在单轮里长得一样。只有把第二、三问**故意省掉主体**
（"它的挡水槛有多高""那谁负责清掏"），才能区分两件事：

- 真的接了上下文：第二问开头会写明"（针对 UP-XH-101）"，并且答出该设施的挡水槛；
- 只是运气好：答成别的隧道、或者反问"你指哪条"，甚至干脆编一个数字。

三问的答案是台账里有确定值的（`underpasses.csv` 的 entry_sill_height_m=0.15、
close_threshold_m=0.25；`department_duties.csv` 的 ORG-DRAIN 负责雨水口清掏），
所以不需要模型当裁判，字符串判据就够。

跑法：

    NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    PYTHONPATH=src python scripts/nexent_asset_qa_multihop.py

退出码：0 三问全过且同一会话；1 任一不成立；2 缺配置。
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

TURNS = [
    {
        "name": "定位设施",
        "question": "UP-XH-101 下穿隧道现在是什么状态？封控阈值是多少？",
        # 首问必须有明确主体与阈值，否则后面的"它"没有指代对象
        "must_contain": ["0.25"],
        "why": "首问必须给出对象与封控阈值 0.25m",
    },
    {
        "name": "省略主体的追问",
        "question": "它的挡水槛有多高？",
        # 主体继承是这一问的判据；答出 0.15 说明查的是同一条隧道
        "must_contain": ["UP-XH-101", "0.15"],
        "why": "第二问故意省掉主体，必须继承 UP-XH-101 并答出挡水槛 0.15m",
    },
    {
        "name": "换话题的追问",
        "question": "那谁负责清掏这条隧道的雨水口？",
        # 答案在 department_duties.csv：ORG-DRAIN 区排水公司
        "must_contain_any": [["ORG-DRAIN", "区排水公司"]],
        "why": "第三问换到职责维度，必须命中 ORG-DRAIN / 区排水公司",
    },
]

SOURCE_MARKERS = ("出处：", "出处:")


def _settings(base: str, key: str, agent: str) -> NexentSettings:
    return NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)


def run_turns(*, base: str, key: str, agent: str) -> dict:
    settings = _settings(base, key, agent)
    transport = NexentHttpTransport(
        settings=settings, query_builder=lambda payload: str(payload.get("question") or "")
    )
    conversation_id = None
    records: list[dict] = []
    for index, turn in enumerate(TURNS, start=1):
        payload = {
            "request_id": f"asset-qa-multihop-{index}-{int(time.time())}",
            "question": turn["question"],
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        # 平台偶发把回答截在半句（生成被截断，不是结论错）。判据是"短到不可能含出处"，
        # 命中就同会话重问一次——这是协议层重试，不是把错误结论刷成通过。
        attempts = 0
        for attempts in range(1, 3):
            result = transport(payload)
            conversation_id = result.get("conversation_id") or conversation_id
            answer = str(result.get("answer") or "")
            if len(answer.strip()) >= 80 and any(m in answer for m in SOURCE_MARKERS):
                break
            payload["request_id"] = f"{payload['request_id']}-retry{attempts}"
        missing = [token for token in turn.get("must_contain", []) if token not in answer]
        for group in turn.get("must_contain_any", []):
            if not any(token in answer for token in group):
                missing.append(" | ".join(group))
        has_source = any(marker in answer for marker in SOURCE_MARKERS)
        records.append(
            {
                "turn": index,
                "name": turn["name"],
                "question": turn["question"],
                "expectation": turn["why"],
                "answer": answer.strip(),
                "missing": missing,
                "has_source": has_source,
                "attempts": attempts,
                "passed": not missing and has_source,
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
            "三问同一会话：第二问刻意省略主体、第三问换维度，判据是字符串而不是模型当裁判。"
            "每问都要求带出处，避免'答对了但没依据'。"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机验证台账问答的多跳追问")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_ASSET_QA_AGENT", "urban_asset_qa"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/asset_qa_multihop.json")
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
        print(f"[{flag}] 第{item['turn']}问 {item['name']}：{item['question']}")
        if item["missing"]:
            print(f"       缺：{item['missing']}")
        print(f"       {item['answer'][:150].replace(chr(10), ' ')}")
    print(f"同一会话：{record['same_conversation']}  conversation_id={record['conversation_id']}")
    print(f"总判定：{'通过' if record['passed'] else '未通过'}")
    print(f"已写出 {target.relative_to(ROOT).as_posix()}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
