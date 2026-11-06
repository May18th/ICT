"""锁住 SKILL.md 正文提到的工具，与主 Agent 实际挂载之间的差集。

为什么需要这条
--------------
主 Agent 故意只挂 2 个工具（`event_deduce` + `evidence_locate`），而 SKILL.md 正文按
完整链路写，会提到 `twin_neighbors` / `conflict_check` / `allocation_plan` 等。
这个不一致本身是**设计**，不是缺陷：

- 正文是给人看的流程说明，写全才有"这个技能原本要做什么"的信息；
- 主 Agent 挂窄是因为 2026-11-03 实测：模型看到那几个工具就会去调，随后走
  `parallel_executor`，回答截断在 `<|tool_call_start|>`，8/8 直接掉到 0/8。

风险在于：**平台哪天开始自动注入正文**（或有人放开 `read_skill_md`），这份差集就会
从"无害的文档偏差"变成"模型真的会去调没挂的工具"。所以这里做两件事：

1. 把差集**登记**下来，正文新增/删掉工具名时测试先红，逼人重新解释；
2. 断言提示词仍然禁用 `read_skill_md`——一旦解禁，风险模型变了，上面这份登记必须重估。

注意这不是"允许偏差"的白名单：登记的是**已知且带理由的偏差**，理由为空同样红。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "nexent"
SKILL_ROOT = ROOT / "samples" / "nexent" / "skill"

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import setup_nexent  # noqa: E402

#: 技能正文提到工具、但主 Agent 不挂载的部分，以及**为什么**。
#: 键必须与实测差集逐一对齐：多一个少一个都红。
BODY_TOOL_GAPS: Final[dict[str, dict[str, str]]] = {
    "urban-six-hop-deduction": {
        "twin_neighbors": (
            "六跳里的「受影响设施」由 event_deduce 一次返回；单独调等于绕开结论接口"
        ),
        "conflict_check": (
            "三类冲突已在 event_deduce 的 conflicts 字段里；11-03 实测模型看到该工具就去调，"
            "随后走 parallel_executor 把回答截断在 <|tool_call_start|>"
        ),
        "allocation_plan": (
            "分配方案已含在 event_deduce 的 action_order；独立调用同样触发上面那次 0/8"
        ),
    },
    "scarcity-allocation-evolution": {
        "allocation_plan": (
            "该技能描述的就是分配策略本身；主 Agent 不挂它，分配结论走 event_deduce 的 action_order"
        ),
        "scene_compare": "可视化对比属演示层，主推演链路不需要",
        "scene_render": "渲染由演示页单独调用，不进决策链路",
    },
}

#: 提示词里必须出现这句，否则"正文不被读取"的前提失效。
FORBID_READ_SKILL = "不要调用 read_skill_md"


def body_of(skill_md: Path) -> str:
    """SKILL.md 正文（去掉 frontmatter）。frontmatter 的 description 会被注入，不算正文。"""
    text = skill_md.read_text(encoding="utf-8")
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)


def tools_mentioned(skill_md: Path) -> set[str]:
    """正文里出现的已知工具名（只在 OUR_TOOLS 全集里匹配，避免把任意标识符当工具）。"""
    body = body_of(skill_md)
    return {
        name
        for name in setup_nexent.OUR_TOOLS
        if re.search(rf"\b{re.escape(name)}\b", body)
    }


def actual_gaps() -> dict[str, set[str]]:
    """每个技能：正文提到的工具 - 主 Agent 挂载。"""
    gaps: dict[str, set[str]] = {}
    for skill_md in sorted(SKILL_ROOT.glob("*/SKILL.md")):
        gaps[skill_md.parent.name] = tools_mentioned(skill_md) - setup_nexent.MAIN_TOOLS
    return gaps


def test_gap_registry_covers_exactly_the_skills_on_disk() -> None:
    on_disk = {item.parent.name for item in SKILL_ROOT.glob("*/SKILL.md")}
    assert on_disk == set(BODY_TOOL_GAPS), (
        f"技能目录与登记表不一致：磁盘 {sorted(on_disk)}，登记 {sorted(BODY_TOOL_GAPS)}"
    )


def test_body_tool_gaps_match_registry() -> None:
    """差集必须与登记逐一对齐——正文改了就重新解释，别让登记悄悄过期。"""

    problems: list[str] = []
    for skill, gaps in actual_gaps().items():
        registered = set(BODY_TOOL_GAPS.get(skill, {}))
        added = sorted(gaps - registered)
        removed = sorted(registered - gaps)
        if added:
            problems.append(
                f"{skill}: 正文新增了未挂载工具 {added}——要么挂上，要么在 BODY_TOOL_GAPS 里写清为什么不挂"
            )
        if removed:
            problems.append(
                f"{skill}: 登记的工具 {removed} 已不在正文差集里——正文改过，删掉对应登记"
            )
    assert not problems, "\n".join(problems)


def test_every_gap_has_a_real_reason() -> None:
    problems = [
        f"{skill}.{tool}: 理由太短或为空"
        for skill, gaps in BODY_TOOL_GAPS.items()
        for tool, reason in gaps.items()
        if len(reason.strip()) < 8
    ]
    assert not problems, "\n".join(problems)


def test_each_skill_body_mentions_at_least_one_tool() -> None:
    # 差集为空也算合法（正文只提挂了的那两个），但"一个都没提到"多半是文件写坏了
    for skill_md in sorted(SKILL_ROOT.glob("*/SKILL.md")):
        assert tools_mentioned(skill_md), f"{skill_md.parent.name} 正文里没有任何已知工具名"


def test_registered_gaps_are_really_not_mounted() -> None:
    # 防止登记表本身写错：登记成"没挂"的工具，必须确实不在 MAIN_TOOLS 里
    leaked = sorted(
        f"{skill}.{tool}"
        for skill, gaps in BODY_TOOL_GAPS.items()
        for tool in gaps
        if tool in setup_nexent.MAIN_TOOLS
    )
    assert not leaked, f"这些工具其实已挂载，不该登记成差集：{leaked}"


@pytest.mark.parametrize("prompt_name", ["DUTY_PROMPT", "COLLAB_PROMPT"])
def test_prompts_still_forbid_read_skill_md(prompt_name: str) -> None:
    """这两个提示词是"正文不参与推理"的唯一保证。

    一旦有人放开这条禁令，平台注入的技能正文就会真的进上下文，
    而正文提到的工具大多没挂——BODY_TOOL_GAPS 里的理由也就全部失效。
    所以这里让它在解禁的第一时间红，而不是等真机跑出 0/8。
    """

    prompt = getattr(setup_nexent, prompt_name)
    assert FORBID_READ_SKILL in prompt, (
        f"{prompt_name} 不再禁止 read_skill_md：技能正文可能被注入，"
        "需要重新评估 BODY_TOOL_GAPS 与 8/8 的口径"
    )
