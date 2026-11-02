"""多智能体协同层：1 总控 + 5 专业部门，协同板 + 规则裁决。

边界（P12 §4）：

- 本包不产生新结论，只把内核推演的 claim 翻译成部门动作卡；
- 联合发布与处置顺序由规则判定，不由 Agent 协商；
- 默认不调模型，`orchestrate()` 是纯函数，离线可重跑。
"""

from decision_poc.collab.blackboard import Blackboard
from decision_poc.collab.contracts import (
    ActionCard,
    CardStatus,
    CollabResult,
    ResourceBinding,
    RoleHandoff,
    kernel_fingerprint,
)
from decision_poc.collab.orchestrator import orchestrate
from decision_poc.collab.roles import (
    AGENT_IDS,
    COORDINATOR,
    ORDER_CONSTRAINTS,
    ROLE_BY_ID,
    ROLES,
    RoleSpec,
    owners_for,
)

__all__ = [
    "AGENT_IDS",
    "COORDINATOR",
    "ORDER_CONSTRAINTS",
    "ROLES",
    "ROLE_BY_ID",
    "ActionCard",
    "Blackboard",
    "CardStatus",
    "CollabResult",
    "ResourceBinding",
    "RoleHandoff",
    "RoleSpec",
    "kernel_fingerprint",
    "orchestrate",
    "owners_for",
]
