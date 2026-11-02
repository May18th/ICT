"""协同层契约：Agent 只能出"动作卡"，不能出"断言"。

这是 P12 规格 §4「断言权在内核」的代码化落点：

- `ActionCard` 不持有任何自由文本结论，只持有动作、对象、数量，以及**指向内核
  claim 的 id**；`card` 构造时校验 `claim_ids` 与 `evidence_refs` 非空，
  所以"Agent 编了一句话"在类型层面就过不去；
- `CollabResult` 把卡片、内核冲突、联合发布对、处置顺序与内核指纹放在一起，
  一致性由 `kernel_fingerprint` 判定，不由 Agent 自述。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from decision_poc.contracts import StrictModel
from decision_poc.deduction import DEDUCTION_STAGES, DepartmentConflict


class CardStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NO_ACTION = "no_action"


class ResourceBinding(StrictModel):
    """动作卡占用的资源：资源 id + 数量 + 单位。

    这是"任务分配"能落到部门手里的最小单位：一张卡要几台泵、几块围挡，
    写清楚才能在分配层与库存对账。
    """

    resource_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit: str = Field(min_length=1)

    @field_validator("resource_id", "unit")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("资源绑定字段不得为空白")
        return value


class ActionCard(StrictModel):
    """一个部门对本跳的一条可执行动作。

    `targets` 允许为空：联合发布、公文流转这类动作没有具体设施对象；
    但只要有动作，就必须有 `claim_ids` 与 `evidence_refs`——动作必须挂在可复核的断言上。
    """

    card_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    stage: str
    action: str = Field(min_length=1)
    targets: list[str] = Field(default_factory=list)
    quantity: int | None = Field(default=None, ge=0)
    depends_on: list[str] = Field(default_factory=list)
    co_sign_required: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    status: CardStatus = CardStatus.PROPOSED
    no_action_reason: str = ""
    rejection_reason: str = ""
    # 执行契约：时限、占用的资源、验收标准。缺省为空 = 还没排到执行层
    latest_execution_time: datetime | None = None
    required_resources: list[ResourceBinding] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)

    @field_validator("latest_execution_time")
    @classmethod
    def _deadline_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("执行时限必须带时区，否则跨部门对齐会错位")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria_not_blank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("验收标准不得出现空条目")
        return value

    @model_validator(mode="after")
    def _check_invariants(self) -> ActionCard:
        if self.stage not in DEDUCTION_STAGES:
            raise ValueError(f"unknown stage: {self.stage}")
        if any(not item.strip() for item in self.evidence_refs):
            raise ValueError("evidence_refs 不允许出现空条目")
        if any(not item.strip() for item in self.claim_ids):
            raise ValueError("claim_ids 不允许出现空条目")
        if self.status is CardStatus.NO_ACTION:
            if not self.no_action_reason.strip():
                raise ValueError("no_action 必须说明理由，否则角色覆盖度不可统计")
            if self.claim_ids or self.evidence_refs:
                raise ValueError("no_action 不应引用 claim 或证据")
            return self
        if not self.claim_ids:
            raise ValueError("有动作的卡必须指向至少一条内核 claim")
        if not self.evidence_refs:
            raise ValueError("有动作的卡必须带至少一条证据引用")
        if self.no_action_reason.strip():
            raise ValueError("非 no_action 不应带 no_action_reason")
        if self.status is CardStatus.REJECTED and not self.rejection_reason.strip():
            raise ValueError("rejected 必须给出拒绝理由")
        if self.status is not CardStatus.REJECTED and self.rejection_reason.strip():
            raise ValueError("只有 rejected 才能带 rejection_reason")
        return self


class RoleHandoff(StrictModel):
    """角色间结构化交接：只传卡片 id，不传自由对话。"""

    handoff_id: str = Field(min_length=1)
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    card_ids: list[str] = Field(min_length=1)
    note: str = ""


class CollabResult(StrictModel):
    event_id: str = Field(min_length=1)
    cards: list[ActionCard] = Field(min_length=1)
    conflicts: list[DepartmentConflict] = Field(default_factory=list)
    co_sign_pairs: list[tuple[str, str]] = Field(default_factory=list)
    handoffs: list[RoleHandoff] = Field(default_factory=list)
    merge_order: list[str] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    covered_agents: list[str] = Field(default_factory=list)
    kernel_fingerprint: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=1)
    rules_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _merge_order_must_name_cards(self) -> CollabResult:
        known = {card.card_id for card in self.cards}
        unknown = [item for item in self.merge_order if item not in known]
        if unknown:
            raise ValueError(f"merge_order 引用了不存在的卡片: {unknown}")
        if len(set(self.merge_order)) != len(self.merge_order):
            raise ValueError("merge_order 不应重复")
        for handoff in self.handoffs:
            missing = [card_id for card_id in handoff.card_ids if card_id not in known]
            if missing:
                raise ValueError(f"handoff {handoff.handoff_id} 引用了不存在的卡片: {missing}")
        return self


def kernel_fingerprint(steps: list[tuple[str, str, str, dict[str, str], list[str]]]) -> str:
    """内核结论指纹：只覆盖推演的确定性字段，不含 Agent 输出。

    输入是 `(step_id, stage, status, conclusion, evidence_refs)` 元组列表，
    排序后才哈希，保证同一份推演两次运行逐位一致。
    """

    payload = [
        {
            "step_id": step_id,
            "stage": stage,
            "status": status,
            "conclusion": dict(sorted(conclusion.items())),
            "evidence_refs": sorted(set(refs)),
        }
        for step_id, stage, status, conclusion, refs in steps
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
