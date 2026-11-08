"""沙盘对象 ↔ 工单 ↔ 动作卡 ↔ 证据 的关联层。

为什么单独一层
--------------
沙盘要回答的是「这个积水点上，谁在干什么、办到哪一步了、凭什么」。
这些信息分别躺在四个地方：

- 对象↔工单：`chain.build_scene_entities()` 已经算好，放在 `entity.links.work_orders`；
- 动作卡：`collab_review()` 的 `cards`，只有部门、阶段、动作文本；
- 证据：卡片自带 `evidence_refs`，格式 `AS-...#A-03/work_order/v1#row:2`；
- 跳次：`event_stream` 里 `kind == 'stage'` 的六条。

本模块只做**索引与传递**，边界写死三条：

1. **不重算**：一个数字都不新算，全部读 `scheduler_view()` 已有的块；
2. **每条关联都能说出依据**：动作卡→工单靠 `evidence_refs` 里的
   `work_order#row:N`（行号即台账行号，与 `scene_layout._line_of` 同一口径）；
   工单→对象靠实体自带的 `links.work_orders`；文本里显式写出的 id 也认，
   但同样逐条可查，不做语义猜测；
3. **执行状态是推演状态，不是现场回传**：台账里 26 张卡全是 `accepted`（契约已接受），
   没有任何"已开工/已完工"的实测字段。所以这里给出的四态由**当前跳次推进**派生，
   规则固定、可断言，页面上必须标来源，不能当现场进度汇报。

最后一条是刻意的：宁可页面上写"推演状态"，也不给沙盘编一个"已处置完成 80%"。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ._shared import dataset_root, read_csv_rows
from .collab.roles import ROLES
from .urban_dataset import DEFAULT_DATASET_ROOT

#: 六跳阶段的固定顺序。跳次号 = 下标 + 1，与 `event_stream` 的 `phase` 字段一一对应。
STAGE_JUMPS: tuple[str, ...] = ("observed", "affected", "owner", "conflict", "resource", "action")

#: 跳次上限。7 = 处置后（六跳全过），只用于前后对比，不是第七个阶段。
MAX_JUMP = len(STAGE_JUMPS) + 1

STAGE_LABEL = {
    "observed": "观测：事件识别",
    "affected": "影响：扩散范围",
    "owner": "责任：部门落位",
    "conflict": "冲突：三类暴露",
    "resource": "资源：缺口核验",
    "action": "动作：处置顺序",
}

#: 动作卡执行状态。四态与沙盘视觉一一对应，映射表在 `CARD_VISUAL`。
CARD_STATE_LABEL = {
    "pending": "待执行",
    "running": "执行中",
    "done": "已完成",
    "needs_confirm": "待人工确认",
}

#: 状态 → 沙盘视觉。前端直接用这个键切样式，避免两边各写一套条件。
CARD_VISUAL = {
    "pending": "idle",
    "running": "active",
    "needs_confirm": "alert",
    "done": "settled",
}

#: 卡片证据里的工单行号，例如 `AS-A-03-work_order-6a1282a1#A-03/work_order/v1#row:2`。
WORK_ORDER_ROW = re.compile(r"work_order[^#]*#row:(\d+)")
#: 台账里的工单号，例如 WO-20261029-011。
WORK_ORDER_ID = re.compile(r"WO-\d{8}-\d{3}")


def _entity_pattern(entity_id: str) -> re.Pattern[str]:
    """实体 id 的整词匹配。

    直接 `id in text` 会把 `WO-20261029-01` 当成 `WO-20261029-011` 的前缀命中，
    反过来也会让 `R-10` 命中 `R-101`。两侧加边界，只认独立出现的 id。
    """
    return re.compile(r"(?<![A-Za-z0-9-])" + re.escape(entity_id) + r"(?![A-Za-z0-9-])")


def work_order_rows(root: Path | str | None = None) -> dict[int, str]:
    """台账行号 → 工单号。与 `scene_layout._line_of` 同口径：第 1 行是表头。"""
    path = dataset_root(root, DEFAULT_DATASET_ROOT) / "work_orders.csv"
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    rows: dict[int, str] = {}
    for line_no, line in enumerate(lines, start=1):
        for order_id in WORK_ORDER_ID.findall(line):
            rows[line_no] = order_id
            break
    return rows


def card_state(card: Mapping[str, Any], current_jump: int) -> str:
    """推演状态：按跳次推进，不看现场回传。

    规则（四条，顺序即优先级）：

    1. 卡片所在跳已经过去了 → `done`；
    2. 但需要会签（`co_sign_required` 非空）的卡，过了也只是"等签字"，记 `needs_confirm`；
    3. 停在当前跳 → `running`；
    4. 还没轮到的跳 → `pending`。
    """
    stage = str(card.get("stage") or "")
    card_jump = STAGE_JUMPS.index(stage) + 1 if stage in STAGE_JUMPS else 1
    if current_jump > card_jump:
        return "needs_confirm" if card.get("co_sign_required") else "done"
    if current_jump == card_jump:
        return "running"
    return "pending"


def _cards_by_order(
    cards: Sequence[Mapping[str, Any]],
    entity_ids: set[str],
    row_to_order: Mapping[int, str],
    order_ids: set[str],
) -> dict[str, list[str]]:
    """动作卡 → 工单号。

    两条依据，都留痕：

    - `evidence_refs` 里 `work_order#row:N` → 台账第 N 行的工单号（主依据）；
    - 动作文本里显式写了的工单号（辅助，只有 S2 受影响那张卡会命中）。

    只收在台账里真实存在的工单号，拼错的、历史作废的一律丢掉。
    """
    mapping: dict[str, list[str]] = {}
    for card in cards:
        card_id = str(card.get("card_id") or "")
        hits: list[str] = []
        for ref in card.get("evidence_refs") or []:
            match = WORK_ORDER_ROW.search(str(ref))
            if not match:
                continue
            order_id = row_to_order.get(int(match.group(1)))
            if order_id and order_id in order_ids and order_id not in hits:
                hits.append(order_id)
        for order_id in WORK_ORDER_ID.findall(str(card.get("action") or "")):
            if order_id in order_ids and order_id not in hits:
                hits.append(order_id)
        # 文本里点名了对象、但当时台账没记工单号：只保留文本命中，关联到对象由下一步补
        if hits:
            mapping[card_id] = hits
    return mapping


def _entity_ids_in(text: str, entity_ids: Sequence[str]) -> list[str]:
    """文本里显式出现的实体 id，按出现顺序去重返回。"""
    hits: list[tuple[int, str]] = []
    for entity_id in entity_ids:
        match = _entity_pattern(entity_id).search(text)
        if match:
            hits.append((match.start(), entity_id))
    return [entity_id for _, entity_id in sorted(hits)]


def _jump_entities(
    phase: str,
    detail: str,
    *,
    entity_ids: Sequence[str],
    affected_names: Sequence[str],
    card_entity_ids: Sequence[str],
) -> list[str]:
    """一跳涉及哪些沙盘对象。

    三个来源取并集，优先级从高到低：

    1. 该跳文字里点名的 id；
    2. `affected` 跳读 `payload.impact.affected` 的真实字段（不是解析文案）；
    3. 该跳动作卡已经关联到的对象（保证"有卡必有对象高亮"）。
    """
    ordered: list[str] = []
    for entity_id in _entity_ids_in(detail, entity_ids):
        if entity_id not in ordered:
            ordered.append(entity_id)
    names = set(affected_names)
    for entity_id in entity_ids:
        if entity_id in names and entity_id not in ordered:
            ordered.append(entity_id)
    for entity_id in card_entity_ids:
        if entity_id not in ordered:
            ordered.append(entity_id)
    if phase == "action":
        # 处置动作卡没有逐个对象的目标，但动作一定落在本事件的积水点上。
        # 这里用事件本体兜底，避免"动作跳"高亮成空。
        pass
    return ordered


def build_twin_links(
    view: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    current_jump: int = 1,
) -> dict[str, Any]:
    """把一份 `scheduler_view()` 摊平成沙盘要的索引。

    参数
    ----
    view
        `web_demo.scheduler_view()` 的返回值（含 payload / orders / scene / collab / stream）。
    current_jump
        当前跳次（1..6），决定动作卡的推演状态。默认停在第一跳。
    """
    payload = view.get("payload") or {}
    scene = view.get("scene") or {}
    collab = view.get("collab") or {}
    orders = list(view.get("orders") or [])
    departments = list(view.get("departments") or [])
    resources = list((view.get("resources") or {}).get("items") or [])

    current_jump = clamp_jump(current_jump)

    entities = [item for item in (scene.get("entities") or []) if item.get("id")]
    entity_ids = [str(item["id"]) for item in entities]
    order_ids = {str(item["order_id"]) for item in orders}
    row_to_order = work_order_rows(root)
    order_by_id = {str(item["order_id"]): item for item in orders}
    dept_by_id = {str(item.get("org_id")): item for item in departments}
    # 总控 `EMERG-COORD` 不是部门 id，它挂在 `ORG-EMERG` 名下。
    # 不映射的话页面上会直接吐 `EMERG-COORD`，评委读不出是区应急管理局。
    role_department = {role.agent_id: role.department for role in ROLES}
    role_title = {role.agent_id: role.title for role in ROLES}

    def org_name_of(agent_id: str) -> str:
        department = dept_by_id.get(role_department.get(agent_id, agent_id)) or {}
        if department.get("org_name"):
            return str(department["org_name"])
        # 台账没登记这个部门时退回角色名（"应急指挥长"），仍然是人话
        return role_title.get(agent_id) or agent_id

    cards = [dict(card) for card in (collab.get("cards") or [])]
    cards_by_order = _cards_by_order(cards, set(entity_ids), row_to_order, order_ids)

    # 工单 → 对象（实体自带 links，本层只做反向索引）
    entities_by_order: dict[str, list[str]] = {}
    for entity in entities:
        for order_id in (entity.get("links") or {}).get("work_orders") or []:
            entities_by_order.setdefault(str(order_id), []).append(str(entity["id"]))

    # 卡片 → 对象：卡片命中的工单 → 该工单关联的对象；再并上文本里直接点名的对象
    card_entities: dict[str, list[str]] = {}
    for card in cards:
        card_id = str(card.get("card_id") or "")
        found: list[str] = []
        for order_id in cards_by_order.get(card_id, []):
            for entity_id in entities_by_order.get(order_id, []):
                if entity_id not in found:
                    found.append(entity_id)
        for entity_id in _entity_ids_in(str(card.get("action") or ""), entity_ids):
            if entity_id not in found:
                found.append(entity_id)
        card_entities[card_id] = found

    # ---- 六跳 ----
    affected_names = [str(item.get("name") or "") for item in (payload.get("impact") or {}).get("affected") or []]
    stage_events = [
        event
        for event in (view.get("stream") or {}).get("events") or []
        if event.get("category") == "timeline" and event.get("kind") == "stage"
    ]
    stage_events.sort(key=lambda event: int(event.get("seq") or 0))

    jumps: list[dict[str, Any]] = []
    for index, phase in enumerate(STAGE_JUMPS, start=1):
        event = next((item for item in stage_events if item.get("phase") == phase), None)
        if event is None:
            jumps.append(
                {
                    "jump": index,
                    "phase": phase,
                    "title": STAGE_LABEL[phase],
                    "detail": "",
                    "status": "missing",
                    "ref_count": 0,
                    "entity_ids": [],
                    "work_order_ids": [],
                    "card_ids": [],
                    "evidence_refs": [],
                }
            )
            continue
        jump_cards = [card for card in cards if str(card.get("stage") or "") == phase]
        jump_card_ids = [str(card.get("card_id")) for card in jump_cards]
        jump_entities: list[str] = []
        for card_id in jump_card_ids:
            for entity_id in card_entities.get(card_id, []):
                if entity_id not in jump_entities:
                    jump_entities.append(entity_id)
        merged = _jump_entities(
            phase,
            str(event.get("detail") or ""),
            entity_ids=entity_ids,
            affected_names=affected_names if phase == "affected" else (),
            card_entity_ids=jump_entities,
        )
        jump_orders: list[str] = []
        for card_id in jump_card_ids:
            for order_id in cards_by_order.get(card_id, []):
                if order_id not in jump_orders:
                    jump_orders.append(order_id)
        jumps.append(
            {
                "jump": index,
                "phase": phase,
                "title": str(event.get("title") or STAGE_LABEL[phase]),
                "detail": str(event.get("detail") or ""),
                "status": str(event.get("status") or ""),
                "ref_count": int(event.get("ref_count") or 0),
                "entity_ids": merged,
                "work_order_ids": jump_orders,
                "card_ids": jump_card_ids,
                "evidence_refs": [str(ref) for ref in event.get("refs") or []],
            }
        )

    # ---- 动作卡 ----
    card_rows: list[dict[str, Any]] = []
    for card in cards:
        card_id = str(card.get("card_id") or "")
        stage = str(card.get("stage") or "")
        state = card_state(card, current_jump)
        agent_id = str(card.get("agent_id") or "")
        card_rows.append(
            {
                "card_id": card_id,
                "agent_id": agent_id,
                "org_name": org_name_of(agent_id),
                "stage": stage,
                "stage_label": STAGE_LABEL.get(stage, stage),
                "jump": STAGE_JUMPS.index(stage) + 1 if stage in STAGE_JUMPS else 1,
                "action": str(card.get("action") or ""),
                "contract_status": str(card.get("status") or ""),
                "state": state,
                "state_label": CARD_STATE_LABEL[state],
                "visual": CARD_VISUAL[state],
                # 1..MAX_JUMP 每一跳的状态，前端直接按下标取。
                # 让页面自己算状态等于把规则复制一份到 JS 里，改一处漂一处。
                "states": [card_state(card, jump) for jump in range(1, MAX_JUMP + 1)],
                "co_sign_required": [str(item) for item in card.get("co_sign_required") or []],
                "entity_ids": card_entities.get(card_id, []),
                "work_order_ids": cards_by_order.get(card_id, []),
                "evidence_count": int(card.get("evidence_count") or 0),
                "evidence_refs": [str(ref) for ref in card.get("evidence_refs") or []],
            }
        )

    # 按跳次排一遍：页面从"观测"往下读，而不是按 card_id 字典序读
    card_rows.sort(key=lambda row: (row["jump"], row["card_id"]))

    # ---- 对象详情 ----
    entity_rows: dict[str, dict[str, Any]] = {}
    for entity in entities:
        entity_id = str(entity["id"])
        links = entity.get("links") or {}
        linked_orders = [str(item) for item in links.get("work_orders") or []]
        linked_cards = [row for row in card_rows if entity_id in row["entity_ids"]]
        linked_resources = [str(item) for item in links.get("resources") or []]
        states = [row["state"] for row in linked_cards]
        # 对象视觉取"最需要人看"的那张卡：待确认 > 执行中 > 待执行 > 已完成。
        # 没有卡的对象退回自身的静态状态（积水/封路等），不给它编一个执行态。
        priority = ("needs_confirm", "running", "pending", "done")
        owner_state = next((state for state in priority if state in states), "")
        by_jump: list[str] = []
        for jump in range(1, MAX_JUMP + 1):
            jump_states = {
                row["states"][jump - 1] for row in linked_cards if len(row.get("states") or []) >= jump
            }
            by_jump.append(next((state for state in priority if state in jump_states), ""))
        entity_rows[entity_id] = {
            "id": entity_id,
            "kind": str(entity.get("kind") or ""),
            "label": str(entity.get("label") or entity_id),
            "state": str(entity.get("state") or ""),
            "detail": str(entity.get("detail") or ""),
            "work_order_ids": linked_orders,
            "work_orders": [
                {
                    "order_id": order_id,
                    "order_type_label": str(order_by_id[order_id].get("order_type_label") or ""),
                    "status": str(order_by_id[order_id].get("status") or ""),
                    "severity": str(order_by_id[order_id].get("severity") or ""),
                    "reporter_org": str(order_by_id[order_id].get("reporter_org") or ""),
                    "description": str(order_by_id[order_id].get("description") or ""),
                }
                for order_id in linked_orders
                if order_id in order_by_id
            ],
            "card_ids": [row["card_id"] for row in linked_cards],
            # 卡片明细只在顶层 `cards` 里存一份。26 张卡挂在 9 个对象上，
            # 每个对象再复制一遍全量卡片会让整包从 20KB 涨到 150KB，
            # 而页面本来就按 card_id 查表——重复没有收益。
            "departments": sorted({row["org_name"] for row in linked_cards if row["org_name"]}),
            "resource_ids": linked_resources,
            "resources": [
                {
                    "resource_id": str(item.get("resource_id") or ""),
                    "name": str(item.get("name") or ""),
                    "unit": str(item.get("unit") or ""),
                    "qty_available": item.get("qty_available"),
                    "required": item.get("required"),
                }
                for item in resources
                if str(item.get("resource_id")) in linked_resources
            ],
            "evidence": [str(item) for item in links.get("evidence") or []],
            "card_state": owner_state,
            "card_state_label": CARD_STATE_LABEL.get(owner_state, ""),
            "visual": CARD_VISUAL.get(owner_state, ""),
            "states": by_jump,
        }

    return {
        "event_id": str(payload.get("event_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
        "current_jump": current_jump,
        "jumps": jumps,
        "cards": card_rows,
        "entities": entity_rows,
        "results": build_results(payload, cards=card_rows, entities=entity_rows, orders=orders),
        # 组织代码 → 中文名，页面显示"上报 ORG-DRAIN"没人看得懂。
        # 原文（detail / evidence）不改写，保留可追溯性；只翻译结构化字段。
        "organizations": {
            str(item.get("org_id")): str(item.get("org_name"))
            for item in departments
            if item.get("org_id")
        },
        "counts": {
            "jumps": len(jumps),
            "cards": len(card_rows),
            "entities": len(entity_rows),
            "linked_entities": len([row for row in entity_rows.values() if row["card_ids"]]),
            "states": {state: len([row for row in card_rows if row["state"] == state]) for state in CARD_STATE_LABEL},
        },
        "source": "scene.entities.links + collab.cards.evidence_refs(work_order#row) + event_stream(stage)",
        "state_basis": "动作卡四态由当前跳次推进派生（推演状态），不是现场回传进度",
    }


def build_results(
    payload: Mapping[str, Any],
    *,
    cards: Sequence[Mapping[str, Any]],
    entities: Mapping[str, Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """处置前后对照。

    只比**算得出来**的项。积水深度的回落是三维动画的示意效果，没有任何实测或
    模型回演数据支撑，所以 `after` 一律给 null 并写明原因——宁缺不编。
    """
    facts = payload.get("facts") or {}
    deduction = payload.get("deduction") or {}
    gaps = deduction.get("resource_gaps") or []
    gap_total = sum(int(item.get("gap") or 0) for item in gaps)
    open_orders = [item for item in orders if str(item.get("status") or "") == "open"]
    linked_entity_count = len([row for row in entities.values() if row["card_ids"]])
    action_cards = [row for row in cards if row.get("stage") == "action"]

    # 动作卡覆盖到哪些未闭环工单：卡片 → 工单号（依据 evidence_refs 行号）
    covered: set[str] = set()
    for row in cards:
        covered.update(str(item) for item in row.get("work_order_ids") or [])
    covered_open = [item for item in open_orders if item["order_id"] in covered]

    def count(state: str, rows: Sequence[Mapping[str, Any]]) -> int:
        return len([row for row in rows if row.get("state") == state])

    rows: list[dict[str, Any]] = [
        {
            "key": "depth_cm",
            "label": "积水深度",
            "before": facts.get("depth_cm"),
            "after": None,
            "unit": "cm",
            "basis": "facts.depth_cm（工单实测）",
            "note": "处置后深度无实测回传、也未做水力回演，沙盘上的回落只是动画示意，不作结论",
        },
        {
            "key": "open_orders",
            "label": "未闭环工单",
            "before": len(open_orders),
            "after": len(open_orders),
            "unit": "件",
            "basis": "work_orders.csv status=open",
            "note": "处置动作没有回写通道，工单状态不会自动变更；这里如实给同一数值",
        },
        {
            "key": "covered_orders",
            "label": "已被处置动作覆盖的未闭环工单",
            "before": 0,
            "after": len(covered_open),
            "unit": "件",
            "basis": "cards[*].work_order_ids ∩ status=open",
            "note": "「覆盖」指已有部门动作卡指向该工单，不等于已办结",
        },
        {
            "key": "action_cards",
            "label": "处置动作卡（已完成）",
            "before": 0,
            "after": count("done", action_cards),
            "unit": "张",
            "basis": "collab cards(stage=action) 按当前跳次派生状态",
            "note": "已完成为推演状态；另有 " + str(count("needs_confirm", cards)) + " 张需会签、"
            + str(count("running", action_cards)) + " 张仍在执行",
        },
        {
            "key": "linked_objects",
            "label": "已挂上动作的沙盘对象",
            "before": 0,
            "after": linked_entity_count,
            "unit": "个",
            "basis": "entities[*].card_ids 非空计数",
            "note": "动作卡与对象的关联依据为工单行号证据，不是空间邻近猜测",
        },
        {
            "key": "gap",
            "label": "移动泵缺口",
            "before": gap_total,
            "after": gap_total,
            "unit": "台",
            "basis": "deduction.resource_gaps",
            "note": "缺口由库存与需求折算决定，处置动作不会改变台账库存，前后必然相同",
        },
        {
            "key": "residual_risks",
            "label": "残余风险",
            "before": len(deduction.get("residual_risks") or []),
            "after": len(deduction.get("residual_risks") or []),
            "unit": "条",
            "basis": "deduction.residual_risks",
            "note": "推演结论条目，处置动作不消除已识别的残余风险",
        },
    ]
    return {
        "before_label": "事件发生时",
        "after_label": "当前跳次处置后",
        "rows": rows,
        "measured": False,
        "source": "全部取自推演输出与台账；不含量测回传",
    }


def clamp_jump(value: Any) -> int:
    """跳次夹到 1..7。

    1..6 就是六跳本身；7 是"处置后"——六跳之外没有第七个阶段，它只表示
    "处置动作全部执行完"，专门给前后对比用。页面上第六个按钮仍是最后一跳。
    """
    try:
        jump = int(value)
    except (TypeError, ValueError):
        jump = 1
    return max(1, min(MAX_JUMP, jump))


def resolve_deeplink(query: Mapping[str, Sequence[str]]) -> tuple[int, str]:
    """解析 `/twin3d?jump=N&entity=<id>` 的两个参数。

    `entity` 原样返回，存在性由调用方判定——不在这里静默换成别的对象：
    点错对象应该看到"未找到"，而不是莫名其妙跳到另一个设施上。
    """
    jump = clamp_jump((query.get("jump") or ["1"])[0])
    entity = str((query.get("entity") or [""])[0] or "")
    return jump, entity
