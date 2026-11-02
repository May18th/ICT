"""角色表：N 由职责边界决定，映射是数据而不是散落的 if/else。"""

from __future__ import annotations

from decision_poc.collab.roles import (
    AGENT_IDS,
    COORDINATOR,
    ORDER_CONSTRAINTS,
    RULE_OWNERS,
    ROLES,
    owners_for,
)


def test_six_roles_one_coordinator_and_five_specialists() -> None:
    assert len(ROLES) == 6
    departments = [role.department for role in ROLES]
    assert len(set(departments)) == 6
    assert COORDINATOR in departments
    assert len(departments) - 1 == 5


def test_agent_id_matches_department_slug() -> None:
    for role in ROLES:
        if role.agent_id == "EMERG-COORD":
            continue
        assert role.agent_id == role.department


def test_every_owner_in_map_is_a_declared_agent() -> None:
    declared = set(AGENT_IDS)
    for rule_id, owners in RULE_OWNERS.items():
        assert owners, f"{rule_id} 没有归属部门"
        assert set(owners) <= declared, f"{rule_id} 指向未声明角色"


def test_explicit_rule_owner_wins() -> None:
    assert owners_for("RULE-ACT-CLOSE-ROAD", stage="action") == ("ORG-ROAD", "ORG-TRAFFIC")


def test_unknown_resource_rule_falls_back_to_drainage() -> None:
    assert owners_for("RULE-RES-UNKNOWN-FUTURE", stage="resource") == ("ORG-DRAIN",)


def test_unknown_rule_still_gets_coordinator() -> None:
    assert owners_for("RULE-SOMETHING-NEW", stage="action") == ("EMERG-COORD",)


def test_order_constraints_cover_生命优先_and_隔离优先() -> None:
    early = {item[0] for item in ORDER_CONSTRAINTS}
    assert "隔离" in early
    assert "封闭地下通道" in early
