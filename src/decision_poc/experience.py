"""P9.1 跨会话沉淀：分层决策记忆 + 人工评审反馈。

为什么单独一个模块
------------------
P9 的进化循环本身只在"一次进程内"成立：每次从 v1 起步、跑完就结束。要回答
"这套策略是沉淀下来的资产，还是每次重新调参"，需要两件东西：

1. **分层记忆**：把"什么场景 → 用了什么策略 → 实际偏差多少"写进经验库，
   下次遇到相似场景直接从已验证有效的策略起步（warm start），而不是从零搜。
   记忆分 Tenant / User / Agent 三层，越具体越优先（Agent > User > Tenant）。
2. **人工评审反馈**：纯模拟反馈会被质疑"不够真实"。领域负责人对方案做一次
   修正/否决，修正结果转成一条**评审用例**进入评测门禁——下一轮进化必须满足它，
   否则候选被拒。这样"避免同类否决"是可断言的行为，不是叙事。

诚实边界（必须跟着代码走）：

- 这里的记忆是**离线同构契约**：接口与 Nexent 的 Tenant/User/Agent 分层记忆对齐，
  但**没有在真实 Nexent 平台上验证**（平台侧需换成记忆 API 写入/检索）。报告与
  答辩口径必须写"离线同构，未真机验证"，不能包装成"已接入平台记忆"。
- 人工评审在演示中是**角色扮演**（`reviewer_role` 写明模拟），不是真实部门盖章。
- 记忆只存"已验证有效"的结果（含偏差数字与反馈指纹），不存模型权重、不做在线学习。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, model_validator

from .allocation import AllocationCase, AllocationExpect
from .contracts import StrictModel
from .deduction import DistrictSignal

MEMORY_SCHEMA_VERSION = "decision-memory-1"
REVIEW_SCHEMA_VERSION = "decision-reviews-1"

DEFAULT_REVIEWS_PATH = (
    Path(__file__).resolve().parents[2] / "samples" / "eval" / "decision_reviews.json"
)
DEFAULT_MEMORY_PATH = (
    Path(__file__).resolve().parents[2] / "samples" / "eval" / "decision_memory.json"
)

# 记忆分层：越具体越优先。Tenant 是租户级共性，User 是某用户偏好，Agent 是智能体
# 自己在同类场景上学到的策略。
SCOPE_PRECEDENCE: tuple[str, ...] = ("agent", "user", "tenant")


class MemoryScope(StrEnum):
    TENANT = "tenant"
    USER = "user"
    AGENT = "agent"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# --- 场景签名与经验条目 ---------------------------------------------------


class ScenarioSignature(StrictModel):
    """场景特征指纹：只保留做决策相关的部分，用于"相似场景"召回。

    不用事件编号/时间做键——那是"同一件事"，不是"同类场景"。这里用：
    - 涉及片区集合；
    - 供不应求的用例占比（资源紧张程度）；
    - 关键状态的组合（封路/危险源/无）。
    """

    districts: list[str] = Field(default_factory=list)
    shortage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    critical_mix: list[str] = Field(default_factory=list)
    cases: int = Field(default=0, ge=0)

    def key(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def similarity(self, other: "ScenarioSignature") -> float:
        """0~1 的相似度；三项加权，结果四舍五入到 6 位以保证可复现。"""
        left = set(self.districts)
        right = set(other.districts)
        union = left | right
        districts = len(left & right) / len(union) if union else 1.0
        shortage = 1.0 - abs(self.shortage_ratio - other.shortage_ratio)
        mix_left, mix_right = set(self.critical_mix), set(other.critical_mix)
        mix_union = mix_left | mix_right
        mix = len(mix_left & mix_right) / len(mix_union) if mix_union else 1.0
        return round(0.4 * districts + 0.3 * shortage + 0.3 * mix, 6)


def signature_for(cases: Sequence[AllocationCase]) -> ScenarioSignature:
    districts: set[str] = set()
    critical_mix: set[str] = set()
    shortage = 0
    for case in cases:
        districts.update(signal.district for signal in case.signals)
        patterns: list[str] = []
        for signal in case.signals:
            flags = []
            if signal.closure_required is True:
                flags.append("closure")
            if signal.has_hazard:
                flags.append("hazard")
            patterns.append("+".join(flags) if flags else "none")
        critical_mix.add("|".join(sorted(patterns)))
        if sum(item.demand for item in case.signals) > sum(
            item.local_available for item in case.signals
        ):
            shortage += 1
    return ScenarioSignature(
        districts=sorted(districts),
        shortage_ratio=round(shortage / len(cases), 6) if cases else 0.0,
        critical_mix=sorted(critical_mix),
        cases=len(cases),
    )


class ExperienceEntry(StrictModel):
    """一条"场景 → 策略 → 实际偏差"的经验。只存已评测过的结果。"""

    entry_id: str = Field(min_length=1)
    scope: MemoryScope = MemoryScope.AGENT
    signature: ScenarioSignature
    label: str = ""
    policy_version: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, float] = Field(default_factory=dict)
    feedback_digest: str = ""
    event_ids: list[str] = Field(default_factory=list)
    review_ids: list[str] = Field(default_factory=list)
    note: str = ""


def make_entry(
    *,
    scope: MemoryScope,
    signature: ScenarioSignature,
    label: str,
    policy_version: str,
    params: Mapping[str, Any],
    outcome: Mapping[str, float],
    feedback_digest: str,
    event_ids: Sequence[str] = (),
    review_ids: Sequence[str] = (),
    note: str = "",
) -> ExperienceEntry:
    entry_id = "M-" + _digest(
        {
            "signature": signature.model_dump(mode="json"),
            "label": label,
            "params": dict(params),
            "outcome": dict(outcome),
        }
    )[:12]
    return ExperienceEntry(
        entry_id=entry_id,
        scope=scope,
        signature=signature,
        label=label,
        policy_version=policy_version,
        params=dict(params),
        outcome={key: round(float(value), 6) for key, value in outcome.items()},
        feedback_digest=feedback_digest,
        event_ids=list(event_ids),
        review_ids=list(review_ids),
        note=note,
    )


class RecalledExperience(StrictModel):
    entry: ExperienceEntry
    similarity: float


class DecisionMemory:
    """追加式分层记忆；不做删除与改写，避免"记忆被调参污染"。

    召回排序：相似度降序 → 分层优先级（agent > user > tenant）→ entry_id 升序。
    warm start 选择：在相似度达标的条目里，先取实际偏差最小（已验证最有效），
    再取相似度更高，最后按 entry_id —— 保证同一记忆库必然给出同一起点。
    """

    def __init__(self, entries: Iterable[ExperienceEntry] = ()) -> None:
        self._entries: list[ExperienceEntry] = []
        self._seq = 0
        for entry in entries:
            self.remember(entry)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[ExperienceEntry, ...]:
        return tuple(self._entries)

    def remember(self, entry: ExperienceEntry) -> ExperienceEntry:
        # 追加式：同 id 视为同一条经验，重复写入直接返回既有条目（幂等）
        for existing in self._entries:
            if existing.entry_id == entry.entry_id:
                return existing
        self._seq += 1
        self._entries.append(entry.model_copy(deep=True))
        return self._entries[-1]

    def remember_many(self, entries: Iterable[ExperienceEntry]) -> "DecisionMemory":
        for entry in entries:
            self.remember(entry)
        return self

    def recall(
        self,
        signature: ScenarioSignature,
        *,
        min_similarity: float = 0.6,
        limit: int = 3,
    ) -> list[RecalledExperience]:
        hits = [
            RecalledExperience(entry=entry, similarity=entry.signature.similarity(signature))
            for entry in self._entries
        ]
        hits = [item for item in hits if item.similarity >= min_similarity]
        hits.sort(
            key=lambda item: (
                -item.similarity,
                SCOPE_PRECEDENCE.index(item.entry.scope.value),
                item.entry.entry_id,
            )
        )
        return hits[:limit]

    def warm_start(
        self, signature: ScenarioSignature, *, min_similarity: float = 0.6
    ) -> RecalledExperience | None:
        hits = self.recall(signature, min_similarity=min_similarity, limit=len(self._entries) or 1)
        if not hits:
            return None
        return min(
            hits,
            key=lambda item: (
                float(item.entry.outcome.get("deviation", 1.0)),
                -item.similarity,
                item.entry.entry_id,
            ),
        )

    # --- 持久化（跨进程/跨会话） -----------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "entries": [entry.model_dump(mode="json") for entry in self._entries],
        }

    def save(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: Path | str) -> "DecisionMemory":
        source = Path(path)
        if not source.is_file():
            return cls()
        payload = json.loads(source.read_text(encoding="utf-8"))
        return cls(ExperienceEntry.model_validate(item) for item in payload.get("entries", []))


# --- 人工评审反馈（第二进化信号） ----------------------------------------


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"


class ReviewRecord(StrictModel):
    """领域负责人对一次决策的评审。

    `signals` 是评审当时看到的决策输入快照：评审用例必须自包含，否则下一轮评测
    还得回头跑推演，别人无法复算。
    """

    review_id: str = Field(min_length=1)
    reviewer_role: str = Field(min_length=1)
    reviewer_note: str = "角色扮演，用于演示人工反馈通道，非真实部门批复"
    event_id: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    policy_version_reviewed: str = Field(min_length=1)
    decision: ReviewDecision
    reason: str = Field(min_length=1)
    signals: list[DistrictSignal] = Field(min_length=1)
    expected_allocation: dict[str, int] = Field(default_factory=dict)
    basis: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def modified_needs_allocation(self) -> "ReviewRecord":
        if self.decision is ReviewDecision.MODIFY and not self.expected_allocation:
            raise ValueError("modify 评审必须给出修正后的分配")
        return self

    def to_case(self) -> AllocationCase:
        """评审 → 评审用例。

        只评"谁拿几台"：跨区时延与排序不由评审负责，因此评审用例的
        transfer/order 维度在偏差计算中被置 0（见 allocation.case_deviation）。
        """
        expect = AllocationExpect(
            allocation_by_district=dict(self.expected_allocation),
            transfers=[],
            priority_order=[],
            critical_unmet={
                signal.district: max(
                    0,
                    signal.demand - self.expected_allocation.get(signal.district, 0),
                )
                for signal in self.signals
                if signal.closure_required is True or signal.has_hazard
                if signal.demand - self.expected_allocation.get(signal.district, 0) > 0
            },
        )
        return AllocationCase(
            case_id=f"R-{self.review_id}",
            title=f"人工评审（{self.reviewer_role}）：{self.reason}",
            split="review",
            basis=[
                f"人工评审（{self.reviewer_role}，{self.reviewer_note}）：{self.reason}",
                *self.basis,
            ],
            signals=list(self.signals),
            expect=expect,
        )


def load_reviews(path: Path | str = DEFAULT_REVIEWS_PATH) -> list[ReviewRecord]:
    source = Path(path)
    if not source.is_file():
        return []
    payload = json.loads(source.read_text(encoding="utf-8"))
    return [ReviewRecord.model_validate(item) for item in payload.get("reviews", [])]


__all__ = [
    "DEFAULT_MEMORY_PATH",
    "DEFAULT_REVIEWS_PATH",
    "DecisionMemory",
    "ExperienceEntry",
    "MEMORY_SCHEMA_VERSION",
    "MemoryScope",
    "REVIEW_SCHEMA_VERSION",
    "RecalledExperience",
    "ReviewDecision",
    "ReviewRecord",
    "SCOPE_PRECEDENCE",
    "ScenarioSignature",
    "load_reviews",
    "make_entry",
    "signature_for",
]
