"""决策事件流：把一次推演拍平成评委能逐条跟着看的时间线。

为什么单独一层
--------------
`/api/demo` 返回的是**一次快照**：六跳、冲突、缺口、动作都在，但页面只能一次性铺开，
评委看不出"哪一步先发生、哪一步依赖上一步"。这里把它拍成有序事件。

三条纪律（演示脚本里"不能只做固定台词和假进度条"的技术保证）：

1. **只派生，不重算**——每个事件带 `source`，写明它读的是 payload 的哪个字段
   （如 `deduction.resource_gaps[0].gap`）。页面上任何一个数字都能回到 `/api/demo` 对上。
2. **不写算不出来的因果**——事件的 detail 只拼装取到的数值，不出现"会更严重""影响很大"
   这类没有字段支撑的判断。缺口怎么算出来的，原样透出 `basis` 测算链。
3. **条数与顺序固定**——六跳各一条 + 冲突逐条 + 缺口逐条 + 动作一条 + 审计一条。
   换事件只会改内容，不会把时间线改成另一种形状。

`compare_branches()` 用于"人工改一个条件 → 重推演"后的前后对照，说明句同样只由数值拼装。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"

# 六跳的固定顺序。跳 6（action）在 `stages` 里会出现 10 次，取第一条作阶段事件，
# 其余展开成 `actions` 列表——顺序本身是推演结论的一部分，不能打乱。
STAGE_ORDER = ("observed", "affected", "owner", "conflict", "resource", "action")

STAGE_LABEL = {
    "observed": "观测：事件识别",
    "affected": "影响：扩散范围",
    "owner": "责任：部门落位",
    "conflict": "冲突：三类暴露",
    "resource": "资源：缺口核验",
    "action": "动作：处置顺序",
}

CONFLICT_KIND_LABEL = {
    "overlap": "职责重叠",
    "gap": "职责空白",
    "contention": "资源争用",
}

# 事件里最多带几条证据引用（全量在 payload 的 evidence_refs 里，页面按需展开）。
REF_HEAD = 3


def _first_stage(stages: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    """取某个阶段的第一条记录；缺失返回 None（不编一条空事件顶上）。"""
    for step in stages:
        if step.get("stage") == name:
            return step
    return None


def _refs(step: Mapping[str, Any]) -> tuple[list[str], int]:
    refs = [str(item) for item in step.get("evidence_refs") or []]
    return refs[:REF_HEAD], len(refs)


def _split_count(value: Any) -> int:
    """`affected`（`a,b,c`）/ `owners`（`a;b`）/ `demand_by_district`（`x:1|y:2`）的拼串计数。

    三种分隔符都要认：早先只认 `;` 与 `|`，把 14 个受影响对象算成了 1 个。
    """
    text = str(value or "").strip()
    if not text:
        return 0
    return len([item for item in re.split(r"[;,|]", text) if item.strip()])


def _stage_detail(
    name: str,
    *,
    step: Mapping[str, Any],
    deduction: Mapping[str, Any],
) -> str:
    conclusion = step.get("conclusion") or {}
    if name == "observed":
        return " · ".join(
            part
            for part in (
                f"响应等级 {conclusion.get('response_level', '-')}",
                f"积水 {conclusion.get('depth_cm', '-')} cm",
                f"片区 {conclusion.get('district', '-')}",
                f"触发：{conclusion.get('trigger', '-')}",
            )
        )
    if name == "affected":
        affected = _split_count(conclusion.get("affected"))
        return f"{affected} 个对象受影响 · 命中 {len(step.get('rule_ids') or [])} 条规则"
    if name == "owner":
        coordinators = str(conclusion.get("coordinator") or "").split(";")
        owners = _split_count(conclusion.get("owners"))
        unassigned = str(conclusion.get("unassigned") or "").strip()
        return (
            f"协调方 {'、'.join(item for item in coordinators if item) or '-'}"
            f" · {owners} 个对象已落位"
            f" · 未落位 {unassigned or '无'}"
        )
    if name == "conflict":
        counts = {key: 0 for key in CONFLICT_KIND_LABEL}
        for item in deduction.get("conflicts") or []:
            kind = str(item.get("kind") or "")
            if kind in counts:
                counts[kind] += 1
        return (
            f"职责重叠 {counts['overlap']}"
            f" · 职责空白 {counts['gap']}"
            f" · 资源争用 {counts['contention']}"
        )
    if name == "resource":
        gaps = deduction.get("resource_gaps") or []
        load = str(conclusion.get("pump_load") or "").strip()
        # 缺口三元组（需 8 / 可用 6 / 缺口 2）只在后面那条「测算依据」里说算式，
        # 这里讲泵站还能撑多久——负荷是风险指标，缺口是资源指标，两个都要出现且不互相替代。
        parts = [f"核验 {len(gaps)} 项资源" if gaps else "本次推演未产出资源缺口"]
        if load:
            parts.append(load)
        return " · ".join(parts)
    if name == "action":
        total = len(deduction.get("action_order") or [])
        return f"{total} 条处置动作，顺序固定（不可按部门偏好重排）"
    return str(step.get("detail") or "")


def _stage_event(step: Mapping[str, Any], name: str, deduction: Mapping[str, Any]) -> dict[str, Any]:
    refs, ref_count = _refs(step)
    return {
        "kind": "stage",
        "phase": name,
        "title": STAGE_LABEL[name],
        "detail": _stage_detail(name, step=step, deduction=deduction),
        "status": str(step.get("status") or ""),
        "rules": list(step.get("rule_ids") or []),
        "refs": refs,
        "ref_count": ref_count,
        "source": f"deduction.stages[{name}]",
    }


def _conflict_events(
    deduction: Mapping[str, Any], *, kinds: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """展开冲突条目。`kinds` 用来把资源争用挪到资源跳后面——它在冲突跳里排着，
    但讲的是库存，紧挨着冲突跳念会让缺口数字连说三遍。"""
    events: list[dict[str, Any]] = []
    for index, item in enumerate(deduction.get("conflicts") or []):
        kind = str(item.get("kind") or "")
        if kinds is not None and kind not in kinds:
            continue
        refs = [str(ref) for ref in item.get("evidence_refs") or []]
        events.append(
            {
                "kind": "conflict",
                "phase": "conflict",
                "title": f"{item.get('conflict_id', '-')} {CONFLICT_KIND_LABEL.get(kind, kind)}：{item.get('subject', '')}",
                "detail": str(item.get("detail") or ""),
                "status": "detected",
                "orgs": list(item.get("orgs") or []),
                "rules": list(item.get("rule_ids") or []),
                "refs": refs[:REF_HEAD],
                "ref_count": len(refs),
                "source": f"deduction.conflicts[{index}]",
            }
        )
    return events


def _gap_events(deduction: Mapping[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, item in enumerate(deduction.get("resource_gaps") or []):
        refs = [str(ref) for ref in item.get("evidence_refs") or []]
        events.append(
            {
                "kind": "gap",
                "phase": "resource",
                "title": f"{item.get('resource_id', '-')} 缺口测算依据（{len(item.get('basis') or [])} 条）",
                "detail": (
                    f"需 {item.get('required')} / 可用 {item.get('available')}"
                    f" = 缺口 {item.get('gap')} {item.get('unit', '')}"
                ),
                # 测算链原样透出：答辩被问"2 台怎么来的"时，答案就在这里
                "basis": list(item.get("basis") or []),
                "status": "shortfall" if (item.get("gap") or 0) > 0 else "covered",
                "refs": refs[:REF_HEAD],
                "ref_count": len(refs),
                "source": f"deduction.resource_gaps[{index}]",
            }
        )
    return events


def _audit_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    audit = payload.get("audit") or {}
    publishable = bool(payload.get("publishable"))
    return {
        "kind": "audit",
        "phase": "audit",
        "title": "证据审计：可发布性判定",
        "detail": (
            f"引用完整率 {audit.get('citation_completeness', '-')}"
            f" · 账本链 {'完整' if audit.get('chain_ok') else '不可信'}"
            f" · {'可发布结论' if publishable else '需人工复核'}"
        ),
        "status": "publishable" if publishable else "needs_review",
        "refs": [],
        "ref_count": len(audit.get("claims") or []),
        "source": "audit / publishable",
    }


def _trace(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """智能体的执行轨迹（哪个技能、跑了多久、有没有降级）。与决策链分开呈现。"""
    return [
        {
            "seq": index + 1,
            "skill": step.get("skill"),
            "stage": step.get("stage"),
            "status": step.get("status"),
            "elapsed_ms": step.get("elapsed_ms"),
            "degraded": list(step.get("degraded") or []),
        }
        for index, step in enumerate(payload.get("steps") or [])
    ]


def build_timeline(payload: Mapping[str, Any] | None = None, *, event_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    """把一次推演的 payload 派生为有序事件流。不传 payload 时现场跑一次。"""
    if payload is None:
        from .web_demo import build_demo_payload  # 函数内导入：避免模块级循环依赖

        payload = build_demo_payload()

    deduction = payload.get("deduction") or {}
    stages = deduction.get("stages") or []

    events: list[dict[str, Any]] = []
    for name in STAGE_ORDER:
        step = _first_stage(stages, name)
        if step is None:
            continue
        events.append(_stage_event(step, name, deduction))
        # 冲突与缺口紧跟在它所属的那一跳后面，读者不会来回跳着看；
        # 资源争用（C3）属库存问题，挂到资源跳之后。
        if name == "conflict":
            events.extend(_conflict_events(deduction, kinds=("overlap", "gap")))
        elif name == "resource":
            events.extend(_conflict_events(deduction, kinds=("contention",)))
            events.extend(_gap_events(deduction))
    events.append(_audit_event(payload))

    for index, event in enumerate(events):
        event["seq"] = index + 1

    all_refs: set[str] = set()
    for step in stages:
        all_refs.update(str(ref) for ref in step.get("evidence_refs") or [])

    return {
        "event_id": payload.get("event_id") or event_id or EVENT_ID,
        "as_of": payload.get("as_of") or as_of or AS_OF,
        "source": "decision_poc.deduction 真实输出派生（与 /api/demo 同源，展示层不重算）",
        "counts": {
            "events": len(events),
            "stages": len([item for item in events if item["kind"] == "stage"]),
            "conflicts": len(deduction.get("conflicts") or []),
            "gaps": len(deduction.get("resource_gaps") or []),
            "actions": len(deduction.get("action_order") or []),
            "evidence_refs": len(all_refs),
        },
        "events": events,
        "actions": list(deduction.get("action_order") or []),
        "residual_risks": list(deduction.get("residual_risks") or []),
        "trace": _trace(payload),
        "audit": {
            "publishable": bool(payload.get("publishable")),
            "citation_completeness": (payload.get("audit") or {}).get("citation_completeness"),
            "chain_ok": (payload.get("audit") or {}).get("chain_ok"),
            "knowledge_version": payload.get("knowledge_version"),
        },
    }


# --- 干预前后对照 ---------------------------------------------------------


def _branch_brief(payload: Mapping[str, Any]) -> dict[str, Any]:
    """一次推演里"会变的那几个数"。对照只比这些，避免整包 diff 看不出重点。"""
    deduction = payload.get("deduction") or {}
    gaps = deduction.get("resource_gaps") or []
    gap = sum(int(item.get("gap") or 0) for item in gaps)
    return {
        "required": sum(int(item.get("required") or 0) for item in gaps),
        "available": sum(int(item.get("available") or 0) for item in gaps),
        "gap": gap,
        "conflicts": [str(item.get("conflict_id")) for item in deduction.get("conflicts") or []],
        "conflict_count": len(deduction.get("conflicts") or []),
        "actions": len(deduction.get("action_order") or []),
        "action_order": list(deduction.get("action_order") or []),
        "residual_risks": len(deduction.get("residual_risks") or []),
        "publishable": bool(payload.get("publishable")),
        "action_count": len(deduction.get("action_order") or []),
    }


def compare_branches(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    interventions: Sequence[str] = (),
) -> dict[str, Any]:
    """人工干预后的前后对照。

    说明句只由数值拼装：缺口变大就说变大几台，处置顺序挪位就说从第几条到第几条。
    不写"某片区会更危险"这类没有字段支撑的因果判断。
    """
    left = _branch_brief(before)
    right = _branch_brief(after)

    delta = {
        "required": right["required"] - left["required"],
        "available": right["available"] - left["available"],
        "gap": right["gap"] - left["gap"],
        "conflict_count": right["conflict_count"] - left["conflict_count"],
        "residual_risks": right["residual_risks"] - left["residual_risks"],
    }

    statements: list[str] = []
    if delta["available"]:
        statements.append(f"可用移动泵 {left['available']} → {right['available']} 台（{delta['available']:+d}）")
    if delta["gap"]:
        statements.append(f"缺口 {left['gap']} → {right['gap']} 台（{delta['gap']:+d}）")
    elif not delta["required"] and not delta["available"]:
        statements.append(f"缺口不变，仍为 {right['gap']} 台")
    if delta["conflict_count"]:
        statements.append(f"冲突 {left['conflict_count']} → {right['conflict_count']} 类")
    else:
        statements.append(f"冲突集合不变：{'、'.join(right['conflicts']) or '无'}")

    moved = [
        (index + 1, old, new)
        for index, (old, new) in enumerate(zip(left["action_order"], right["action_order"]))
        if old != new
    ]
    if moved:
        first_index, _, _ = moved[0]
        statements.append(f"处置顺序有 {len(moved)} 条挪位，最早从第 {first_index} 条开始")
    else:
        statements.append("处置顺序未变")

    return {
        "interventions": list(interventions),
        "before": left,
        "after": right,
        "delta": delta,
        "statements": statements,
        "moved_actions": [
            {"order": index, "before": old, "after": new} for index, old, new in moved
        ],
        "source": "两棵分支都由 decision_poc 现跑，不是对基线结果做加减",
    }


__all__ = [
    "AS_OF",
    "EVENT_ID",
    "STAGE_LABEL",
    "STAGE_ORDER",
    "build_timeline",
    "compare_branches",
]
