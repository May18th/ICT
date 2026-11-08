"""执行账本：动作卡状态的唯一事实源（append-only）。

为什么单独一层
--------------
沙盘上的四态原来是**按跳次派生**的推演状态，页面上明确写着"非现场回传"。
要做真正的执行闭环，就得有人真的把"这张卡开工了/办完了/等签字"记下来。
这份记录不能写进 `work_orders.csv`——那是冻结数据资产，改一个字节，
推演快照哈希与 108 项自检会整体漂。

所以写进独立的 append-only 账本：

- `events.jsonl` 是唯一事实源，只追加，不修改不删除；
- `state.json` 是投影缓存，删掉可由事件重放重建，不是真相。

四条硬约束（都会拒绝而不是纠正）
--------------------------------
1. **非法状态迁移直接拒**：`pending → done` 这种跳步不认，也不会被"自动补一条 running"；
2. **幂等**：`idempotency_key = sha256(card_id + to_state)`，重复提交返回 `duplicate`，不再追加；
3. **乐观锁**：请求必须带 `expected_version`，与服务端版本不一致就 `409`，由调用方决定重读；
4. **会签**：带 `co_sign_required` 的卡要转 `done`，必须先把这些部门全部签过。

身份边界
--------
PoC 没有登录系统，**不伪造令牌**。`actor_org / actor_role` 由服务端固定配置给出，
前端传了也一律忽略（`append_event()` 不接受这两个字段）。页面上标注"演示执行账本"。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

EXECUTION_DIR_ENV = "URBAN_TWIN_EXECUTION_DIR"

#: 卡片状态机。终止态没有出口，只能靠新事件重开（暂不支持）。
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("running", "cancelled"),
    "running": ("done", "needs_confirm", "cancelled"),
    "needs_confirm": ("done", "cancelled"),
    "done": (),
    "cancelled": (),
}

STATE_LABEL = {
    "pending": "待执行",
    "running": "执行中",
    "needs_confirm": "待人工确认",
    "done": "已完成",
    "cancelled": "已取消",
}

#: 服务端固定身份。演示页面不接真实登录，也不接受前端传入覆盖。
DEFAULT_ACTOR = {"actor_org": "ORG-EMERG", "actor_role": "值班调度（演示）"}

#: 事件动作：状态迁移、会签、工单关闭。
ACTION_TRANSITION = "transition"
ACTION_SIGN = "sign"
ACTION_CLOSE_ORDER = "close_order"
ACTIONS = (ACTION_TRANSITION, ACTION_SIGN, ACTION_CLOSE_ORDER)

#: 必填字段：少一个就拒。审计字段不是"能填就填"，是写完才有账。
REQUIRED_FIELDS = ("event_id", "card_id", "action", "expected_version")


def execution_dir() -> Path:
    override = os.environ.get(EXECUTION_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "artifacts" / "execution"


def events_path() -> Path:
    return execution_dir() / "events.jsonl"


def state_path() -> Path:
    return execution_dir() / "state.json"


# --- 读 -------------------------------------------------------------------


def read_events() -> list[dict[str, Any]]:
    path = events_path()
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # 坏行不静默跳过：账本里出现读不懂的行，说明有人在手改
            raise ValueError(f"执行账本存在无法解析的行：{line[:80]}")
    return events


def idempotency_key(card_id: str, to_state: str) -> str:
    """幂等键：同一张卡同一目标状态只算一次。

    粒度是契约定的 `(card_id, to_state)`，代价要说清：**同一张卡第二次提交同一个目标状态，
    无论中间状态怎么变，都会被当成重试吸收**。所以 `done` 之后的重开请求不会报
    `illegal_transition`，而是返回 `duplicate`——效果一样是拒绝，但错误码不同。
    要精确区分"重试"和"重开"，幂等键得带上 `from_state` 或客户端 `request_id`，
    那是契约变更，不在本次范围。
    """
    return hashlib.sha256(f"{card_id}|{to_state}".encode("utf-8")).hexdigest()[:32]


def _card_signatures(events: Sequence[Mapping[str, Any]], card_id: str) -> list[str]:
    """这张卡已经收到哪些部门的会签（去重保序）。"""
    signed: list[str] = []
    for event in events:
        if event.get("card_id") != card_id or event.get("action") != ACTION_SIGN:
            continue
        org = str(event.get("actor_org") or "")
        if org and org not in signed:
            signed.append(org)
    return signed


def project(events: Sequence[Mapping[str, Any]], cards: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """把事件流重放成当前状态。纯函数，同一份事件永远得到同一个结果。"""
    states: dict[str, dict[str, Any]] = {}
    for card_id, card in cards.items():
        order_ids = [str(item) for item in card.get("order_ids") or [] if item]
        states[card_id] = {
            "card_id": card_id,
            "state": "pending",
            "version": 0,
            "order_id": order_ids[0] if order_ids else "",
            # 一张卡可能同时指向多张工单（一个动作覆盖多处积水），关单校验要按全量看
            "order_ids": order_ids,
            "co_sign_required": [str(item) for item in card.get("co_sign_required") or []],
            "signatures": [],
            "last_event_id": "",
            "last_at": "",
        }

    closed_orders: dict[str, dict[str, Any]] = {}
    for event in events:
        action = str(event.get("action") or "")
        card_id = str(event.get("card_id") or "")
        if action == ACTION_SIGN and card_id in states:
            org = str(event.get("actor_org") or "")
            if org and org not in states[card_id]["signatures"]:
                states[card_id]["signatures"].append(org)
            states[card_id]["last_event_id"] = str(event.get("event_id") or "")
            states[card_id]["last_at"] = str(event.get("at") or "")
            continue
        if action == ACTION_CLOSE_ORDER:
            order_id = str(event.get("order_id") or "")
            if order_id:
                closed_orders[order_id] = {
                    "order_id": order_id,
                    "closed_at": str(event.get("at") or ""),
                    "closed_by": str(event.get("actor_org") or ""),
                    "event_id": str(event.get("event_id") or ""),
                    "note": str(event.get("note") or ""),
                }
            continue
        if card_id not in states:
            continue
        row = states[card_id]
        row["state"] = str(event.get("to_state") or row["state"])
        row["version"] = int(event.get("version") or row["version"])
        row["last_event_id"] = str(event.get("event_id") or "")
        row["last_at"] = str(event.get("at") or "")

    counts: dict[str, int] = {state: 0 for state in STATE_LABEL}
    for row in states.values():
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"cards": states, "closed_orders": closed_orders, "counts": counts, "events": len(events)}


def current_state(cards: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """读投影。`state.json` 只是缓存，与重放结果不一致时以事件为准并重写。"""
    events = read_events()
    rebuilt = project(events, cards)
    path = state_path()
    cached = None
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
    if cached != rebuilt:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rebuilt


# --- 写 -------------------------------------------------------------------


class LedgerError(Exception):
    """带 HTTP 语义的拒绝：status 给路由用，code 给前端判断。"""

    def __init__(self, message: str, *, status: int, code: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


def _next_event_id(events: Sequence[Mapping[str, Any]]) -> str:
    return f"EX-{len(events) + 1:04d}"


def append_event(
    payload: Mapping[str, Any],
    *,
    cards: Mapping[str, Mapping[str, Any]],
    actor: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """追加一条执行事件。

    `actor` 只能由服务端传入；payload 里的 `actor_org/actor_role` 一律忽略，
    避免前端伪造审计身份。
    """
    for field in REQUIRED_FIELDS:
        value = payload.get(field)
        # 注意 `expected_version` 合法值是 0，不能用 `value or ""` 判空
        if value is None or str(value).strip() == "":
            raise LedgerError(f"缺少必填字段：{field}", status=400, code="missing_field", detail=field)

    action = str(payload.get("action") or "").strip()
    if action not in ACTIONS:
        raise LedgerError(f"未知动作：{action}", status=400, code="unknown_action", detail=action)

    card_id = str(payload.get("card_id") or "").strip()
    if card_id not in cards:
        raise LedgerError(f"动作卡不存在：{card_id}", status=404, code="card_not_found", detail=card_id)

    events = read_events()
    card = cards[card_id]
    current = project(events, cards)["cards"][card_id]
    who = dict(DEFAULT_ACTOR)
    who.update({key: str(value) for key, value in (actor or {}).items() if value})

    at = str(payload.get("at") or "").strip() or datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        datetime.fromisoformat(at)
    except ValueError as exc:
        raise LedgerError(f"时间戳不合法：{at}", status=400, code="bad_timestamp") from exc

    base: dict[str, Any] = {
        "event_id": _next_event_id(events),
        "at": at,
        "card_id": card_id,
        # 工单与依据由服务端从卡片补齐：前端可以不传，但账本里必须留全，
        # 否则事后问"这条状态是谁依据什么改的"会答不上来
        "order_id": str(payload.get("order_id") or (card.get("order_ids") or [""])[0] or ""),
        "action": action,
        "actor_org": who["actor_org"],
        "actor_role": who["actor_role"],
        "claim_ids": [str(item) for item in (payload.get("claim_ids") or card.get("claim_ids") or [])],
        "evidence_refs": [str(item) for item in (payload.get("evidence_refs") or card.get("evidence_refs") or [])],
        "expected_version": int(payload.get("expected_version") or 0),
        "note": str(payload.get("note") or ""),
    }

    if action == ACTION_CLOSE_ORDER:
        return _append_close(payload, base, events, cards, current)

    if action == ACTION_SIGN:
        if current["state"] != "needs_confirm":
            raise LedgerError(
                f"该卡当前状态为「{STATE_LABEL.get(current['state'], current['state'])}」，不需要会签",
                status=409,
                code="sign_not_required",
                detail=current["state"],
            )
        org = who["actor_org"]
        if org in current["signatures"]:
            return {"status": "duplicate", "code": "already_signed", "event": None, "card": current}
        base.update({"to_state": "", "version": current["version"]})
        return _write(base, events, cards, result="signed")

    # transition
    to_state = str(payload.get("to_state") or "").strip()
    if to_state not in STATE_LABEL:
        raise LedgerError(f"未知目标状态：{to_state}", status=400, code="unknown_state", detail=to_state)
    from_state = current["state"]

    key = idempotency_key(card_id, to_state)
    if any(event.get("idempotency_key") == key for event in events):
        return {"status": "duplicate", "code": "duplicate", "event": None, "card": current, "idempotency_key": key}

    if to_state not in TRANSITIONS.get(from_state, ()):
        raise LedgerError(
            f"非法状态迁移：{STATE_LABEL.get(from_state, from_state)} → {STATE_LABEL.get(to_state, to_state)}",
            status=409,
            code="illegal_transition",
            detail={"from_state": from_state, "to_state": to_state, "allowed": list(TRANSITIONS.get(from_state, ()))},
        )

    expected = int(payload.get("expected_version") or 0)
    if expected != current["version"]:
        raise LedgerError(
            f"版本冲突：请求 {expected}，当前 {current['version']}",
            status=409,
            code="version_conflict",
            detail={"expected_version": expected, "current_version": current["version"]},
        )

    if to_state == "done":
        missing = [org for org in current["co_sign_required"] if org not in current["signatures"]]
        if missing:
            raise LedgerError(
                "会签未齐，不能转为已完成：" + "、".join(missing),
                status=409,
                code="co_sign_incomplete",
                detail={"missing": missing, "signed": current["signatures"]},
            )

    base.update({"from_state": from_state, "to_state": to_state, "version": current["version"] + 1, "idempotency_key": key})
    return _write(base, events, cards, result="ok")


def _append_close(
    payload: Mapping[str, Any],
    base: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    cards: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """工单关闭：必须人工确认（这里就是那次人工动作），且所有关联卡已完成。

    不写 `work_orders.csv`，只在账本里记一条关闭记录——冻结资产不动。
    """
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise LedgerError("关闭工单必须给 order_id", status=400, code="missing_order_id")

    projection = project(events, cards)
    if order_id in projection["closed_orders"]:
        return {"status": "duplicate", "code": "already_closed", "event": None, "card": current}

    siblings = [row for row in projection["cards"].values() if order_id in (row.get("order_ids") or [])]
    if not siblings:
        raise LedgerError(f"账本里没有与该工单关联的动作卡：{order_id}", status=404, code="order_not_found", detail=order_id)
    unfinished = [row["card_id"] for row in siblings if row["state"] != "done"]
    if unfinished:
        raise LedgerError(
            "工单还有未完成的动作卡，不能关闭：" + "、".join(unfinished),
            status=409,
            code="card_not_done",
            detail={"unfinished": unfinished},
        )

    expected = int(payload.get("expected_version") or 0)
    if expected not in (0, int(current["version"])):
        raise LedgerError(
            f"版本冲突：请求 {expected}，当前 {current['version']}",
            status=409,
            code="version_conflict",
            detail={"expected_version": expected, "current_version": current["version"]},
        )

    event = dict(base)
    event.update(
        {
            "order_id": order_id,
            "to_state": "order_closed",
            "version": int(current["version"]),
            "idempotency_key": hashlib.sha256(f"order|{order_id}".encode("utf-8")).hexdigest()[:32],
        }
    )
    return _write(event, events, cards, result="order_closed")


def _write(
    event: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    cards: Mapping[str, Mapping[str, Any]],
    *,
    result: str,
) -> dict[str, Any]:
    directory = execution_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event), ensure_ascii=False) + "\n")
    updated = list(events) + [dict(event)]
    projection = project(updated, cards)
    state_path().write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": result,
        "event": dict(event),
        "card": projection["cards"].get(str(event.get("card_id") or ""), {}),
        "counts": projection["counts"],
    }


# --- 计划 vs 实际 ---------------------------------------------------------


def compare_plan(
    cards: Sequence[Mapping[str, Any]],
    projected: Mapping[str, Any],
) -> dict[str, Any]:
    """计划完成数 vs 账本实际完成数。

    "计划"是推演按跳次算出来的应当完成数；"实际"只在账本里。两者不一致就是偏差，
    页面上要并排显示——只报实际会被读成"进度落后"，只报计划又回到推演状态充现场。
    """
    planned_done = [
        card["card_id"]
        for card in cards
        if (card.get("planned_state") or card.get("state")) == "done"
    ]
    rows = projected.get("cards") or {}
    actual_done = [card_id for card_id, row in rows.items() if row.get("state") == "done"]
    missing = [card_id for card_id in planned_done if card_id not in actual_done]
    extra = [card_id for card_id in actual_done if card_id not in planned_done]
    return {
        "planned_done": len(planned_done),
        "actual_done": len(actual_done),
        "delta": len(actual_done) - len(planned_done),
        "behind": missing,
        "ahead": extra,
        "labels": {"planned": "计划完成", "actual": "账本实际完成"},
    }


# --- 导出 -----------------------------------------------------------------


def export_snapshot(*, cards: Mapping[str, Mapping[str, Any]], target: Path | None = None) -> Path:
    """导出脱敏演示快照：给评委复现用，不含 actor 姓名/联系方式。

    `actor_org` 是部门代码（ORG-DRAIN 之类），本身不含个人信息，保留；
    `actor_role` 与 `note` 可能带人名，导出时抹掉。
    """
    events = read_events()
    projection = project(events, cards)
    safe_events = []
    for event in events:
        item = dict(event)
        item["actor_role"] = "（已脱敏）"
        item["note"] = ""
        safe_events.append(item)
    payload = {
        "schema_version": "execution-snapshot-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "脱敏演示快照：actor_role 与 note 已抹除；部门代码保留，用于复现计划/实际对照",
        "events": safe_events,
        "projection": projection,
    }
    out = target or (execution_dir() / "snapshot.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out
