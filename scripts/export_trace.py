"""把一次推演导出成可带走的审计 trace。

为什么单独一个脚本
------------------
大屏左侧已经写了"读了哪本账、哪条规则、哪行原文"，但那是渲染结果：关掉页面就没了，
评委也没法拿它去核。这个脚本把同一次运行的过程落成两份文件：

- `<trace_id>.jsonl`：一行一条事件，机器读（导入 Excel / 日志平台 / 审计对账）；
- `<trace_id>.md`：按阶段分节的人读版，每步列规则、依据、耗时、是否降级。

边界（与本文件同样重要）
------------------------
1. **只读**：不重跑规则、不重算结论、不写执行台账（`persist=False`）；
2. **不新增数字**：所有数都来自这一次运行的返回值，脚本不做任何统计加工；
3. **缺就写缺**：某步降级或报错，原样落 `status` / `error_code` / `degraded`，不补空话。

用法
----
```powershell
$env:PYTHONPATH = "src"
uv run --locked python scripts/export_trace.py                      # 默认事件
uv run --locked python scripts/export_trace.py --out out/trace      # 指定目录
uv run --locked python scripts/export_trace.py --stdout             # 只打印摘要
```
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.skills import SkillPipeline  # noqa: E402
from decision_poc.timeline import SKILL_TOOL  # noqa: E402
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT  # noqa: E402

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
LABELS = ("ORG-EMERG",)

#: 阶段名 → 评委能读的中文（与 `/map2d` 字幕同口径，避免两处各译一套）
STAGE_LABEL = {
    "observed": "发生了什么",
    "affected": "影响对象",
    "owner": "责任归属",
    "conflict": "冲突识别",
    "resource": "资源缺口",
    "action": "处置动作",
    "audit": "结果复算",
    "handoff": "交接",
}

STATUS_LABEL = {
    "ok": "有据",
    "supported": "有据",
    "conflict": "暴露冲突",
    "insufficient": "证据不足",
    "missing": "缺口",
    "error": "报错",
    "partial": "部分降级",
}


def _split_ref(ref: str) -> dict[str, str]:
    """`AS-...-csv#line:12` → {asset_id, locator}；无片段时 locator 留空。"""

    asset_id, sep, fragment = ref.partition("#")
    return {"ref": ref, "asset_id": asset_id, "locator": fragment if sep else ""}


def build_trace(
    event_id: str = EVENT_ID,
    as_of: str = AS_OF,
    labels: Sequence[str] = LABELS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """跑一次（只读）并把过程铺成事件列表。返回 (events, summary)。"""

    started = datetime.now().astimezone()
    trace_id = f"trace-{started:%Y%m%dT%H%M%S}-{event_id}"
    report = SkillPipeline().run(event_id, as_of, caller_labels=list(labels), persist=False)
    deduction = report.deduction

    events: list[dict[str, Any]] = []

    # ① 运行头：一次运行一行，后面所有事件都挂在这个 trace_id 下
    events.append(
        {
            "kind": "run",
            "trace_id": trace_id,
            "started_at": started.isoformat(timespec="seconds"),
            "event_id": report.event_id,
            "as_of": report.as_of.isoformat(),
            "caller_labels": list(report.caller_labels),
            "dataset_root": str(DEFAULT_DATASET_ROOT),
            "snapshot_hash": report.snapshot_hash,
            "knowledge_version": report.knowledge_version,
            "rules_version": deduction.rules_version if deduction else "",
            "pipeline_status": report.status,
            "publishable": report.publishable,
            "handoff_required": report.handoff_required,
            "handoff_reasons": list(report.handoff_reasons),
            "stop_reason": report.stop_reason,
        }
    )

    # ② 工具调用：五项技能各自调了哪个 MCP 工具、耗时多少、有没有降级
    for seq, step in enumerate(report.steps, 1):
        tool, actor = SKILL_TOOL.get(step.skill, (step.skill, "主 Agent"))
        events.append(
            {
                "kind": "tool_call",
                "trace_id": trace_id,
                "seq": seq,
                "skill": step.skill,
                "stage": step.stage,
                "stage_label": STAGE_LABEL.get(step.stage, step.stage),
                "tool": tool,
                "actor": actor,
                "status": step.status,
                "elapsed_ms": step.elapsed_ms,
                "error_code": step.error_code,
                "degraded": list(step.degraded),
            }
        )

    # ③ 六跳：每跳的规则、依据、结论。这一层就是"读了哪条规则、哪行原文"
    if deduction is not None:
        for seq, hop in enumerate(deduction.stages, 1):
            events.append(
                {
                    "kind": "hop",
                    "trace_id": trace_id,
                    "seq": seq,
                    "stage": hop.stage,
                    "stage_label": STAGE_LABEL.get(hop.stage, hop.stage),
                    "status": hop.status,
                    "status_label": STATUS_LABEL.get(hop.status, hop.status),
                    "rule_ids": list(hop.rule_ids),
                    "evidence": [_split_ref(ref) for ref in hop.evidence_refs],
                    "conclusion": dict(hop.conclusion),
                    "detail": hop.detail,
                }
            )

        for item in deduction.conflicts:
            events.append(
                {
                    "kind": "conflict",
                    "trace_id": trace_id,
                    "conflict_id": item.conflict_id,
                    "conflict_kind": str(item.kind),
                    "subject": item.subject,
                    "orgs": list(item.orgs),
                    "rule_ids": list(item.rule_ids),
                    "evidence": [_split_ref(ref) for ref in item.evidence_refs],
                    "detail": item.detail,
                }
            )

        for item in deduction.resource_gaps:
            events.append(
                {
                    "kind": "resource_gap",
                    "trace_id": trace_id,
                    "resource_id": item.resource_id,
                    "required": item.required,
                    "available": item.available,
                    "gap": item.gap,
                    "unit": item.unit,
                    "rule_ids": list(item.rule_ids),
                    "evidence": [_split_ref(ref) for ref in item.evidence_refs],
                }
            )

    # ④ 审计：每条宣称过没过、被拒的理由、哈希链是否完整
    audit = report.audit
    if audit is not None:
        for claim in audit.claims:
            events.append(
                {
                    "kind": "claim",
                    "trace_id": trace_id,
                    "claim_id": claim.claim_id,
                    "claim_kind": claim.kind,
                    "supported": claim.supported,
                    "reasons": list(claim.reasons),
                    "rule_id": claim.rule_id,
                    "method_ref": claim.method_ref,
                    "evidence": [_split_ref(ref) for ref in claim.evidence_refs],
                }
            )
        events.append(
            {
                "kind": "audit",
                "trace_id": trace_id,
                "decision_id": audit.decision_id,
                "supported": audit.supported,
                "rejected": audit.rejected,
                "citation_completeness": audit.citation_completeness,
                "record_id": audit.record_id,
                "record_hash": audit.record_hash,
                "chain_ok": audit.chain_ok,
                "assets": audit.assets,
            }
        )

    summary = {
        "trace_id": trace_id,
        "event_id": report.event_id,
        "pipeline_status": report.status,
        "publishable": report.publishable,
        "handoff_required": report.handoff_required,
        "tool_calls": sum(1 for item in events if item["kind"] == "tool_call"),
        "hops": sum(1 for item in events if item["kind"] == "hop"),
        "conflicts": sum(1 for item in events if item["kind"] == "conflict"),
        "resource_gaps": sum(1 for item in events if item["kind"] == "resource_gap"),
        "claims_supported": sum(
            1 for item in events if item["kind"] == "claim" and item["supported"]
        ),
        "claims_rejected": sum(
            1 for item in events if item["kind"] == "claim" and not item["supported"]
        ),
        "chain_ok": audit.chain_ok if audit else None,
        "record_hash": audit.record_hash if audit else None,
        "events": len(events),
    }
    return events, summary


def _render_markdown(events: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    head = next(item for item in events if item["kind"] == "run")
    lines = [
        f"# 推演审计 trace · {head['event_id']}",
        "",
        f"- trace_id：`{head['trace_id']}`",
        f"- 时点：{head['as_of']}（调用方身份 {','.join(head['caller_labels']) or '—'}）",
        f"- 数据版本：`knowledge_version={head['knowledge_version']}` · "
        f"`snapshot_hash={head['snapshot_hash'][:12]}` · `rules_version={head['rules_version']}`",
        f"- 流水线状态：**{head['pipeline_status']}** · 可发布 `{head['publishable']}` · "
        f"需人工接手 `{head['handoff_required']}`",
    ]
    if head["stop_reason"]:
        lines.append(f"- 停止原因：{head['stop_reason']}")
    if head["handoff_reasons"]:
        lines.append(f"- 交接理由：{'；'.join(head['handoff_reasons'])}")

    lines += ["", "## 工具调用", "", "| # | 技能 | 工具 | 状态 | 耗时 ms | 降级 |", "|---:|---|---|---|---:|---|"]
    for item in events:
        if item["kind"] != "tool_call":
            continue
        degraded = "、".join(item["degraded"]) or "—"
        lines.append(
            f"| {item['seq']} | {item['skill']} | `{item['tool']}` | "
            f"{item['status']} | {item['elapsed_ms']} | {degraded} |"
        )

    hops = [item for item in events if item["kind"] == "hop"]
    if hops:
        lines += ["", "## 分步依据（六跳 + 复算/交接）", ""]
        for item in hops:
            lines.append(
                f"### {item['seq']}. {item['stage_label']}（`{item['stage']}`）· "
                f"{item['status_label']} · {item['status']}"
            )
            if item["rule_ids"]:
                lines.append(f"- 规则：{'、'.join(f'`{rule}`' for rule in item['rule_ids'])}")
            if item["evidence"]:
                lines.append("- 依据：")
                for ref in item["evidence"]:
                    lines.append(f"  - `{ref['asset_id']}` → `{ref['locator'] or '—'}`")
            if item["conclusion"]:
                pairs = "；".join(f"{key}={value}" for key, value in item["conclusion"].items())
                lines.append(f"- 结论：{pairs}")
            if item["detail"]:
                lines.append(f"- 说明：{item['detail']}")
            lines.append("")

    conflicts = [item for item in events if item["kind"] == "conflict"]
    if conflicts:
        lines += ["## 冲突", ""]
        for item in conflicts:
            lines.append(
                f"- `{item['conflict_id']}`（{item['conflict_kind']}）主体 {item['subject']}："
                f"{'、'.join(item['orgs'])} · 规则 {'、'.join(item['rule_ids']) or '—'}"
            )
        lines.append("")

    gaps = [item for item in events if item["kind"] == "resource_gap"]
    if gaps:
        lines += ["## 资源缺口", ""]
        for item in gaps:
            lines.append(
                f"- `{item['resource_id']}`：需 {item['required']} / 有 {item['available']} / "
                f"缺 {item['gap']} {item['unit']}"
            )
        lines.append("")

    audit = next((item for item in events if item["kind"] == "audit"), None)
    if audit is not None:
        lines += [
            "## 审计",
            "",
            f"- 判定号：`{audit['decision_id']}` · 有据 {audit['supported']} · 被拒 {audit['rejected']} · "
            f"引用完整率 {audit['citation_completeness']}",
            (
                f"- 哈希链：`{audit['record_hash']}` · 完整 `{audit['chain_ok']}`"
                if audit["record_hash"]
                else f"- 哈希链：本次为只读导出，未写入执行台账，故无记录号（`chain_ok={audit['chain_ok']}` 指审计侧校验结果）"
            ),
        ]
        rejected = [item for item in events if item["kind"] == "claim" and not item["supported"]]
        if rejected:
            lines += ["", "被拒条目：", ""]
            for item in rejected:
                lines.append(f"- `{item['claim_id']}`：{'；'.join(item['reasons']) or '未给理由'}")
        lines.append("")

    lines += [
        "## 摘要",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2),
        "```",
        "",
        "> 本 trace 只记录一次运行的过程，不含任何推测值；某步缺失即标 `insufficient` / `error`。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出一次推演的审计 trace（只读）")
    parser.add_argument("--event", default=EVENT_ID)
    parser.add_argument("--as-of", default=AS_OF)
    parser.add_argument("--labels", default=",".join(LABELS), help="逗号分隔的调用方组织标识")
    parser.add_argument("--out", default="out/trace", help="输出目录")
    parser.add_argument("--stdout", action="store_true", help="只打印摘要，不落盘")
    args = parser.parse_args(argv)

    labels = tuple(item.strip() for item in args.labels.split(",") if item.strip())
    events, summary = build_trace(args.event, args.as_of, labels)

    if not args.stdout:
        out_dir = Path(args.out)
        if not out_dir.is_absolute():
            out_dir = ROOT / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = out_dir / f"{summary['trace_id']}.jsonl"
        md_path = out_dir / f"{summary['trace_id']}.md"
        jsonl_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
            encoding="utf-8",
        )
        md_path.write_text(_render_markdown(events, summary), encoding="utf-8")
        print(f"jsonl: {jsonl_path}")
        print(f"md   : {md_path}")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["pipeline_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
