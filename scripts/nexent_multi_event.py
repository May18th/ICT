"""主 Agent 多事件真机回归：同一套 8 项判据跑多个不同事件。

为什么需要它
------------
`chain_live.json` 只覆盖 `WO-20261029-007` 一个事件。评委追问「是不是只对这一个工单
调好了」时，单事件证据回答不了。历史那份 `multi_event_regression.json` 采集于
Agent v19，不能拿来当 v34 的结果——版本一变就得重跑，否则等于用旧快照冒充当前状态。

本脚本对每个事件各跑一次真实对话，与离线 `DeductionEngine` 逐项比对 8 项，落盘
`samples/nexent/live/multi_event_regression.json`。**每事件只调一次模型**：解析成功但
结论不一致属于结论错误，重试等于把错答案刷成通过。

跑法（平台在线时）：

    export NEXENT_BASE_URL="http://localhost:5013"
    export NEXENT_API_KEY="<Access Key>"
    uv run --locked python scripts/nexent_multi_event.py
    uv run --locked python scripts/nexent_multi_event.py --events WO-20261029-007   # 只跑一个

退出码：0 全部通过；1 有事件未通过；2 缺配置。
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

from decision_poc.nexent_transport import NexentSettings  # noqa: E402
from decision_poc.nexent_urban import run as run_main  # noqa: E402

#: 与历史快照同口径的四个事件：主推演、基坑+燃气、泵站故障、道路积水。
DEFAULT_EVENTS: tuple[tuple[str, str], ...] = (
    ("WO-20261029-007", "2026-10-29T09:40:00+08:00"),
    ("WO-20261029-009", "2026-10-29T09:55:00+08:00"),
    ("WO-20261030-021", "2026-10-30T10:35:00+08:00"),
    ("WO-20261026-013", "2026-10-26T16:00:00+08:00"),
)

OUT_DEFAULT = "samples/nexent/live/multi_event_regression.json"


def platform_agent_version(agent_name: str) -> int | None:
    """从本地北向快照读当前发布版本号，避免把版本号写成常量。"""
    path = ROOT / "samples" / "nexent" / "live" / "agents.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for item in payload.get("data") or []:
        if item.get("name") == agent_name:
            version = item.get("current_version_no")
            return int(version) if isinstance(version, int) else None
    return None


def _settings(base: str, key: str, agent: str) -> NexentSettings:
    # 不走 from_env：它只认 NEXENT_AGENT_NAME，换 Agent 名会静默失效。
    return NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)


def run_one(event_id: str, as_of: str, settings: NexentSettings) -> dict[str, Any]:
    started = time.time()
    report = run_main(
        settings=settings,
        event_id=event_id,
        as_of=as_of,
        attempts=1,
        out=io.StringIO(),
    )
    elapsed = round(time.time() - started, 1)
    checks = list(report.checks)
    passed = sum(1 for item in checks if item.passed)
    answer = report.answer
    record: dict[str, Any] = {
        "event_id": event_id,
        "as_of": as_of,
        "conversation_id": report.conversation_id,
        "elapsed_s": elapsed,
        "attempts_used": report.attempts_used,
        "checks_passed": f"{passed}/{len(checks)}" if checks else "0/0",
        "failed_checks": [item.name for item in report.failures],
        "passed": report.passed,
        "errors": list(report.errors),
    }
    if report.attempt_errors:
        record["attempt_errors"] = list(report.attempt_errors)
    if answer is not None:
        record["answer_shape"] = {
            "six_hops": len(answer.six_hops),
            "conflicts": [str(item.get("conflict_id")) for item in answer.conflicts],
            "resource_gap": answer.resource_gap,
            "action_count": len(answer.action_order),
            "evidence_refs": len(answer.evidence_refs),
            "handoff_required": answer.handoff_required,
        }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="主 Agent 多事件真机回归")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_AGENT_NAME", "urban_deduction_agent"))
    parser.add_argument("--json", dest="json_path", default=OUT_DEFAULT)
    parser.add_argument("--events", nargs="*", default=None,
                        help="只跑指定 event_id，缺省跑内置四个")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY")
        return 2

    selected = [
        (event_id, as_of)
        for event_id, as_of in DEFAULT_EVENTS
        if args.events is None or event_id in set(args.events)
    ]
    if not selected:
        print(f"没有匹配的事件：{args.events}")
        return 2

    settings = _settings(args.base, args.key, args.agent)
    events: list[dict[str, Any]] = []
    for event_id, as_of in selected:
        record = run_one(event_id, as_of, settings)
        events.append(record)
        print(
            f"[{event_id}] conv={record['conversation_id']} "
            f"{record['checks_passed']} elapsed={record['elapsed_s']}s "
            f"passed={record['passed']}"
            + (f" failed={record['failed_checks']}" if record["failed_checks"] else "")
        )

    ok_count = sum(1 for item in events if item["passed"])
    version = platform_agent_version(args.agent)
    payload = {
        "schema_version": "nexent-multi-event-1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_name": args.agent,
        "agent_version": version,
        "model": "DeepSeek-Chat",
        "method": (
            "北向 /nb/v1/chat/run 实测；每事件一次对话，与离线 DeductionEngine 逐项比对 8 项。"
            "解析成功但结论不一致不重试——那属于结论错误，重试等于把错答案刷成通过。"
        ),
        "note": (
            f"本轮 {ok_count}/{len(events)} 通过；版本号取自 samples/nexent/live/agents.json 的 "
            "current_version_no，不是代码常量。多事件证据只说明这几个工单在同一条链路上一致，"
            "不外推为全量覆盖。"
        ),
        "summary": {"passed": ok_count, "total": len(events)},
        "events": events,
    }

    out = Path(args.json_path)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[multi-event] {ok_count}/{len(events)} 通过，v{version} -> {out}")
    return 0 if ok_count == len(events) else 1


if __name__ == "__main__":
    raise SystemExit(main())
