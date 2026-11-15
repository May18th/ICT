"""系统自洽检查：策略唯一性、冻结集完整、反馈闭环、台账与平台状态。

**为什么单独一个模块**：`/status` 页面与 `python -m decision_poc.evidence_check` 要判同一批事实。
判据写两份，改一处忘一处就会出现"页面亮绿灯、命令行报红"。这里只读文件，不依赖任何页面代码。

**不参与 evidence_check 的项数与指纹**：那两项由 `samples/eval/evidence_expectations.json`
定义（期望文件内容决定指纹）。本模块的结果只作为额外一行自洽摘要打印，改它不会动冻结基线。

**不复制参数逐项比对**：那是 `scripts/policy_sensitivity.py::assert_production_singleton()`
的职责（且它用 SystemExit，被 web 层调用会直接杀进程）。这里只查"有没有依据"。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "samples" / "eval"
PLATFORM_SNAPSHOT = ROOT / "artifacts" / "ledger" / "platform_versions.json"
LEDGER_SYNC = ROOT / "artifacts" / "ledger" / "ledger_sync.json"
FEEDBACK_HISTORY = ROOT / "artifacts" / "feedback" / "history.jsonl"

PRODUCTION_LABEL_EXPECTED = "flood-response-v1.2"
FROZEN_CASE_COUNT = 12


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def platform_versions_payload() -> dict[str, Any]:
    """平台 Agent 版本快照（只读文件，由 snapshot_platform_versions.py 生成）。"""
    if not PLATFORM_SNAPSHOT.is_file():
        return {
            "available": False,
            "hint": "尚未生成记录：python deploy/nexent/snapshot_platform_versions.py",
            "path": str(PLATFORM_SNAPSHOT),
        }
    data = read_json_file(PLATFORM_SNAPSHOT)
    if data is None:
        return {
            "available": False,
            "hint": "记录读取失败（不是合法 JSON）",
            "path": str(PLATFORM_SNAPSHOT),
        }
    return {"available": True, **data, "path": str(PLATFORM_SNAPSHOT)}


def ledger_sync_payload() -> dict[str, Any]:
    """知识库台账同步状态（只读文件，由 check_ledger_sync.py 生成）。"""
    if not LEDGER_SYNC.is_file():
        return {
            "available": False,
            "hint": "尚未查询同步状态：python deploy/nexent/check_ledger_sync.py",
            "path": str(LEDGER_SYNC),
        }
    data = read_json_file(LEDGER_SYNC)
    if data is None:
        return {
            "available": False,
            "hint": "状态文件读取失败（不是合法 JSON）",
            "path": str(LEDGER_SYNC),
        }
    return {"available": True, **data, "path": str(LEDGER_SYNC)}


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def latest_feedback_ts() -> str | None:
    latest: str | None = None
    if not FEEDBACK_HISTORY.is_file():
        return None
    for line in FEEDBACK_HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            latest = json.loads(line).get("ts") or latest
        except json.JSONDecodeError:
            continue
    return latest


def ledger_freshness() -> dict[str, Any]:
    """本地最新反馈有没有已经进到平台台账里。只读两个本地文件，不碰平台。"""
    sync = ledger_sync_payload()
    if not sync.get("available"):
        return {"state": "unknown", "hint": sync.get("hint") or "尚未查询台账同步状态"}

    policy_doc = next(
        (
            doc
            for doc in sync.get("documents") or []
            if str(doc.get("path_or_url", "")).startswith("policy-ledger://")
        ),
        None,
    )
    if not policy_doc:
        return {
            "state": "stale",
            "hint": "平台上还没有策略台账文档：python deploy/nexent/sync_policy_ledger.py --apply",
        }

    feedback_at = _parse_iso(latest_feedback_ts())
    doc_at = _parse_iso(policy_doc.get("platform_created_at"))
    if feedback_at and doc_at and feedback_at > doc_at:
        return {
            "state": "stale",
            "hint": "本地反馈比平台台账新：python deploy/nexent/sync_policy_ledger.py --apply",
        }
    return {
        "state": "fresh",
        "hint": "平台台账已覆盖本地最新反馈",
        "latest_feedback_at": latest_feedback_ts(),
        "platform_document_at": policy_doc.get("platform_created_at"),
    }


def _feedback_run_count() -> int:
    if not FEEDBACK_HISTORY.is_file():
        return 0
    return sum(
        1 for line in FEEDBACK_HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def status_checks() -> list[dict[str, Any]]:
    """四类事实的自洽检查。只读，返回 [{name, state, detail}]。"""
    from .policy_evolution import PRODUCTION_LABEL

    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "生产策略唯一",
            "state": "ok" if PRODUCTION_LABEL == PRODUCTION_LABEL_EXPECTED else "bad",
            "detail": f"PRODUCTION_LABEL = {PRODUCTION_LABEL}",
        }
    )

    memory = read_json_file(EVAL_DIR / "decision_memory.json")
    entries = (
        [item for item in (memory.get("entries") or []) if item.get("label") == PRODUCTION_LABEL]
        if memory
        else []
    )
    checks.append(
        {
            "name": "参数来源明确",
            "state": "ok" if entries else "warn",
            "detail": (
                f"decision_memory.json 命中 {len(entries)} 条（{PRODUCTION_LABEL}）"
                if memory
                else "decision_memory.json 缺失或不可读"
            ),
        }
    )

    cases = read_json_file(EVAL_DIR / "allocation_cases.json")
    case_count = len(cases.get("cases") or []) if cases else 0
    checks.append(
        {
            "name": "评测集完整",
            "state": "ok" if case_count == FROZEN_CASE_COUNT else "bad",
            "detail": f"allocation_cases.json 共 {case_count} 例（期望 {FROZEN_CASE_COUNT}，基线不可增删）",
        }
    )

    review_doc = read_json_file(EVAL_DIR / "decision_reviews.json")
    review_count = len(review_doc.get("reviews") or []) if review_doc else 0
    checks.append(
        {
            "name": "专家评审样例可用",
            "state": "ok" if review_count else "warn",
            "detail": f"decision_reviews.json 共 {review_count} 条（角色扮演，非真实部门批复）",
        }
    )

    run_count = _feedback_run_count()
    checks.append(
        {
            "name": "反馈记录完整",
            "state": "ok" if run_count else "warn",
            "detail": f"history.jsonl 共 {run_count} 条",
        }
    )

    fresh = ledger_freshness()
    checks.append(
        {
            "name": "平台台账已同步",
            "state": {"fresh": "ok", "stale": "warn", "unknown": "warn"}.get(
                str(fresh.get("state")), "warn"
            ),
            "detail": fresh.get("hint") or "—",
        }
    )

    platform = platform_versions_payload()
    agents = platform.get("platform_agents") or []
    checks.append(
        {
            "name": "平台智能体可读",
            "state": "ok" if platform.get("available") and agents else "warn",
            "detail": (
                f"{len(agents)} 个智能体（取数来源 {platform.get('source') or '—'}）"
                if platform.get("available")
                else (platform.get("hint") or "记录缺失")
            ),
        }
    )
    return checks


def summarize(checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = status_checks() if checks is None else checks
    ok = sum(1 for item in items if item["state"] == "ok")
    attention = [item for item in items if item["state"] != "ok"]
    return {
        "total": len(items),
        "ok": ok,
        "attention": len(attention),
        "failed": sum(1 for item in items if item["state"] == "bad"),
        "items": items,
    }


def summary_line() -> str:
    """一行摘要，供 evidence_check 打印（不进项数、不进指纹）。"""
    result = summarize()
    if result["attention"]:
        names = "、".join(item["name"] for item in result["items"] if item["state"] != "ok")
        return (
            f"系统自洽：{result['total']} 项，通过 {result['ok']}，需注意 {result['attention']}"
            f"（{names}）；不计入上方项数与校验值"
        )
    return f"系统自洽：{result['total']} 项全部通过；不计入上方项数与校验值"


__all__ = [
    "ledger_freshness",
    "ledger_sync_payload",
    "platform_versions_payload",
    "read_json_file",
    "status_checks",
    "summarize",
    "summary_line",
]
