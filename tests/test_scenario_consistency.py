"""场景目录与基础数据的一致性守卫：改基础数据忘了同步场景，这里必须红。

为什么需要
----------
`samples/scenarios/real_20260801/` 目前是 `samples/assets/urban/` 的**全量复制**，
只换掉了 `weather_alerts.csv`（把橙色预警行换成真实暴雨映射行）。

这种「复制型场景」最大的风险不是占空间（334 KB），而是**改一边忘另一边**：
在 `urban/` 里改了台账、补了字段、调了管径，场景目录还是旧的；跑演示时看起来一切正常，
直到某一步推演的数字与台账对不上。没有任何测试会红——两边各自都「自洽」。

所以这里把「除覆盖文件外逐字节一致」钉成断言。**不重构目录结构**（不引入 overlay），
只在现有布局上加一道守卫：真要改成 overlay 时，这个测试就是它的验收条件。

豁免清单只有两个，都带理由
--------------------------
- `weather_alerts.csv`：场景存在的意义就是替换它；
- `README.md`：基础数据的是资产清单，场景的是情景说明，本来就不该相同。

行尾差异单独提示
----------------
按字节比较，但失败信息区分「仅行尾不同」和「内容不同」。
仓库 `.gitattributes` 已把文本钉成 `eol=lf`；若某台机器检出成 CRLF，
会得到「仅行尾不同」的明确提示，而不是一句看不懂的哈希不一致。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE = ROOT / "samples" / "assets" / "urban"
SCENARIO = ROOT / "samples" / "scenarios" / "real_20260801"

#: 允许与基础数据不同的文件 -> 理由。
SCENARIO_OVERRIDES: dict[str, str] = {
    "weather_alerts.csv": "场景覆盖文件：橙色预警行换成真实暴雨映射行",
    "README.md": "场景自带的情景说明，与基础数据的资产清单本来不同",
}


def _files(root: Path) -> dict[str, Path]:
    return {p.name: p for p in root.iterdir() if p.is_file()}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _only_line_endings_differ(left: Path, right: Path) -> bool:
    """字节不等时判断是否只是行尾差异。"""

    def normalized(path: Path) -> bytes:
        return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    return normalized(left) == normalized(right)


def test_scenario_covers_same_files_as_base() -> None:
    """场景目录的文件集合必须与基础数据完全相同（不多不少）。

    多出来 = 有人往场景里塞了不该有的东西；少了 = 复制时漏了文件。
    两者都会让「场景是基础数据 + 一处覆盖」这个前提不成立。
    """

    base = set(_files(BASE))
    scenario = set(_files(SCENARIO))

    missing = sorted(base - scenario)
    extra = sorted(scenario - base)

    problems: list[str] = []
    if missing:
        problems.append(f"场景缺少这些文件：{missing}")
    if extra:
        problems.append(f"场景多出这些文件（基础数据里没有）：{extra}")
    assert not problems, (
        "real_20260801 与 samples/assets/urban 的文件集合不一致。"
        "重跑 `python scripts/build_real_scenario_dataset.py` 可以重建：\n" + "\n".join(problems)
    )


def test_scenario_files_match_base_except_overrides() -> None:
    """除豁免文件外，场景与基础数据必须逐字节一致。"""

    base = _files(BASE)
    scenario = _files(SCENARIO)

    content_diff: list[str] = []
    eol_diff: list[str] = []

    for name in sorted(set(base) & set(scenario)):
        if name in SCENARIO_OVERRIDES:
            continue
        left, right = base[name], scenario[name]
        if _sha256(left) == _sha256(right):
            continue
        if _only_line_endings_differ(left, right):
            eol_diff.append(name)
        else:
            content_diff.append(
                f"{name}：基础数据 {left.stat().st_size} 字节 / 场景 {right.stat().st_size} 字节"
            )

    problems: list[str] = []
    if content_diff:
        problems.append(
            "内容不一致（在 urban 里改了数据但没同步场景）：\n  " + "\n  ".join(content_diff)
        )
    if eol_diff:
        problems.append(
            "仅行尾不同（不是数据问题，是检出时的换行符）：\n  "
            + "\n  ".join(eol_diff)
            + "\n  `.gitattributes` 已声明 `* text=auto eol=lf`；"
              "检查是否被 `core.autocrlf=true` 覆盖，或重跑生成脚本。"
        )

    assert not problems, (
        "real_20260801 与 samples/assets/urban 不再一致。"
        "场景只应覆盖 weather_alerts.csv 与自己的 README.md：\n" + "\n".join(problems)
    )


def test_override_list_only_contains_files_that_actually_differ() -> None:
    """豁免列表要精简：登记了却与基础数据完全相同，说明覆盖已经不需要，应删掉条目。

    反方向（该豁免的没豁免）由上一条测试红着。这条防的是「豁免越挂越多，
    最后整个目录都豁免了，守卫形同虚设」。
    """

    base = _files(BASE)
    scenario = _files(SCENARIO)

    stale = [
        name
        for name in SCENARIO_OVERRIDES
        if name in base and name in scenario and _sha256(base[name]) == _sha256(scenario[name])
    ]
    assert not stale, (
        f"这些文件已在 SCENARIO_OVERRIDES 里登记豁免，但与基础数据完全相同，请删除条目：{stale}"
    )


def test_scenario_weather_override_is_actually_different() -> None:
    """覆盖文件必须真的被改过：如果场景只是原样复制，整个场景就失去意义。"""

    name = "weather_alerts.csv"
    base_file, scenario_file = BASE / name, SCENARIO / name
    assert base_file.is_file() and scenario_file.is_file(), f"{name} 必须同时存在于两处"
    assert _sha256(base_file) != _sha256(scenario_file), (
        f"{name} 与基础数据完全相同：场景应当把橙色预警行替换成真实暴雨映射行，"
        "检查 scripts/build_real_scenario_dataset.py 是否真的执行了替换"
    )
