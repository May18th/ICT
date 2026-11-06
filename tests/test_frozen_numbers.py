"""对外数字的单一真源守卫：**当前面向材料**里写死的计数必须等于权威源实算值。

为什么需要
----------
2b 资产化那一轮，`16/16`、`表格 9`、`101/102 三元组`、`28/32 条规则`、`817 测试`、`106 项自检`
在 10+ 个文件里漂过：人 grep 会漏（词序一变就漏、正则写宽就误报），CI 不会漏。
本文件把"写死的数字"变成断言：范围内任何命中，要么等于权威源实算值，要么在 allowlist 里带理由。

扫描范围（显式清单，不用宽 glob）
--------------------------------
- 当前口径载体：`README.md`、`AGENTS.md`、`docs/README.md`、`docs/demo-runbook.md`、
  `docs/innovation-and-evidence.md`、`docs/competition-gap-audit.md`、`docs/competition/**`；
- 生成器与复现脚本：`scripts/build_answer_ppt.py`、`scripts/build_preliminary_docx.py`、`scripts/reproduce.py`；
- 数据与集成说明：`samples/assets/urban/README.md`、`samples/nexent/README.md`。

刻意排除：`docs/reports/**`（阶段报告保留落地时数字，政策如此）、`docs/superpowers/**`（设计快照）、
`docs/tasks/**`、`tests/**`（本文件自己就全是正则字面量，扫自己必然自匹配）。

权威源（不在这里手写第二份真值）
------------------------------
规则条数 ← `load_rules()`；资产数 ← `URBAN_ASSETS`；三元组 ← `urban_eval.evaluate`；
工具数 ← `UrbanToolkit().tools()`；自检项数 ← 期望文件 `checks` 的叶子数；指纹 ← `expectations_digest`。
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from decision_poc.deduction import load_rules
from decision_poc.evidence_check import (
    EXPECTATIONS_PATH,
    expectations_digest,
    load_expectations,
)
from decision_poc.mcp_server import UrbanToolkit
from decision_poc.urban_dataset import URBAN_ASSETS

ROOT = Path(__file__).resolve().parents[1]

SCOPE: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    "docs/README.md",
    "docs/demo-runbook.md",
    "docs/innovation-and-evidence.md",
    "docs/competition-gap-audit.md",
    *sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "docs" / "competition").glob("*.md")
    ),
    "scripts/build_answer_ppt.py",
    "scripts/build_preliminary_docx.py",
    "scripts/reproduce.py",
    "samples/assets/urban/README.md",
    "samples/nexent/README.md",
)

#: (family, 正则)。词序一变就漏，所以同一语义允许挂多条正则；新写法出现时在这里补一条。
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rules", re.compile(r"(\d+)\s*条规则")),
    ("rules", re.compile(r"规则\s*(\d+)\s*条")),
    ("assets", re.compile(r"(\d+)\s*个资产")),
    ("synthetic_assets", re.compile(r"(\d+)\s*个合成资产")),
    ("real_plans", re.compile(r"(\d+)\s*[个份]?\s*\**\s*真实公开预案")),
    ("triples", re.compile(r"(\d+)\s*(?:条)?三元组")),
    ("triples", re.compile(r"三元组\s*(\d+)\s*条")),
    ("triples", re.compile(r"三元组\s*/\s*谓词种类\s*\|\s*(\d+)\s*条")),
    ("tools", re.compile(r"(\d+)\s*个工具")),
    # 「13 工具」这种不带宽度的写法漏了两轮（13→14 时没人发现），单独挂一条：
    # `(\d+)\s*个工具` 要求有「个」，而材料里常写「14 工具注册」。
    ("tools", re.compile(r"(\d+)\s*工具")),
    ("selfcheck", re.compile(r"(\d+)/(\d+)\s*项一致")),
    ("selfcheck", re.compile(r"(\d+)\s*项自检")),
    ("selfcheck", re.compile(r"自检\s*(\d+)\s*项")),
    ("selfcheck", re.compile(r"(\d+)\s*项(?:宣称证据|冻结期望|对外宣称数字|逐条表|证据自检)")),
    ("fingerprint", re.compile(r"指纹\s*`?([0-9a-f]{16})")),
)

#: 真实公开预案的份数（2020 / 2026 应急预案原件）。只在这里写一次：
#: 「N 个资产」与「N 个合成资产」都由它派生，避免两处手写真值互相漂。
REAL_PUBLIC_PLAN_ASSETS = 2

#: 合法但**不是总数**的命中：(family, 文件, 值) -> (允许出现次数, 理由)。
#: 次数不是装饰：同一文件里同值若出现两次而只登记一次，说明有一处没解释，必须红。
ALLOWED_NON_TOTAL: dict[tuple[str, str, int | str], tuple[int, str]] = {
    ("tools", "README.md", 2): (1, "「Agent 只挂 2 个工具」是绑定数，不是工具总数"),
    (
        "tools",
        "docs/competition/winning-gap-audit.md",
        2,
    ): (1, "「2 工具主 Agent」是主 Agent 的挂载数，不是工具总数"),
    (
        "tools",
        "docs/competition/winning-gap-audit.md",
        6,
    ): (1, "「6 工具协同 Agent」是协同 Agent 的挂载数，不是工具总数"),
    (
        "tools",
        "docs/competition/video-storyboard-v0.1.md",
        2,
    ): (1, "旧版分镜里「（2 工具）」同样指主 Agent 挂载数"),
    ("selfcheck", "README.md", 8): (2, "两处「8/8 项一致」都是平台真机一致性，不是自检项数"),
    (
        "selfcheck",
        "docs/competition/preliminary-submission-draft.md",
        8,
    ): (1, "「8/8 项一致」是平台真机一致性，不是自检项数"),
}


def _leaf_count(value: object) -> int:
    """期望文件里一个叶子算一项（与 evidence_check._compare 的遍历方式一致）。"""

    if isinstance(value, dict):
        return sum(_leaf_count(item) for item in value.values())
    return 1


@cache
def _urban_metrics():
    from decision_poc.urban_eval import evaluate, load_cases

    return evaluate(load_cases()).metrics


@cache
def _expected() -> dict[str, int | str]:
    return {
        "rules": len(load_rules().rules),
        "assets": len(URBAN_ASSETS),
        # 「N 合成」= 资产总数 − 真实公开预案份数（不给第二处手写真值）
        "synthetic_assets": len(URBAN_ASSETS) - REAL_PUBLIC_PLAN_ASSETS,
        "real_plans": REAL_PUBLIC_PLAN_ASSETS,
        "triples": _urban_metrics().triples,
        "tools": len(UrbanToolkit().tools()),
        "selfcheck": _leaf_count(json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))["checks"]),
        "fingerprint": expectations_digest(load_expectations())[:16],
    }


def _scan() -> tuple[list[tuple[str, str, int | str, str]], list[str]]:
    """把范围内所有命中扫出来。左右不等（如 `106/108 项一致`）不在这里断言，
    而是作为问题返回——断言抛出来既没有文件名也没有行号，定位要靠猜。
    """

    matches: list[tuple[str, str, int | str, str]] = []
    inconsistent: list[str] = []
    for rel in SCOPE:
        path = ROOT / rel
        assert path.is_file(), f"扫描清单里的文件不存在：{rel}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for family, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    value: int | str = match.group(1)
                    if family == "fingerprint":
                        value = str(value)
                    else:
                        value = int(match.group(1))
                        if family == "selfcheck" and match.re.groups >= 2 and match.group(2):
                            second = int(match.group(2))
                            if value != second:
                                inconsistent.append(
                                    f"{rel}:{lineno} 自检数左右不等「{match.group(0)}」：{line.strip()[:80]}"
                                )
                    matches.append((rel, family, value, line.strip()))
    return matches, inconsistent


def _assert_families(*families: str) -> None:
    expected = _expected()
    matches, problems = _scan()
    counts: dict[tuple[str, str, int | str], int] = {}
    for rel, family, value, _ in matches:
        counts[(family, rel, value)] = counts.get((family, rel, value), 0) + 1

    for rel, family, value, line in matches:
        if family not in families:
            continue
        if (family, rel, value) in ALLOWED_NON_TOTAL:
            continue
        if value == expected[family]:
            continue
        problems.append(f"{rel} [{family}] 期望 {expected[family]!r}，实际 {value!r}：{line[:100]}")

    # 豁免必须与命中次数对得上：写成 1 次却出现 2 次，等于有一处没解释就被顺带放过。
    # 只核对真正被扫描的文件——否则临时收窄 SCOPE 时，范围外的条目会以"命中 0 次"误报。
    for (family, rel, value), (allowed_count, _reason) in ALLOWED_NON_TOTAL.items():
        if family not in families or rel not in SCOPE:
            continue
        actual = counts.get((family, rel, value), 0)
        if actual != allowed_count:
            problems.append(
                f"allowlist {rel} [{family}] {value!r} 声明 {allowed_count} 次，实际命中 {actual} 次"
            )

    assert not problems, "当前面向材料的数字与权威源不一致：\n" + "\n".join(problems)


def test_rules_count_in_materials_is_current() -> None:
    _assert_families("rules")


def test_asset_counts_in_materials_are_current() -> None:
    _assert_families("assets", "synthetic_assets", "real_plans")


def test_triple_count_in_materials_is_current() -> None:
    _assert_families("triples")


def test_tool_count_in_materials_is_current() -> None:
    _assert_families("tools")


def test_selfcheck_item_count_in_materials_is_current() -> None:
    _assert_families("selfcheck")


def test_fingerprint_in_materials_matches_expectations() -> None:
    _assert_families("fingerprint")


def test_allowlist_entries_are_still_needed() -> None:
    """allowlist 只允许装"合法非总数"；命中消失后必须删条目，否则会盖住未来的漂移。"""

    seen = {(family, rel, value) for rel, family, value, _ in _scan()[0]}
    stale = [key for key in ALLOWED_NON_TOTAL if key not in seen]
    assert not stale, f"allowlist 已失效，请删除这些条目：{stale}"


# ---------------------------------------------------------------------------
# 平台运行态：Agent 版本与模型名
#
# 这两项不在 PATTERNS 的"计数"体系里，却比计数更容易骗人：材料写 v10 + openPangu，
# 平台上跑的是 v19 + DeepSeek-Chat，评委现场一对就穿。权威源取真机抓下来的
# `samples/nexent/live/agents.json`（capture_evidence.py 的产物，不是手写）。
# ---------------------------------------------------------------------------

LIVE_AGENTS_PATH = ROOT / "samples" / "nexent" / "live" / "agents.json"

#: 「发布 vN」「重发布 v19 / v7」里的版本号。
VERSION_CLAIM = re.compile(r"发布\s*\**v(\d+)\**")

#: 已停服的旧平台模型。只允许出现在带原因的句子里（停服/欠费/已于/当时/历史）。
RETIRED_MODELS = ("openPangu-2.0-Pro",)
RETIRED_MARKERS = ("停服", "欠费", "已于", "当时", "历史", "原绑定")

#: 协同 Agent 自己也有版本号，行内提到它时跳过主 Agent 版本断言。
COLLAB_MARKERS = ("协同", "collab_agent")


@cache
def _live_agents() -> dict[str, dict[str, object]]:
    payload = json.loads(LIVE_AGENTS_PATH.read_text(encoding="utf-8"))
    return {item["name"]: item for item in payload["data"]}


def _scope_lines():
    for rel in SCOPE:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            yield rel, lineno, line


def test_agent_version_claims_match_platform() -> None:
    """材料里的「发布 vN」必须等于平台上主 Agent 的 current_version_no。"""

    expected = int(_live_agents()["urban_deduction_agent"]["current_version_no"])
    problems = []
    for rel, lineno, line in _scope_lines():
        if any(marker in line for marker in COLLAB_MARKERS):
            continue
        for match in VERSION_CLAIM.finditer(line):
            claimed = int(match.group(1))
            if claimed != expected:
                problems.append(
                    f"{rel}:{lineno} 期望 v{expected}，实际 v{claimed}：{line.strip()[:90]}"
                )
    assert not problems, (
        "对外材料里的 Agent 版本与平台真机记录不一致（权威源 samples/nexent/live/agents.json）：\n"
        + "\n".join(problems)
    )


def test_retired_model_only_appears_with_reason() -> None:
    """停服的旧模型名只能连原因一起出现，不能当成"当前用什么"来写。"""

    problems = []
    for rel, lineno, line in _scope_lines():
        if not any(model in line for model in RETIRED_MODELS):
            continue
        if not any(marker in line for marker in RETIRED_MARKERS):
            problems.append(f"{rel}:{lineno} 出现已停服模型但没写原因：{line.strip()[:90]}")
    assert not problems, (
        "这些位置把已停服的 openPangu 当成当前口径在写，补上「已于 2026-11-06 欠费停服」之类的说明：\n"
        + "\n".join(problems)
    )


def test_current_platform_model_is_what_materials_say() -> None:
    """平台当前模型以 agents.json 为准；材料里至少要有一次写对，避免只靠历史叙述过关。"""

    expected = str(_live_agents()["urban_deduction_agent"]["model_name"])
    hits = [
        f"{rel}:{lineno}"
        for rel, lineno, line in _scope_lines()
        if expected in line and "本地" not in line
    ]
    assert hits, f"对外材料没有一处说明当前平台模型是 {expected}"

