"""Nexent 侧注册清单的一致性：`samples/nexent/tools.json` 必须与代码同源。

为什么值得测：平台注册靠手抄 schema 一定会漂——加了工具忘同步、改了必填项没更新。
这里把 `tools.json` 当成**生成产物**：它由
`python -m decision_poc.mcp_server --export-tools samples/nexent/tools.json` 生成，
测试负责保证它没和代码走散，并检查几个"不该出现的参数"（位姿/材质/镜头这类
低层渲染控制——智能体一旦能传这些，决策循环就会被渲染细节污染）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc.mcp_server import SERVER_NAME, TOOL_GROUPS, UrbanToolkit

REPO = Path(__file__).resolve().parents[1]
TOOLS_PATH = REPO / "samples" / "nexent" / "tools.json"
SKILLS_PATH = REPO / "samples" / "nexent" / "skills.json"

# 智能体不该拿到的渲染控制参数：给了它们，"渲染"就从决策动作退化成美术调参
FORBIDDEN_RENDER_PARAMS = {
    "location",
    "rotation",
    "transform",
    "camera",
    "lens",
    "material",
    "materials",
    "position",
    "scale",
    "color",
    "actor",
    "actor_path",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert TOOLS_PATH.is_file(), (
        "缺少 tools.json：跑 python -m decision_poc.mcp_server "
        "--export-tools samples/nexent/tools.json"
    )
    return json.loads(TOOLS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def skills() -> dict:
    return json.loads(SKILLS_PATH.read_text(encoding="utf-8"))


def test_manifest_matches_live_server(manifest: dict) -> None:
    toolkit = UrbanToolkit()
    assert manifest["server"]["name"] == SERVER_NAME
    assert [tool["name"] for tool in manifest["tools"]] == [
        tool["name"] for tool in toolkit.tools()
    ], "tools.json 与代码走散了：重新跑 --export-tools"


def test_every_tool_has_description_and_schema(manifest: dict) -> None:
    for tool in manifest["tools"]:
        assert tool["description"].strip(), tool["name"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"], tool["name"]


def test_groups_partition_all_tools(manifest: dict) -> None:
    names = {tool["name"] for tool in manifest["tools"]}
    grouped: list[str] = []
    for group, members in manifest["groups"].items():
        assert members, f"分组 {group} 是空的"
        grouped.extend(members)
    assert len(grouped) == len(set(grouped)), "一个工具不能同时属于两个分组"
    assert set(grouped) == names
    assert set(manifest["groups"]) == set(TOOL_GROUPS)


def test_presentation_and_bootstrap_split(manifest: dict) -> None:
    groups = manifest["groups"]
    assert groups["presentation"] == ["scene_render", "scene_compare"]
    assert groups["bootstrap"] == ["asset_lookup"]
    # 推演阶段不该看见渲染工具
    for name in groups["presentation"] + groups["bootstrap"]:
        assert name not in groups["decision"]


def test_render_tools_take_no_low_level_params(manifest: dict) -> None:
    render_names = {"scene_render", "scene_compare", "asset_lookup"}
    for tool in manifest["tools"]:
        if tool["name"] not in render_names:
            continue
        props = set((tool["inputSchema"].get("properties") or {}).keys())
        leaked = props & FORBIDDEN_RENDER_PARAMS
        assert not leaked, f"{tool['name']} 暴露了低层渲染参数 {leaked}"


def test_scene_compare_requires_both_ids(manifest: dict) -> None:
    tool = next(item for item in manifest["tools"] if item["name"] == "scene_compare")
    assert tool["inputSchema"]["required"] == ["baseline_id", "candidate_id"]


def test_skills_reference_only_real_tools(manifest: dict, skills: dict) -> None:
    known = {tool["name"] for tool in manifest["tools"]}
    for entry in skills["standalone_tools"]:
        assert set(entry["mcp_tools"]) <= known, entry["name"]
    for skill in skills["skills"]:
        assert set(skill["mcp_tools"]) <= known, skill["name"]


def test_every_tool_is_bound_somewhere(manifest: dict, skills: dict) -> None:
    """加了工具却没进绑定表，等于平台侧看不见它。"""
    bound: set[str] = set()
    for entry in skills["standalone_tools"]:
        bound.update(entry["mcp_tools"])
    for skill in skills["skills"]:
        bound.update(skill["mcp_tools"])
    missing = {tool["name"] for tool in manifest["tools"]} - bound
    assert not missing, f"这些工具没有绑定到任何 Skill/独立工具：{sorted(missing)}"


def test_presentation_entries_declare_their_group(skills: dict) -> None:
    by_name = {entry["name"]: entry for entry in skills["standalone_tools"]}
    assert by_name["scene_render"]["group"] == "presentation"
    assert by_name["scene_compare"]["group"] == "presentation"
    assert by_name["asset_lookup"]["group"] == "bootstrap"
    # 每个独立工具都要写清失败语义，否则编排层不知道能不能继续
    for entry in skills["standalone_tools"]:
        assert entry["failure_mode"].strip()
        assert entry["when"].strip()
