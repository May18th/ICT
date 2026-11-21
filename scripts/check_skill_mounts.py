#!/usr/bin/env python3
"""技能会调的工具 vs Agent 实际挂载：求差集，出现未登记的缺口即拦。

为什么要有这个脚本
------------------
技能作者按整个 MCP 服务写正文，而工具是按 Agent 挂载的。平台两边各校验各的：
`POST /agent/check_skills` 只查技能重名，从不查工具引用。结果是「技能绑上去、列表全绿、
跑起来截断」。更麻烦的是**平台侧没有「技能 -> 工具」的结构化声明**——
`samples/nexent/live/skills.json` 里只有 name / sha256 / description，
所以除了扫 SKILL.md 正文，没有第二处可读。

`tests/test_skill_body_tools.py` 已经把这份差集登记成 `BODY_TOOL_GAPS`（逐项带理由），
但那是 pytest 里的事。本脚本把同一件事做成不依赖 pytest 的一次性检查，供发布前手工跑，
并且**把登记表当基线**：缺口与登记一致 -> 已知且已解释（退出 0）；出现登记之外的新缺口
-> 退出 1（要么补挂载，要么写清为什么不挂）。

挂载集有两个来源，默认用代码常量，因为导出包有个坑：
- `const`（默认）：`deploy/nexent/setup_nexent.py` 的 `MAIN_TOOLS` / `COLLAB_TOOLS`，
  与发布链路同一份定义。
- `export`：`/agent/export` 导出包里的 `agent_info.<id>.tools`。**它读的是草稿版本**
  （现行运行版是发布版），2026-11-14 那份快照就只有 2 个工具、缺 event_deduce，
  直接拿它判会得出完全相反的结论，所以脚本会先核对工具数再决定是否采用。

只读：不连平台、不改文件。

用法：
    python scripts/check_skill_mounts.py
    python scripts/check_skill_mounts.py --mounted-from export --agent-zip <导出包>
    python scripts/check_skill_mounts.py --json out/skill_mounts.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "nexent"
SKILL_ROOT = ROOT / "samples" / "nexent" / "skill"
BINDINGS = ROOT / "samples" / "nexent" / "live" / "skill_bindings.json"
REGISTRY_SOURCE = ROOT / "tests" / "test_skill_body_tools.py"
DEFAULT_AGENT_ZIP = ROOT / "samples" / "nexent" / "live" / "urban_deduction_agent.zip"

#: Agent 名 -> 代码里的挂载常量名。平台里的 Agent 名以 `agents.json` 为准。
MOUNT_CONSTANTS: Final[dict[str, str]] = {
    "urban_deduction_agent": "MAIN_TOOLS",
    "urban_collab_agent": "COLLAB_TOOLS",
}

if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))


def load_setup():
    import setup_nexent  # noqa: PLC0415 — 延迟导入：脚本本身要能被无依赖地导入

    return setup_nexent


def load_registry() -> dict[str, dict[str, str]]:
    """读 pytest 里的 `BODY_TOOL_GAPS` 当基线；读不到就退化为空表（缺口一律算新）。"""
    if not REGISTRY_SOURCE.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("_skill_body_tools", REGISTRY_SOURCE)
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(getattr(module, "BODY_TOOL_GAPS", {}))


def body_tool_mentions(skill_md: Path, known: set[str]) -> set[str]:
    """SKILL.md 正文提到的已知工具名（frontmatter 的 description 会被注入，不算正文）。"""
    text = skill_md.read_text(encoding="utf-8")
    body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    return {name for name in known if re.search(rf"\b{re.escape(name)}\b", body)}


def mounted_from_export(bundle: Path, agent_name: str) -> tuple[set[str], list[str]]:
    """从导出包取挂载集；返回（工具集, 警告）。草稿快照会被判为不可信。"""
    if not bundle.is_file():
        return set(), [f"找不到导出包：{bundle}"]
    with zipfile.ZipFile(bundle) as zf:
        raw = json.loads(zf.read("agent.json").decode("utf-8"))

    warnings: list[str] = []
    for cfg in (raw.get("agent_info") or {}).values():
        if cfg.get("name") and cfg["name"] != agent_name:
            continue
        tools = {
            str(tool.get(key))
            for tool in (cfg.get("tools") or [])
            for key in ("name", "class_name")
            if isinstance(tool, dict) and tool.get(key)
        }
        return tools, warnings
    return set(), [f"导出包里没有 {agent_name}"]


def load_bindings() -> tuple[str, list[str], int | None]:
    """`skill_bindings.json`：当前绑在哪个 Agent 上、绑了哪几个技能、记录时是第几版。"""
    data = json.loads(BINDINGS.read_text(encoding="utf-8"))
    agent = (data.get("agent") or {}).get("name")
    skills = sorted((data.get("expected_skills") or {}))
    if not agent:
        raise SystemExit(f"{BINDINGS} 里没有 agent.name")
    return agent, skills, (data.get("agent") or {}).get("current_version_no")


def current_version(agent_name: str) -> int | None:
    """`live/agents.json` 里该 Agent 当前发布到第几版；读不到就不判断。"""
    path = ROOT / "samples" / "nexent" / "live" / "agents.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data.get("data") or []:
        if item.get("name") == agent_name:
            return item.get("current_version_no")
    return None


def check(agent_name: str, skills: list[str], mounted_source: str, bundle: Path,
          recorded_version: int | None = None) -> dict:
    setup_nexent = load_setup()
    known: set[str] = set(setup_nexent.OUR_TOOLS)
    warnings: list[str] = []

    # 绑定快照会陈旧：技能是不是还绑着、绑的是第几版，都只代表记录那一刻
    live_version = current_version(agent_name)
    if recorded_version is not None and live_version is not None and live_version != recorded_version:
        warnings.append(
            f"绑定快照陈旧：记录时是 v{recorded_version}，平台现为 v{live_version}"
            f"——重新发版后技能集合可能已变，发布前重跑 bind_skills.py verify"
        )

    if mounted_source == "export":
        mounted, export_warnings = mounted_from_export(bundle, agent_name)
        warnings.extend(export_warnings)
        const_name = MOUNT_CONSTANTS.get(agent_name)
        expected = getattr(setup_nexent, const_name, set()) if const_name else set()
        # 导出包读草稿：工具数比常量少，说明它不是运行版本，不能用来判定
        if expected and mounted != set(expected):
            warnings.append(
                f"导出包挂载 {len(mounted)} 个（{', '.join(sorted(mounted)) or '空'}），"
                f"代码常量 {const_name} 是 {len(expected)} 个——导出读的是草稿，"
                f"与实际运行版本不一致，已改用常量"
            )
            mounted = set(expected)
    else:
        const_name = MOUNT_CONSTANTS.get(agent_name)
        if not const_name:
            raise SystemExit(f"未知 Agent {agent_name}：请在 MOUNT_CONSTANTS 里登记其挂载集")
        mounted = set(getattr(setup_nexent, const_name))
        warnings.append(f"挂载集来自代码常量 {const_name}")

    registry = load_registry()
    rows: list[dict] = []
    for skill in skills:
        skill_md = SKILL_ROOT / skill / "SKILL.md"
        if not skill_md.is_file():
            rows.append({"skill": skill, "missing_file": True, "gap": [], "registered": {}})
            continue
        mentioned = body_tool_mentions(skill_md, known | mounted)
        gap = sorted(mentioned - mounted)
        registered = {tool: registry.get(skill, {}).get(tool, "") for tool in gap}
        rows.append(
            {
                "skill": skill,
                "mentioned": sorted(mentioned),
                "gap": gap,
                "registered": registered,
                "unregistered": [tool for tool, reason in registered.items() if not reason],
                "stale": sorted(set(registry.get(skill, {})) - set(gap)),
            }
        )

    return {
        "agent": agent_name,
        "mounted_source": mounted_source,
        "mounted": sorted(mounted),
        "warnings": warnings,
        "rows": rows,
    }


def render(report: dict) -> str:
    lines = [f"{report['agent']}  挂载 {len(report['mounted'])} 个：{', '.join(report['mounted'])}"]
    lines.extend(f"  · {w}" for w in report["warnings"])
    lines.append("")
    lines.append(f"{'skill':<32}{'正文提及':>8}{'未挂载':>8}  说明")
    lines.append("-" * 74)
    for row in report["rows"]:
        if row.get("missing_file"):
            lines.append(f"{row['skill']:<32}{'-':>8}{'-':>8}  缺 SKILL.md")
            continue
        gap = ", ".join(row["gap"]) or "-"
        note = "已登记" if row["gap"] and not row["unregistered"] else ("含未登记" if row["gap"] else "无缺口")
        lines.append(f"{row['skill']:<32}{len(row['mentioned']):>8}{len(row['gap']):>8}  {note}：{gap}")
        for tool in row["unregistered"]:
            lines.append(f"{'':<32}{'':>8}{'':>8}  ✗ {tool} 没有理由")
        if row["stale"]:
            lines.append(f"{'':<32}{'':>8}{'':>8}  ! 登记表里 {', '.join(row['stale'])} 已不在差集内")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="技能引用的工具 vs Agent 实际挂载（只读）")
    parser.add_argument("--agent", default=None, help="默认取 skill_bindings.json 里的 Agent")
    parser.add_argument("--mounted-from", choices=("const", "export"), default="const")
    parser.add_argument("--agent-zip", type=Path, default=DEFAULT_AGENT_ZIP)
    parser.add_argument("--json", type=Path, default=None, help="把结果落盘为 JSON")
    args = parser.parse_args(argv)

    agent_name, skills, recorded_version = load_bindings()
    if args.agent:
        agent_name = args.agent

    report = check(agent_name, skills, args.mounted_from, args.agent_zip, recorded_version)
    print(render(report))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n[json] {args.json}")

    bad = [row for row in report["rows"]
           if row.get("missing_file") or row["unregistered"] or row["stale"]]
    if bad:
        print(f"\n{len(bad)} 个技能与登记表不一致：新缺口要补挂载或写理由，过期登记要删", file=sys.stderr)
        for row in bad:
            problems = []
            if row.get("missing_file"):
                problems.append("缺 SKILL.md")
            if row["unregistered"]:
                problems.append(f"未登记 {row['unregistered']}")
            if row["stale"]:
                problems.append(f"登记过期 {row['stale']}")
            print(f"  - {row['skill']}：{'；'.join(problems)}", file=sys.stderr)
        return 1

    total = sum(len(row["gap"]) for row in report["rows"])
    print(f"\n{total} 处缺口全部在登记表里且都带理由。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
