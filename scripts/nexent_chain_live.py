"""主 Agent → 协同 Agent 一条链的真机验证。

为什么需要它
------------
两个 Agent 各自都有真机证据：主 Agent 8/8、协同 Agent 2/2。但那是**两次独立会话**，
评委问"这是个协同系统，还是两个各跑各的 Agent"时，只能掏两段日志靠人脑拼——
这正是"两个 Agent 各跑各的"这个质疑的口子。

本脚本在一次运行里按顺序做三件事，把链路固化成一份带时间戳的证据：

1. 主 Agent（urban_deduction_agent）调 `event_deduce` 出六跳；
2. 拿同一个 `event_id` 让协同 Agent（urban_collab_agent）调 `collab_run` 复核；
3. 校验两侧指向同一事件、内核指纹与离线逐字一致，落盘
   `samples/nexent/live/chain_live.json`。

跑法（平台在线时）：

    $env:NEXENT_BASE_URL="http://localhost:5013"
    $env:NEXENT_API_KEY="<Access Key>"
    uv run --locked python scripts/nexent_chain_live.py

退出码：0 两环都过；1 任一环失败；2 缺配置。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings  # noqa: E402
from decision_poc.nexent_urban import DEFAULT_AS_OF, DEFAULT_LABELS, run as run_main  # noqa: E402
from nexent_collab_live import build_collab_query, judge, offline_reference  # noqa: E402

EVENT_ID = "WO-20261029-007"
AS_OF = DEFAULT_AS_OF
LABELS = list(DEFAULT_LABELS)


def _settings(base: str, key: str, agent: str) -> NexentSettings:
    # 不走 from_env：它只认 NEXENT_AGENT_NAME，会让 --collab-agent 静默失效，
    # 表现为"改了半天 agent 名、跑的还是默认那个"（nexent_collab_live 踩过）。
    return NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)


def run_chain(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base
    key = args.key
    steps: list[dict[str, Any]] = []

    started = time.time()
    main_report = run_main(
        settings=_settings(base, key, args.main_agent),
        event_id=EVENT_ID,
        as_of=AS_OF,
        labels=LABELS,
        attempts=1,
        out=io.StringIO(),
    )
    steps.append(
        {
            "step": 1,
            "agent": args.main_agent,
            "tool": "event_deduce",
            "conversation_id": main_report.conversation_id,
            "elapsed_s": round(time.time() - started, 1),
            "passed": main_report.passed,
            "checks": len(main_report.checks),
            "failures": [item.name for item in main_report.failures],
            "errors": list(main_report.errors),
            "event_id": main_report.event_id,
        }
    )

    offline = offline_reference()
    transport = NexentHttpTransport(
        settings=_settings(base, key, args.collab_agent), query_builder=build_collab_query
    )
    started = time.time()
    record: dict[str, Any] = {"step": 2, "agent": args.collab_agent, "tool": "collab_run"}
    try:
        result = transport(
            {
                "request_id": f"chain-live-{int(started)}",
                "event_id": EVENT_ID,
                "as_of": AS_OF,
                "caller_labels": LABELS,
            }
        )
        record["conversation_id"] = result.get("conversation_id")
        record["elapsed_s"] = round(time.time() - started, 1)
        record["event_id"] = EVENT_ID
        judge(result.get("answer") or "", offline, record)
        record["passed"] = bool(record.get("json_parsed")) and not record.get("mismatches")
    except Exception as exc:  # 网络/流中断都算失败，如实记录，不让异常变成"没跑过"
        record["conversation_id"] = None
        record["elapsed_s"] = round(time.time() - started, 1)
        record["passed"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    steps.append(record)

    main_ok = bool(steps[0]["passed"])
    collab_ok = bool(steps[1]["passed"])
    same_event = steps[0].get("event_id") == steps[1].get("event_id") == EVENT_ID
    return {
        "event_id": EVENT_ID,
        "as_of": AS_OF,
        "chain": steps,
        "summary": {
            "main_passed": main_ok,
            "collab_passed": collab_ok,
            "same_event": same_event,
            "chain_ok": main_ok and collab_ok and same_event,
            "offline_kernel_fingerprint": offline.get("kernel_fingerprint"),
            "offline_cards": offline.get("cards"),
        },
        "note": (
            "两环在**一次运行内串联**：主 Agent 出结论后，协同 Agent 用同一 event_id 复核。"
            "与 /api/collab-review 的离线复核同源，指纹应逐字一致。"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="主 Agent → 协同 Agent 一条链的真机验证")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--main-agent", default=os.getenv("NEXENT_AGENT_NAME", "urban_deduction_agent"))
    parser.add_argument("--collab-agent", default=os.getenv("NEXENT_COLLAB_AGENT", "urban_collab_agent"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/chain_live.json")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY")
        return 2

    payload = run_chain(args)
    out = Path(args.json_path)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for step in payload["chain"]:
        print(f"[step {step['step']}] {step['agent']} -> {step['tool']} "
              f"conv={step.get('conversation_id')} elapsed={step.get('elapsed_s')}s "
              f"passed={step.get('passed')}")
    print(f"[chain] {'OK' if payload['summary']['chain_ok'] else 'FAIL'} -> {out}")
    return 0 if payload["summary"]["chain_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
