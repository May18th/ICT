"""人工协同 vs 智能体辅助：可复算的结构性对照。

为什么不做成"我们省了 30 分钟"
------------------------------
耗时必须真人测（`docs/competition/value-evidence.md` §4 有 15 分钟的做法），
没测就不能写进材料——这是本仓库的硬规矩。但"人工要逐个确认几个部门、要翻几行台账、
要读多少字规程"是从数据里算出来的，不依赖任何人计时，也不怕评委追问。

所以本模块只做两类事：

1. **算得出来的**：部门数、跨部门确认次数下限、台账行数、规程字数、证据可追溯性；
2. **算不出来的**：耗时一律留 `None`，并写明"未实测 + 方法在哪"。

对照表里凡是 `None` 的格子，就是还没做的那一件事，不许用估算值填。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .urban_dataset import DEFAULT_DATASET_ROOT
from ._shared import dataset_root, read_csv_rows

#: 人工值守时要翻的台账。数字直接来自行数，不凭印象写"十几张表"。
LEDGER_FILES = (
    "work_orders.csv",
    "road_network.csv",
    "drainage_facilities.csv",
    "resource_inventory.csv",
    "construction_plans.csv",
    "underpasses.csv",
    "weather_alerts.csv",
)

#: 人工要读的规程与预案。字数用来对照"智能体检索命中几条"。
PROCEDURE_FILES = (
    "procedure_waterlogging.md",
    "procedure_gas_protection.md",
    "plan_flood_emergency_v2.md",
)


def _root(root: Path | str | None = None) -> Path:
    return dataset_root(root, DEFAULT_DATASET_ROOT)


def _rows(path: Path) -> list[dict[str, str]]:
    return read_csv_rows(path)


def build_manual_baseline(root: Path | str | None = None) -> dict[str, Any]:
    """人工协同的结构性基线：全部按台账与预案可复算，不含任何计时。"""
    base = _root(root)
    duties = _rows(base / "department_duties.csv")
    orgs = [row for row in duties if str(row.get("org_id") or "").strip()]
    # 权限交叉/需联合发布的条目：这类条目每多一条，人工就多一次"找谁签字"的往返
    cross = [
        row
        for row in duties
        if str(row.get("escalation_note") or "").strip()
    ]

    ledger_detail: dict[str, int] = {}
    for name in LEDGER_FILES:
        ledger_detail[name.replace(".csv", "")] = len(_rows(base / name))
    ledger_rows = sum(ledger_detail.values())

    procedure_chars = 0
    procedure_detail: dict[str, int] = {}
    for name in PROCEDURE_FILES:
        path = base / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            procedure_detail[name] = len(text)
            procedure_chars += len(text)

    return {
        "organizations": len(orgs),
        "organizations_detail": [
            {"org_id": row.get("org_id"), "org_name": row.get("org_name"), "authority": row.get("authority")}
            for row in orgs
        ],
        # 下限：每个参与部门至少一次确认。真实场景还要加上找人、等回复、反复解释的时间。
        "confirm_calls_min": len(orgs),
        "cross_authority_notes": [
            {"org_id": row.get("org_id"), "note": row.get("escalation_note")} for row in cross
        ],
        "cross_authority_count": len(cross),
        "ledger_rows": ledger_rows,
        "ledger_detail": ledger_detail,
        "procedure_chars": procedure_chars,
        "procedure_detail": procedure_detail,
        "traceability": "无强制留痕：结论依赖个人经验与会议记录，抽查才追溯",
        # 算不出来的就别算。这一栏保持 None，等 §4 的实测回填。
        "duration_minutes": None,
        "duration_note": "未实测。3 人各做一次、记录首条动作与完整方案时间，方法见 docs/competition/value-evidence.md §4",
    }


def build_agent_baseline(
    payload: Mapping[str, Any],
    *,
    collab: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """智能体侧的结构性基线：工具数、调用次数、证据条数、计算耗时。"""
    steps = list(payload.get("steps") or [])
    deduction = payload.get("deduction") or {}
    refs: set[str] = set()
    for claim in (payload.get("audit") or {}).get("claims") or []:
        refs.update(str(item) for item in claim.get("evidence_refs") or [])
    for step in deduction.get("stages") or []:
        refs.update(str(item) for item in step.get("evidence_refs") or [])

    tool_count = 0
    try:
        from .mcp_server import UrbanToolkit

        tool_count = len(UrbanToolkit().tools())
    except Exception:  # 工具清单取不到不影响对照表主干
        tool_count = 0

    collab_counts = (collab or {}).get("counts") or {}
    elapsed_total = sum(int(step.get("elapsed_ms") or 0) for step in steps)

    return {
        "tools_registered": tool_count,
        "tool_calls": len(steps),
        "stages": len(deduction.get("stages") or []),
        "review_agents": collab_counts.get("agents", 0),
        "review_cards": collab_counts.get("cards", 0),
        "handoffs": collab_counts.get("handoffs", 0),
        "evidence_refs": len(refs),
        "citation_completeness": (payload.get("audit") or {}).get("citation_completeness"),
        # 这是**计算耗时**，不是协同耗时。两者不能混为一谈，所以字段名写死。
        "compute_ms": elapsed_total,
        "compute_note": "本机单次流水线的 skill 执行耗时合计，不含人工确认与网络等待",
    }


def compare(
    payload: Mapping[str, Any],
    *,
    collab: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """并排输出，并说明哪些是算出来的、哪些还没测。"""
    manual = build_manual_baseline(root)
    agent = build_agent_baseline(payload, collab=collab)

    statements = [
        (
            f"人工至少要逐个确认 {manual['organizations']} 个部门"
            f"（其中 {manual['cross_authority_count']} 条职责有权限交叉，需联合签署）；"
            f"智能体 1 次复核覆盖 {agent['review_agents']} 个部门角色、产出 {agent['review_cards']} 张动作卡"
        ),
        (
            f"人工需翻阅 {manual['ledger_rows']} 行台账、读 {manual['procedure_chars']} 字规程/预案；"
            f"智能体按 event_id 检索，{agent['evidence_refs']} 条证据引用逐条带行号"
        ),
        (
            f"智能体本次 {agent['tool_calls']} 次工具调用、计算耗时合计 {agent['compute_ms']} ms（本机实测）；"
            "人工协同耗时尚未实测，故不宣称分钟数"
        ),
        (
            f"结论可追溯性：人工侧「{manual['traceability']}」；"
            f"智能体侧引用完整率 {agent['citation_completeness']}"
        ),
    ]

    gaps = []
    if manual["duration_minutes"] is None:
        gaps.append("人工协同耗时（需 3 人各测一次，value-evidence §4）")
    if not agent.get("citation_completeness"):
        gaps.append("引用完整率（本次 payload 未产出审计字段）")

    return {
        "event_id": payload.get("event_id"),
        "manual": manual,
        "agent": agent,
        "statements": statements,
        "unmeasured": gaps,
        "rule": "耗时未实测就留空，不用估算值填表——被追问时要能说清哪一栏是算的、哪一栏是测的",
        "source": "台账 CSV 行数 + deduction.stages + collab.orchestrate（全部可复算）",
    }


__all__ = ["build_agent_baseline", "build_manual_baseline", "compare"]


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI 薄壳
    """`python -m decision_poc.baseline_compare`：打印可复算对照，供答辩现场核对。"""
    import json as _json
    import sys

    from .web_demo import build_demo_payload, collab_review

    payload = build_demo_payload()
    result = compare(payload, collab=collab_review())
    text = _json.dumps(result, ensure_ascii=False, indent=2)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
