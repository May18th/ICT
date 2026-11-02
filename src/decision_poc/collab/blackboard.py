"""协同板：所有角色写同一块板，读同一份内核事实。

刻意不做的事：不做消息队列、不做自由对话。角色之间只通过"卡片 id"交接，
这样"谁在什么时候基于哪条证据做了什么"是可重放的，而不是靠对话记录还原。
"""

from __future__ import annotations

from decision_poc.collab.contracts import ActionCard
from decision_poc.collab.roles import AGENT_IDS, ROLE_BY_ID
from decision_poc.deduction import DeductionResult


class Blackboard:
    """一次协同的共享状态。"""

    def __init__(self, deduction: DeductionResult) -> None:
        self.deduction = deduction
        self._cards: dict[str, list[ActionCard]] = {agent_id: [] for agent_id in AGENT_IDS}

    @property
    def event_id(self) -> str:
        return self.deduction.event_id

    def post(self, card: ActionCard) -> None:
        if card.agent_id not in self._cards:
            raise KeyError(f"未声明的角色: {card.agent_id}")
        self._cards[card.agent_id].append(card)

    def cards_of(self, agent_id: str) -> list[ActionCard]:
        return list(self._cards[agent_id])

    def all_cards(self) -> list[ActionCard]:
        return [card for agent_id in AGENT_IDS for card in self._cards[agent_id]]

    def accepted(self) -> list[ActionCard]:
        return [card for card in self.all_cards() if card.status.value == "accepted"]

    def uncovered_agents(self) -> list[str]:
        """没有任何卡片（含 no_action 卡）的角色——角色覆盖度的分母。"""

        return [agent_id for agent_id in AGENT_IDS if not self._cards[agent_id]]

    def role_title(self, agent_id: str) -> str:
        return ROLE_BY_ID[agent_id].title
