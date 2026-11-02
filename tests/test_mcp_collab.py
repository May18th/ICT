"""MCP 协同工具：`collab_run` 与部门工具分组。

守住三件事：

1. 协同入口在工具清单里，且分组过滤能让部门 Agent **看不见**它（越权在工具层就拦住）；
2. 每个部门分组只包含决策语义工具，不暴露渲染/协同入口；
3. `collab_run` 的输出仍是内核产出的冲突与顺序，不是第二套结论。
"""

from __future__ import annotations

import pytest

from decision_poc.mcp_server import ROLE_TOOL_GROUPS, TOOL_GROUPS, UrbanToolkit

AS_OF = "2026-10-29T09:40:00+08:00"
EVENT = "WO-20261029-007"


@pytest.fixture(scope="module")
def toolkit() -> UrbanToolkit:
    return UrbanToolkit()


def test_collab_run_is_registered_in_the_tool_list(toolkit: UrbanToolkit) -> None:
    names = [tool["name"] for tool in toolkit.tools()]
    assert "collab_run" in names
    assert len(names) == 13


def test_collaboration_group_exposes_only_collab_run(toolkit: UrbanToolkit) -> None:
    names = [tool["name"] for tool in toolkit.tools(groups=["collaboration"])]
    assert names == ["collab_run"]


def test_role_groups_cannot_see_the_collaboration_entrypoint(toolkit: UrbanToolkit) -> None:
    decision_tools = set(TOOL_GROUPS["decision"])
    for group in sorted(ROLE_TOOL_GROUPS):
        names = {tool["name"] for tool in toolkit.tools(groups=[group])}
        assert names, f"{group} 没有可用工具"
        assert "collab_run" not in names
        assert names <= decision_tools


def test_every_group_names_a_registered_tool(toolkit: UrbanToolkit) -> None:
    known = {tool["name"] for tool in toolkit.tools()}
    for group, members in TOOL_GROUPS.items():
        assert set(members) <= known, f"{group} 指向了未注册的工具"


def test_collab_run_covers_six_roles_and_keeps_kernel_conflicts(toolkit: UrbanToolkit) -> None:
    result = toolkit.call(
        "collab_run",
        {"event_id": EVENT, "as_of": AS_OF, "caller_labels": ["ORG-EMERG"]},
    )
    assert result.status == "ok"
    data = result.data
    assert len(data["covered_agents"]) == 6
    assert {item["conflict_id"] for item in data["conflicts"]} == {"C1", "C2", "C3"}
    assert data["merge_order"]
    assert len(data["kernel_fingerprint"]) == 16
    assert data["co_sign_pairs"]


def test_collab_run_unknown_event_is_reported_not_invented(toolkit: UrbanToolkit) -> None:
    result = toolkit.call(
        "collab_run",
        {"event_id": "WO-NOT-EXIST", "as_of": AS_OF, "caller_labels": ["ORG-EMERG"]},
    )
    assert result.status != "ok"
