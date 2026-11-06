"""对外数字的单一真源守卫：**当前面向材料**里写死的计数必须等于权威源实算值。

为什么需要
----------
2b 资产化那一轮，`16/16`、`表格 9`、`101/102 三元组`、`28/32 条规则`、`817 测试`、`106 项自检`
在 10+ 个文件里漂过：人 grep 会漏（词序一变就漏、正则写宽就误报），CI 不会漏。
本文件把"写死的数字"变成断言：范围内任何命中，要么等于权威源实算值，要么在 allowlist 里带理由。

扫描范围（**全扫 + 排除**，不是显式清单）
--------------------------------------
- 根文件 `README.md`、`AGENTS.md`；
- `docs/**`、`scripts/**`、`samples/**` 下的 `.md` / `.py`；
- 排除：`docs/reports/**`（阶段报告保留落地时数字，政策如此）、`docs/superpowers/**`（设计快照）、
  `docs/tasks/**`、`tests/**`（本文件自己就全是正则字面量，扫自己必然自匹配）。

> 为什么从"显式清单"改成全扫：清单模式下**新增文档永远不会被扫到**。13 工具漂了两轮、
> `106/106` 在 `docs/image-semantics.md` 与 `docs/real-feedback-channel.md` 里挂了两版，
> 都是因为那两个文件不在清单里。全扫之后，新文档默认就被覆盖，漏掉的是"排除条"而不是"新增文件"。

权威源（不在这里手写第二份真值）
------------------------------
规则条数 ← `load_rules()`；资产数 ← `URBAN_ASSETS`；三元组 ← `urban_eval.evaluate`；
工具数 ← `UrbanToolkit().tools()`；自检项数 ← 期望文件（与 `evidence_check._compare` 同一套展开）；
测试数 ← 期望文件 `test_baseline.collected`；指纹 ← `expectations_digest`。
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

#: 扫描根：文件直接收，目录递归收。
SCAN_ROOTS: tuple[str, ...] = ("README.md", "AGENTS.md", "docs", "scripts", "samples")

#: 只扫这些后缀；二进制与 JSON 不参与（JSON 是权威源本身，扫它等于自证）。
SCAN_SUFFIXES: tuple[str, ...] = (".md", ".py")

#: 递归时跳过的前缀（相对仓库根）。
SCAN_EXCLUDES: tuple[str, ...] = (
    "docs/reports/",
    "docs/superpowers/",
    "docs/tasks/",
    "tests/",
)


def _scope_files() -> tuple[str, ...]:
    """展开扫描范围。返回相对仓库根的 posix 路径，排序后稳定。"""

    files: list[str] = []
    for root in SCAN_ROOTS:
        base = ROOT / root
        if base.is_file():
            files.append(root)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(rel.startswith(prefix) for prefix in SCAN_EXCLUDES):
                continue
            files.append(rel)
    return tuple(files)


SCOPE: tuple[str, ...] = _scope_files()

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
    # 「834 项通过」这类验收口径。以前不扫，`553 项通过` 在两份文档里挂了两版都没人发现。
    ("testcount", re.compile(r"(\d+)\s*项通过")),
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
    (
        "selfcheck",
        "docs/nexent-urban-integration-design.md",
        8,
    ): (1, "验收期望行里的「8/8 项一致」是平台主链路一致性，不是自检项数"),
    (
        "tools",
        "docs/p10-visualization-tools-design.md",
        9,
    ): (1, "「P5 起的 MCP 服务有 9 个工具」是阶段叙述（当时工具数），不是当前总数"),
    (
        "testcount",
        "docs/ICT-赛题3-城市内涝事件推演-WorkBuddy任务单.md",
        370,
    ): (1, "赛题任务单记录的是当时验收（370 项），属于任务书原文，不改写成当前值"),
    # delivery-status 如实记录了两份二进制交付物"当时"的旧数字（并标注了时点与重生成计划），
    # 这不是当前口径，但必须让读者看到差异在哪。
    (
        "tools",
        "docs/competition/delivery-status.md",
        13,
    ): (1, "引用 11-05 快照文件里的旧工具数，同行已标「当时的值」"),
    (
        "selfcheck",
        "docs/competition/delivery-status.md",
        106,
    ): (2, "同上，两份 11-05 快照（PPT 与信息表）的旧自检项数各一处"),
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
    payload = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    return {
        "rules": len(load_rules().rules),
        "assets": len(URBAN_ASSETS),
        # 「N 合成」= 资产总数 − 真实公开预案份数（不给第二处手写真值）
        "synthetic_assets": len(URBAN_ASSETS) - REAL_PUBLIC_PLAN_ASSETS,
        "real_plans": REAL_PUBLIC_PLAN_ASSETS,
        "triples": _urban_metrics().triples,
        "tools": len(UrbanToolkit().tools()),
        "selfcheck": _leaf_count(payload["checks"]),
        "testcount": int(payload["test_baseline"]["collected"]),
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


def test_test_count_in_materials_is_current() -> None:
    _assert_families("testcount")


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

#: 不做 Agent 版本断言的文档：赛题任务单是外部给定原文，里面「发布 v1 / 回滚到 v1」
#: 指的是本体版本，不是平台 Agent 版本。计数类断言仍扫它（见 ALLOWED_NON_TOTAL）。
VERSION_CLAIM_EXCLUDES = ("docs/ICT-赛题3-城市内涝事件推演-WorkBuddy任务单.md",)


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
        if rel in VERSION_CLAIM_EXCLUDES:
            continue
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

