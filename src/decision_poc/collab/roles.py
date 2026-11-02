"""六个角色的声明与"规则 -> 责任部门"映射。

两条原则：

1. **N 由职责边界决定，不由场景数量决定**。现有职责台账只登记了 5 个专业部门
   加 1 个统筹部门，所以是 1 总控 + 5 专业；新增角色必须先有独立台账与职责原文。
2. **映射是数据，不是 if/else**。规则归属写在这张表里，新增规则只加一行；
   按前缀兜底（`RULE-RES-*` → 排水），保证未显式列出的规则也有确定归属，
   而不是落进"没人管"的黑洞。
"""

from __future__ import annotations

from pydantic import Field

from decision_poc.contracts import StrictModel

COORDINATOR = "ORG-EMERG"


class RoleSpec(StrictModel):
    """一个部门 Agent 的静态声明。"""

    agent_id: str = Field(min_length=1)
    department: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tool_groups: list[str] = Field(min_length=1)
    scope: str = Field(min_length=1)


ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(
        agent_id="EMERG-COORD",
        department=COORDINATOR,
        title="应急指挥长",
        tool_groups=["decision", "collaboration"],
        scope="统一定级、触发推演、汇总冲突、编排联合发布与处置顺序；不替专业部门出具体动作",
    ),
    RoleSpec(
        agent_id="ORG-DRAIN",
        department="ORG-DRAIN",
        title="排水处置",
        tool_groups=["role_drain"],
        scope="雨水口清掏、泵站补位、移动泵需求与抽排时序",
    ),
    RoleSpec(
        agent_id="ORG-ROAD",
        department="ORG-ROAD",
        title="道路封控",
        tool_groups=["role_road"],
        scope="封控路段、围挡与封路实施；与交通部门联合发布",
    ),
    RoleSpec(
        agent_id="ORG-TRAFFIC",
        department="ORG-TRAFFIC",
        title="交通管制",
        tool_groups=["role_traffic"],
        scope="绕行组织、管制点设置、高峰避让；与道路部门联合发布",
    ),
    RoleSpec(
        agent_id="ORG-GAS",
        department="ORG-GAS",
        title="燃气管线风险",
        tool_groups=["role_gas"],
        scope="基坑保护、停气降压、浓度检测与复检条件",
    ),
    RoleSpec(
        agent_id="ORG-POWER",
        department="ORG-POWER",
        title="供电保障",
        tool_groups=["role_power"],
        scope="泵站备电、浸水断电、发电车占用",
    ),
)

ROLE_BY_ID: dict[str, RoleSpec] = {role.agent_id: role for role in ROLES}
AGENT_IDS: tuple[str, ...] = tuple(role.agent_id for role in ROLES)

#: 规则 -> 责任部门。一条规则可以同时归多个部门（那就是联合动作）。
RULE_OWNERS: dict[str, tuple[str, ...]] = {
    "RULE-OWNER-EMERG-AUTONOMY": ("EMERG-COORD",),
    "RULE-OWNER-GAP-NO-MAINTAINER": ("EMERG-COORD",),
    "RULE-CONF-CLOSURE-JOINT": ("ORG-ROAD", "ORG-TRAFFIC"),
    "RULE-ACT-STOP-EXCAVATION": ("ORG-GAS",),
    "RULE-ACT-ISOLATE-FIRST": ("ORG-GAS", "ORG-POWER"),
    "RULE-ACT-CLOSE-ROAD": ("ORG-ROAD", "ORG-TRAFFIC"),
    "RULE-ACT-CLOSE-ROAD-JOINT": ("ORG-ROAD", "ORG-TRAFFIC"),
    "RULE-ACT-INTAKE-CLEAN-FIRST": ("ORG-DRAIN",),
    "RULE-ACT-PUMP-SECOND": ("ORG-DRAIN", "ORG-POWER"),
    "RULE-ACT-DETOUR": ("ORG-TRAFFIC",),
    "RULE-ACT-TRAFFIC-SYNC": ("ORG-TRAFFIC",),
    "RULE-ACT-RESUME-AFTER-DRAWDOWN": ("ORG-GAS",),
    "RULE-RISK-SOIL-LOSS": ("ORG-GAS",),
    "RULE-RISK-LEL-THRESHOLD": ("ORG-GAS",),
    "RULE-RISK-PUMP-POWER-LOSS": ("ORG-POWER",),
    "RULE-RISK-DETOUR-CONGESTION": ("ORG-TRAFFIC",),
    "RULE-RISK-PIT-EXPOSED-ORANGE": ("ORG-GAS", "EMERG-COORD"),
    "RULE-RISK-PIT-EXPOSED-RED": ("ORG-GAS", "EMERG-COORD"),
}

#: 前缀兜底：未显式登记的规则按前缀归口，避免出现"无主规则"。
RULE_PREFIX_OWNERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("RULE-RES-", ("ORG-DRAIN",)),
    ("RULE-ACT-CLOSE", ("ORG-ROAD", "ORG-TRAFFIC")),
    ("RULE-ACT-", ("EMERG-COORD",)),
    ("RULE-RISK-", ("EMERG-COORD",)),
    ("RULE-OWNER-", ("EMERG-COORD",)),
    ("RULE-CONF-", ("ORG-ROAD", "ORG-TRAFFIC")),
    ("RULE-AFF-", ("EMERG-COORD",)),
    ("RULE-OBS-", ("EMERG-COORD",)),
)

#: 处置顺序的硬约束（P12 §5.3）：前者必须不晚于后者。
#: 键是动作关键词，值是"必须更早或同时"的动作关键词。
ORDER_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("隔离", "抽排", "隔离风险源必须先于抽排（废止预案才会先抽排）"),
    ("封闭地下通道", "封闭积水路段", "封闭地下通道不晚于道路封控（生命风险优先）"),
)


def owners_for(rule_id: str, *, stage: str) -> tuple[str, ...]:
    """规则的归属部门：显式表优先，其次前缀兜底，最后按跳兜底到总控。"""

    explicit = RULE_OWNERS.get(rule_id)
    if explicit:
        return explicit
    for prefix, owners in RULE_PREFIX_OWNERS:
        if rule_id.startswith(prefix):
            return owners
    if stage in {"action", "conflict"}:
        return ("EMERG-COORD",)
    return ("EMERG-COORD",)
