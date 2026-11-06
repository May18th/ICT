"""二进制交付物的数字守卫（只用标准库，不引入 python-docx / python-pptx）。

为什么单独一个文件
------------------
`test_frozen_numbers.py` 只扫 `.md`/`.py`，所以 `.pptx` / `.docx` 里的数字**结构上扫不到**：
`初赛答辩-PPT-v0.1.pptx` 里写着「776 测试 · 106 项自检」「openPangu-2.0-Pro」，
`初赛作品信息收集表-内容稿-v0.1.docx` 里写着「13 工具 / v10 / conversation_id=78 / 639 项通过」，
两轮口径变更都没被发现。这两份是**要交到评委手里**的文件，比文档更容易翻车。

做法
----
Office 文件本质是 zip：`ppt/slides/slideN.xml` 与 `word/document.xml` 里，
正文都落在 `<a:t>` / `<w:t>` 文本节点。用 `zipfile` 直接读，`re` 取节点文本，再套
`test_frozen_numbers.PATTERNS`（同一套口径，避免两处各写一份正则）。

两种状态
--------
- **当前交付物**：数字必须等于权威源实算值；
- **已登记的快照**（`STALE_SNAPSHOTS`）：数字必须等于**登记时的旧值**。这一条才是关键——
  登记不是消音，它把旧值也钉住了：数字再漂一次（既不是当前值也不是登记值）就红，
  提醒你这文件被改过；哪天重跑生成器把它更新到当前口径了，`test_stale_entries_are_still_stale`
  会红，要求删掉登记。
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from functools import cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_frozen_numbers import (  # noqa: E402
    PATTERNS,
    RETIRED_MARKERS,
    RETIRED_MODELS,
    _expected,
    _live_agents,
)

ROOT = Path(__file__).resolve().parents[1]

#: 二进制交付物的搜索根（相对仓库根）。docs/reports 下的阶段快照不在其中。
OFFICE_ROOTS: tuple[str, ...] = ("docs",)

#: 只认这两种容器；其余扩展名忽略。
OFFICE_SUFFIXES: tuple[str, ...] = (".pptx", ".docx")

#: 正文所在的 XML（pptx 按页取，docx 取正文）。
SLIDE_XML = re.compile(r"ppt/slides/slide\d+\.xml$")
WORD_XML = re.compile(r"word/document\.xml$")

#: `<a:t>` / `<w:t>` 文本节点。注意必须是 `(?:a|w):t` 后面紧跟空白或 `>`，
#: 写成 `[^>]*` 会把 `<w:tcPr>` 也吃进来（踩过）。
TEXT_NODE = re.compile(r"<(?:a|w):t(?:\s[^>]*)?>(.*?)</(?:a|w):t>", re.S)

#: 「发布 v19」「Agent v10」「（v10）」这类版本号。排除 `v2.7.0` 这种小节点版本：
#: 数字后面紧跟 `.` 或数字就不算。
VERSION_CLAIM = re.compile(r"v(\d{1,2})(?![\d.])")

#: 已登记的快照：相对路径 -> 登记信息。
#: `values` 只登记**与当前权威值不同**的项；没登记的 family 仍按当前值校验。
STALE_SNAPSHOTS: dict[str, dict[str, object]] = {
    "docs/competition/初赛答辩-PPT-v0.1.pptx": {
        "captured": "2026-11-05",
        "values": {
            "testcount": 776,
            "selfcheck": 106,
            "assets": 16,
            "model": "openPangu-2.0-Pro",
        },
        "reason": (
            "11-05 版式定稿的产物。重跑 scripts/build_answer_ppt.py 需要 python-pptx + Pillow，"
            "不是默认依赖（uv sync --locked 不装），不能在验收环境里临时装包。"
            "重做/套模板时一并重跑，之后删掉本登记。"
        ),
    },
    "docs/competition/初赛作品信息收集表-内容稿-v0.1.docx": {
        "captured": "2026-11-05",
        "values": {
            "testcount": 639,
            "selfcheck": 106,
            "fingerprint": "65e58b1e98039670",
            "tools": 13,
            "assets": 15,
            "synthetic_assets": 15,
            "rules": 28,
            "triples": 101,
            "agent_version": 10,
            "conversation_id": 78,
            "model": "openPangu-2.0-Pro",
        },
        "reason": (
            "同上，产物来自 11-05；脚本 build_preliminary_docx.py 已是新口径，纯属没重跑。"
            "官方模板到手后必须重生成再提交，之后删掉本登记。"
        ),
    },
}


def _office_files() -> tuple[str, ...]:
    files: list[str] = []
    for root in OFFICE_ROOTS:
        for path in sorted((ROOT / root).rglob("*")):
            if path.is_file() and path.suffix.lower() in OFFICE_SUFFIXES:
                files.append(path.relative_to(ROOT).as_posix())
    return tuple(files)


def _texts(rel: str) -> list[str]:
    """取出容器里所有正文文本节点。"""

    chunks: list[str] = []
    with zipfile.ZipFile(ROOT / rel) as archive:
        for name in sorted(archive.namelist()):
            if not (SLIDE_XML.search(name) or WORD_XML.search(name)):
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            chunks.extend(TEXT_NODE.findall(xml))
    return chunks


@cache
def _current() -> dict[str, int | str]:
    expected = dict(_expected())
    expected["agent_version"] = int(_live_agents()["urban_deduction_agent"]["current_version_no"])
    expected["conversation_id"] = 120
    expected["model"] = str(_live_agents()["urban_deduction_agent"]["model_name"])
    return expected


def _wanted(rel: str, family: str) -> tuple[object, str]:
    """该文件该口径应当出现的值，以及出错时的说明。"""

    snapshot = STALE_SNAPSHOTS.get(rel)
    if snapshot and family in (snapshot["values"] or {}):
        return snapshot["values"][family], "登记快照值"
    return _current()[family], "当前权威值"


def _scan(rel: str) -> list[str]:
    problems: list[str] = []

    for text in _texts(rel):
        for family, pattern in PATTERNS:
            for match in pattern.finditer(text):
                value: int | str = match.group(1)
                if family == "fingerprint":
                    value = str(value)
                else:
                    value = int(match.group(1))
                    if family == "selfcheck" and match.re.groups >= 2 and match.group(2):
                        if value != int(match.group(2)):
                            problems.append(
                                f"{rel} 自检数左右不等「{match.group(0)}」：{text.strip()[:80]}"
                            )
                want, label = _wanted(rel, family)
                if value != want:
                    problems.append(
                        f"{rel} [{family}] 实际 {value!r}，{label} {want!r}：{text.strip()[:80]}"
                    )

        for model in RETIRED_MODELS:
            if model not in text:
                continue
            if any(marker in text for marker in RETIRED_MARKERS):
                continue
            want, label = _wanted(rel, "model")
            if model == want:
                # 登记快照里明确写了"这份文件当时就是 openPangu"，不算问题
                continue
            problems.append(
                f"{rel} [model] 出现已停服模型 {model}，{label} {want!r}：{text.strip()[:80]}"
            )

        for match in VERSION_CLAIM.finditer(text):
            claimed = int(match.group(1))
            if claimed < 5:  # v1/v2 之类多是条目编号或本体版本，不按 Agent 版本算
                continue
            want, label = _wanted(rel, "agent_version")
            if claimed != int(want):
                problems.append(
                    f"{rel} [agent_version] 实际 v{claimed}，{label} v{want}：{text.strip()[:80]}"
                )

    return problems


def test_office_deliverables_match_authoritative_numbers() -> None:
    files = _office_files()
    assert files, "没有找到任何 .pptx/.docx，检查 OFFICE_ROOTS 是否写错"

    problems: list[str] = []
    for rel in files:
        problems.extend(_scan(rel))

    assert not problems, (
        "二进制交付物里的数字与权威源/登记快照不一致：\n" + "\n".join(problems)
    )


def test_stale_entries_are_still_stale() -> None:
    """登记的快照一旦被重跑更新，登记就该删掉，否则会盖住以后的新漂移。"""

    problems: list[str] = []
    for rel, snapshot in STALE_SNAPSHOTS.items():
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"登记的文件不存在：{rel}")
            continue
        leftovers = []
        for family, stale_value in (snapshot["values"] or {}).items():
            if family not in ("testcount", "selfcheck", "fingerprint", "tools",
                              "agent_version", "conversation_id", "model"):
                continue
            current = _current()[family]
            if stale_value != current:
                leftovers.append(f"{family}={stale_value!r}（当前 {current!r}）")
        if not leftovers:
            problems.append(f"{rel} 已与当前口径一致，请删除 STALE_SNAPSHOTS 里的登记")
    assert not problems, "\n".join(problems)


def test_office_texts_are_extractable() -> None:
    """守卫本身要能读到字：抽不出文本时不能静默通过。"""

    for rel in _office_files():
        texts = _texts(rel)
        assert texts, f"{rel} 抽不出任何文本节点，守卫形同虚设"
        assert any(re.search(r"\d", item) for item in texts), f"{rel} 文本里没有任何数字，检查抽取逻辑"


def test_registered_snapshots_are_documented() -> None:
    """每个登记项都要带采集日期与理由，避免变成无记录的消音名单。"""

    for rel, snapshot in STALE_SNAPSHOTS.items():
        assert snapshot.get("captured"), f"{rel} 缺 captured"
        assert snapshot.get("reason"), f"{rel} 缺 reason"
        assert len(str(snapshot["reason"])) >= 20, f"{rel} 的 reason 太短，写清为什么当快照"


if __name__ == "__main__":
    for relative in _office_files():
        print(json.dumps({"file": relative, "problems": _scan(relative)}, ensure_ascii=False, indent=2))
