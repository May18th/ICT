"""Nexent 平台真机验证：LLM 决策对照任务（N01）经平台 Agent 实跑。

通道：北向 /nb/v1/chat/run（SSE），与真机 8/8 六跳验证同一链路；
任务构造与 web 端点 /api/llm-decision、P8 A/B 评测完全同源（llm_ab.build_task）。

用法：
    $env:NEXENT_API_KEY = "<发布 urban_llm_decision_agent 后生成的 Access Key>"
    $env:NEXENT_AGENT_NAME = "urban_llm_decision_agent"
    uv run --group llm python scripts/nexent_llm_decision_live.py

产出：samples/nexent/live/llm_decision_platform.json（真机证据，随发布包走）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from decision_poc.evidence_resolver import make_ref_resolver  # noqa: E402
from decision_poc.llm_ab import build_task  # noqa: E402
from decision_poc.nexent_adapter import NexentAdapter  # noqa: E402
from decision_poc.nexent_transport import (  # noqa: E402
    NexentError,
    NexentHttpTransport,
    NexentSettings,
)
from decision_poc.skills import SkillPipeline, SkillRuntime  # noqa: E402
from decision_poc.urban_eval import load_cases  # noqa: E402

OUT_PATH = ROOT / "samples" / "nexent" / "live" / "llm_decision_platform.json"
DEMO_CASE_ID = "N01"


def main() -> int:
    try:
        settings = NexentSettings.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"配置无效: {exc}")
        return 2

    case = next((c for c in load_cases() if c.case_id == DEMO_CASE_ID), None)
    if case is None:
        print(f"找不到用例 {DEMO_CASE_ID}")
        return 2
    report = SkillPipeline(SkillRuntime()).run(
        case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
    )
    # 证据要带原文：只给"第1优先动作 + 出处字符串"，模型看不到规程条款与施工计划行，
    # 就会按选项里的表面阈值（30cm ≥ 25cm）自行改判（2026-11-06 复测 6 次里 2 次如此）。
    task = build_task(case, report, resolve_ref=make_ref_resolver(case.caller_labels))

    started = time.perf_counter()
    try:
        result = NexentAdapter(invoke=NexentHttpTransport(settings=settings)).run(task.request)
    except NexentError as exc:
        print(f"平台调用失败: {type(exc).__name__}: {exc}")
        return 1
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    decision = result.get("decision")
    decision_dict = decision if isinstance(decision, dict) else None
    record = {
        "schema_version": "nexent-llm-decision-live-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "method": "北向 /nb/v1/chat/run 实测；Agent=urban_llm_decision_agent（DeepSeek-Chat，独立 Agent，不动 v10）",
        "nexent_base_url": settings.base_url,
        "agent_name": settings.agent_name,
        "case_id": case.case_id,
        "event_id": case.event_id,
        "as_of": case.as_of,
        "request_id": task.request.request_id,
        "expected_option_id": task.expected_option_id,
        "options": task.options,
        "task_digest": hashlib.sha256(
            json.dumps(task.request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16],
        "latency_ms": elapsed_ms,
        "schema_valid": decision is not None,
        "selected_option_id": (decision_dict or {}).get("selected_option_id"),
        "confidence": (decision_dict or {}).get("confidence"),
        "summary": str((decision_dict or {}).get("summary") or "")[:300],
        "risks": (decision_dict or {}).get("risks") or [],
        "evidence_cited": len((decision_dict or {}).get("evidence") or []),
        "decision_error": result.get("decision_error"),
        "answer_length": len(str(result.get("answer") or "")),
    }
    record["selected_match"] = (
        record["selected_option_id"] == task.expected_option_id
        if record["selected_option_id"]
        else None
    )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"schema_valid={record['schema_valid']} "
        f"selected={record['selected_option_id']} "
        f"expected={record['expected_option_id']} "
        f"match={record['selected_match']} latency={elapsed_ms}ms"
    )
    print(f"证据已固化: {OUT_PATH}")
    return 0 if record["schema_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
