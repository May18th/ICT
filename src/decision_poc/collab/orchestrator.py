"""协同编排：把内核推演结果翻译成"六个部门各干什么"，而不是让模型自由写方案。

为什么这么设计（对应 P12 §3.4、§4）：

- **断言权在内核**：本模块不产生任何新结论，只把 `DeductionStep` 的 claim 与证据
  映射成部门动作卡；`ActionCard.claim_ids` 指向内核 claim，改不了、编不出；
- **裁决权在规则**：联合发布要求（`co_sign_required`）与处置顺序硬约束由本模块判定，
  Agent 只能 `accepted` / `rejected`，不能协商掉；
- **可复现**：`kernel_fingerprint` 只哈希推演的确定性字段，多 Agent 层再怎么变，
  指纹必须相同——这就是"多 Agent 不是自由发挥"的可断言形态。

本模块默认不调模型：`plan()` 是纯函数，离线可测。真实 LLM 适配器可以在保留
`ActionCard` 契约的前提下替换出卡策略，但不能绕过契约校验。
"""

from __future__ import annotations

from datetime import datetime

from decision_poc.collab.blackboard import Blackboard
from decision_poc.collab.contracts import (
    ActionCard,
    CardStatus,
    CollabResult,
    ResourceBinding,
    RoleHandoff,
    kernel_fingerprint,
)
from decision_poc.collab.roles import (
    AGENT_IDS,
    COORDINATOR,
    ORDER_CONSTRAINTS,
    owners_for,
)
from decision_poc.deduction import DeductionResult, StageStatus, claims_from

_ACTION_KEYS = ("action", "risk", "resolution", "include", "reason")


def _card_action(step) -> str:
    for key in _ACTION_KEYS:
        value = step.conclusion.get(key, "")
        if value:
            return value
    return step.stage


def _claim_by_step_id(deduction: DeductionResult) -> dict[str, str]:
    """step_id -> claim_id。只保留内核真正产出了 claim 的步骤。"""

    mapping: dict[str, str] = {}
    for claim in claims_from(deduction):
        step_id = claim.claim_id.removeprefix("CL-")
        if step_id and step_id != claim.claim_id:
            mapping[step_id] = claim.claim_id
    return mapping


def _owners_of(step) -> tuple[str, ...]:
    owners: list[str] = []
    for rule_id in step.rule_ids:
        for owner in owners_for(rule_id, stage=step.stage):
            if owner not in owners:
                owners.append(owner)
    if not owners:
        owners.append(COORDINATOR)
    return tuple(owners)


def _latest_execution_time(step) -> datetime | None:
    """只接受内核明确给出的、带时区的执行时限，不从动作文本猜时间。"""

    raw = step.conclusion.get("latest_execution_time") or step.conclusion.get("deadline")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _acceptance_criteria(step) -> list[str]:
    """读取内核提供的验收口径；缺少来源时保持空，不复制动作文本冒充验收。"""

    raw = step.conclusion.get("acceptance_criteria") or step.conclusion.get("acceptance")
    if not raw:
        return []
    return [item.strip() for item in raw.split("|") if item.strip()]


def build_cards(deduction: DeductionResult, board: Blackboard) -> None:
    """按"一跳 x 责任部门"出卡；多部门规则出多张卡并互相要求联合发布。"""

    claim_by_step = _claim_by_step_id(deduction)
    eligible_steps = [
        step
        for step in deduction.steps
        if step.status is not StageStatus.INSUFFICIENT and step.step_id in claim_by_step
    ]
    card_ids_by_step = {
        step.step_id: [f"AC-{step.step_id}-{owner}" for owner in _owners_of(step)]
        for step in eligible_steps
    }
    for step in eligible_steps:
        claim_id = claim_by_step[step.step_id]
        owners = _owners_of(step)
        action = _card_action(step)
        depends_on = [
            card_id
            for premise_step_id in step.premise_step_ids
            for card_id in card_ids_by_step.get(premise_step_id, [])
        ]
        for owner in owners:
            card = ActionCard(
                card_id=f"AC-{step.step_id}-{owner}",
                agent_id=owner,
                stage=step.stage,
                action=action,
                depends_on=depends_on,
                latest_execution_time=_latest_execution_time(step),
                acceptance_criteria=_acceptance_criteria(step),
                evidence_refs=sorted(set(step.evidence_refs)),
                claim_ids=[claim_id],
                co_sign_required=[item for item in owners if item != owner],
            )
            board.post(card)

    # 资源缺口单独出卡：缺口是"台数"，不是一句动作，必须带数量。
    for gap in deduction.resource_gaps:
        refs = sorted({ref for step in deduction.stage("resource") for ref in step.evidence_refs})
        claim_ids = [
            claim_by_step[step.step_id]
            for step in deduction.stage("resource")
            if step.step_id in claim_by_step
        ]
        if not refs or not claim_ids:
            continue
        board.post(
            ActionCard(
                card_id=f"AC-GAP-{gap.resource_id}-ORG-DRAIN",
                agent_id="ORG-DRAIN",
                stage="resource",
                action="dispatch_pump",
                quantity=gap.gap,
                required_resources=[
                    ResourceBinding(
                        resource_id=gap.resource_id,
                        quantity=gap.gap,
                        unit=gap.unit,
                    )
                ]
                if gap.gap > 0
                else [],
                evidence_refs=refs,
                claim_ids=claim_ids,
            )
        )


def resolve_co_sign(board: Blackboard) -> None:
    """联合发布判定：列名部门都出了同一条动作的卡才 accepted，否则 rejected。"""

    by_key: dict[tuple[str, str], set[str]] = {}
    for card in board.all_cards():
        if card.status is CardStatus.NO_ACTION:
            continue
        by_key.setdefault((card.stage, card.action), set()).add(card.agent_id)

    for agent_id in AGENT_IDS:
        for index, card in enumerate(board.cards_of(agent_id)):
            if card.status is CardStatus.NO_ACTION:
                continue
            if not card.co_sign_required:
                updated = card.model_copy(update={"status": CardStatus.ACCEPTED})
            else:
                present = by_key.get((card.stage, card.action), set())
                missing = [item for item in card.co_sign_required if item not in present]
                if missing:
                    updated = card.model_copy(
                        update={
                            "status": CardStatus.REJECTED,
                            "rejection_reason": f"联合发布缺少部门：{', '.join(missing)}",
                        }
                    )
                else:
                    updated = card.model_copy(update={"status": CardStatus.ACCEPTED})
            board._cards[agent_id][index] = updated  # noqa: SLF001 - 板内更新，接口保持最小


def fill_no_action(board: Blackboard) -> None:
    """没有职责的角色显式声明 no_action，否则角色覆盖度无法统计。"""

    for agent_id in AGENT_IDS:
        if board.cards_of(agent_id):
            continue
        board.post(
            ActionCard(
                card_id=f"AC-NONE-{agent_id}",
                agent_id=agent_id,
                stage="owner",
                action="no_action",
                status=CardStatus.NO_ACTION,
                no_action_reason="本次推演未命中该部门职责规则，按空白处理，不默认指派",
            )
        )


def _ordered_action_cards(deduction: DeductionResult, board: Blackboard) -> list[ActionCard]:
    """按内核算出的处置顺序排序；同序位按卡片 id 稳定排序。"""

    rank = {action: index for index, action in enumerate(deduction.action_order)}
    cards = [
        card
        for card in board.accepted()
        if card.stage == "action" and card.status is CardStatus.ACCEPTED
    ]
    return sorted(cards, key=lambda card: (rank.get(card.action, 99), card.card_id))


def _check_constraints(ordered: list[ActionCard]) -> list[str]:
    violations: list[str] = []
    for early_keyword, late_keyword, message in ORDER_CONSTRAINTS:
        early = next((i for i, card in enumerate(ordered) if early_keyword in card.action), None)
        late = next((i for i, card in enumerate(ordered) if late_keyword in card.action), None)
        if early is not None and late is not None and early > late:
            violations.append(message)
    return violations


def build_handoffs(deduction: DeductionResult, board: Blackboard) -> list[RoleHandoff]:
    """交接只有两个来源：内核冲突里的部门，以及联合发布对。

    交接必须指向**真实存在的卡片**：拿不到卡片就不发交接，不发一条指向空气的记录。
    """

    handoffs: list[RoleHandoff] = []
    seen: set[tuple[str, str]] = set()
    for conflict in deduction.conflicts:
        for org in conflict.orgs:
            agent_id = org if org in AGENT_IDS else COORDINATOR
            if agent_id == COORDINATOR:
                continue
            key = (COORDINATOR, agent_id)
            if key in seen:
                continue
            cards = [
                card
                for card in board.cards_of(agent_id)
                if card.status is not CardStatus.NO_ACTION
            ]
            conflict_cards = [card for card in cards if card.stage == "conflict"]
            target = conflict_cards or cards
            if not target:
                continue
            seen.add(key)
            handoffs.append(
                RoleHandoff(
                    handoff_id=f"HO-{conflict.conflict_id}-{agent_id}",
                    from_agent=COORDINATOR,
                    to_agent=agent_id,
                    card_ids=[target[0].card_id],
                    note=f"{conflict.kind.value}:{conflict.subject}",
                )
            )
    return handoffs


def orchestrate(deduction: DeductionResult) -> CollabResult:
    """一条推演 -> 一次完整协同（确定性，可离线重跑）。"""

    board = Blackboard(deduction)
    build_cards(deduction, board)
    resolve_co_sign(board)
    fill_no_action(board)

    ordered = _ordered_action_cards(deduction, board)
    violations = _check_constraints(ordered)

    co_sign_pairs = sorted(
        {
            tuple(sorted((card.agent_id, partner)))
            for card in board.all_cards()
            for partner in card.co_sign_required
        }
    )

    fingerprint = kernel_fingerprint(
        [
            (
                step.step_id,
                step.stage,
                step.status.value,
                dict(step.conclusion),
                list(step.evidence_refs),
            )
            for step in deduction.steps
        ]
    )

    return CollabResult(
        event_id=deduction.event_id,
        cards=board.all_cards(),
        conflicts=list(deduction.conflicts),
        co_sign_pairs=[(left, right) for left, right in co_sign_pairs],
        handoffs=build_handoffs(deduction, board),
        merge_order=[card.card_id for card in ordered],
        constraint_violations=violations,
        covered_agents=[agent_id for agent_id in AGENT_IDS if board.cards_of(agent_id)],
        kernel_fingerprint=fingerprint,
        snapshot_hash=deduction.snapshot_hash or f"event:{deduction.event_id}",
        rules_version=deduction.rules_version or "unknown",
    )
