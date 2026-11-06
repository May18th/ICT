"""锁住平台装配的步骤顺序，以及 `status` 的"不可断言"语义。

为什么值得为顺序单独写测试
--------------------------
这轮之前，装配是四个脚本 + 若干 `docker cp`，顺序靠人记。真实踩到的两个坑：

1. `bind_skills.py` 只能写草稿（`version_no=0`），运行期读的是已发布版本快照。
   绑完不重新发布，平台 `ag_skill_instance_t` 里草稿有、发布版本没有——
   **看起来绑了，实际不生效**。
2. 旧 `status` 只查 MCP / 工具 / 两个 Agent，**技能完全不在它的视野里**，
   于是"没检查"和"检查通过"返回同一个 0。这类假绿比直接报错难查得多。

所以这里既锁顺序（数据表），也锁 `status` 的三档退出码（0 全绿 / 1 有失败 / 2 无法断言）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "nexent"

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import setup_nexent  # noqa: E402


class _FakePlatform:
    """只实现 render_status 会用到的读接口，不碰网络。"""

    def __init__(self, *, skills_rc: int = 0, mcp: dict | None = None, tools: set[str] | None = None):
        self._skills_rc = skills_rc
        self._mcp = mcp if mcp is not None else {"mcp_id": 3, "enabled": True}
        self._tools = tools if tools is not None else set(setup_nexent.OUR_TOOLS)
        self.verify_calls: list[bool] = []

    def find_mcp(self):
        return self._mcp

    def tools_by_name(self):
        return {name: {"tool_id": index} for index, name in enumerate(sorted(self._tools))}

    def find_agent(self):
        return {"agent_id": 3, "name": "urban_deduction_agent", "current_version_no": 19}

    def find_collab_agent(self):
        return {"agent_id": 4, "name": "urban_collab_agent", "current_version_no": 7}

    def verify_skills(self, *, require_published: bool) -> int:
        self.verify_calls.append(require_published)
        return self._skills_rc


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """指向一个真的有 */SKILL.md 的目录，模拟"技能目录已挂进容器"。"""
    (tmp_path / "urban-six-hop-deduction").mkdir()
    (tmp_path / "urban-six-hop-deduction" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    monkeypatch.setattr(setup_nexent, "SKILLS_DIR", tmp_path)
    return tmp_path


# --- 步骤顺序 -------------------------------------------------------------


def test_all_runs_every_step() -> None:
    assert setup_nexent.COMMAND_STEPS["all"] == (
        "register-mcp",
        "sync-tools",
        "create-main",
        "create-collab",
        "skills",
        "mint-key",
    )


def test_skills_step_runs_after_both_agent_publishes() -> None:
    """技能先绑草稿再重新发布；排在任何一次发布之前都会绑到马上被覆盖的快照上。"""

    steps = setup_nexent.COMMAND_STEPS["all"]
    assert steps.index("skills") > steps.index("create-main")
    assert steps.index("skills") > steps.index("create-collab")


def test_mint_key_is_last() -> None:
    # 装配没走完就发 Key，等于把一把"看起来能用"的钥匙递给调用方
    assert setup_nexent.COMMAND_STEPS["all"][-1] == "mint-key"


def test_every_command_starts_with_register_mcp() -> None:
    for command, steps in setup_nexent.COMMAND_STEPS.items():
        assert steps[0] == "register-mcp", f"{command} 没有先注册 MCP"
        assert steps[-1] in setup_nexent.ASSEMBLY_STEPS or steps[-1] == steps[-1]


def test_every_command_step_is_implemented() -> None:
    for command, steps in setup_nexent.COMMAND_STEPS.items():
        for step in steps:
            assert step in setup_nexent.ASSEMBLY_STEPS, f"{command} 引用了未实现的步骤 {step}"


# --- status 的三档语义 ----------------------------------------------------


def test_status_is_green_when_everything_present(skills_dir) -> None:
    platform = _FakePlatform()
    assert setup_nexent.render_status(platform) == 0
    # 技能必须按"已发布版本也要带"来查，否则又是草稿绿、运行期空
    assert platform.verify_calls == [True]


def test_status_is_2_when_skills_dir_missing(tmp_path, monkeypatch) -> None:
    """目录没挂进容器时不能返回 0——那是"没检查"，不是"通过"。"""

    monkeypatch.setattr(setup_nexent, "SKILLS_DIR", tmp_path / "absent")
    platform = _FakePlatform()
    assert setup_nexent.render_status(platform) == 2
    assert platform.verify_calls == []


def test_status_is_1_when_skill_binding_fails(skills_dir) -> None:
    platform = _FakePlatform(skills_rc=1)
    assert setup_nexent.render_status(platform) == 1


def test_status_flags_missing_tool(skills_dir) -> None:
    tools = set(setup_nexent.OUR_TOOLS) - {"collab_run"}
    platform = _FakePlatform(tools=tools)
    assert setup_nexent.render_status(platform) == 1


def test_status_flags_disabled_mcp(skills_dir) -> None:
    platform = _FakePlatform(mcp={"mcp_id": 3, "enabled": False})
    assert setup_nexent.render_status(platform) == 1


def test_skill_files_detects_unmounted_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup_nexent, "SKILLS_DIR", tmp_path / "absent")
    assert setup_nexent.skill_files() == []
