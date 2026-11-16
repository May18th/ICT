"""仓库布局与关键资产守卫：clone 下来之后，底图和产物必须还在。

为什么需要
----------
底图数据曾经放在 `out/`（已在 `.gitignore` 内），本地一切正常，**clone 之后全丢**：
`/map2d` 从 760KB 掉到 157KB、`/data` 从 148KB 掉到 14KB，而**页面照样返回 200**，
没有任何测试变红。这类故障的共同特征是「静默降级」——不抛异常、不报 5xx，
只是页面变空、数字变少，靠人眼看才发现。

所以这里只守两件事：
1. **该入库的资产在不在**（在 `samples/assets/` 里而不是被 ignore 的 `out/` 里）；
2. **体积还在不在同一个数量级**（丢了/生成器产出空壳，量级会掉一个档）。

刻意收窄的边界
--------------
- **不绑定精确字节数**。下限一律取实测值的约一半（大文件）或「非空」阈值（小文件），
  只承担「资产没丢」这一个职责。资产内容正常更新、字节数小幅变化，都不该让测试红。
- `dist/` 不入库，干净副本里不存在，所以**产物检查只在文件存在时生效**，缺失不算失败。
- 需要 `.git` 的检查（是否被 ignore）在没有 `.git` 的干净副本里自动跳过——
  那种环境下「文件存在」本身已经证明它被分发了。

权威口径
--------
体积下限表在这里写一次，改表即改守卫。真实体积可用 `ls -l` 复核。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

OSM_DIR = ROOT / "samples" / "assets" / "osm"

#: OSM 底图必需文件 -> 体积下限（字节）。
#: 下限约为实测值的 1/2，取整到「万」量级；作用是拦住「文件丢了」和「被空壳替换」，
#: 不是锁定内容版本。实测值见括号。
OSM_REQUIRED: dict[str, int] = {
    "base_areas.json": 40_000,  # 实测 82.5 KB：面要素（片区/水体）
    "base_buildings_full.json": 200_000,  # 实测 445 KB：建筑轮廓全量
    "base_roads.json": 35_000,  # 实测 74.8 KB：路网
    "chengdu_bbox_raw.json": 400_000,  # 实测 916 KB：原始 bbox 抓取结果
    "osm_chengdu_centre.json": 700_000,  # 实测 1463 KB：市中心提取结果，/map2d 的主底图
    "road_alias.json": 200,  # 实测 1.4 KB：路名别名表，只拦「被清空」
}

#: 入库的生成产物 -> 体积下限。这些是 `reproduce.py` 里各生成器的直接产物，
#: 一旦生成逻辑坏了产出空壳，量级会立刻掉档。
ARTIFACT_REQUIRED: dict[str, int] = {
    "samples/assets/urban/manifest.json": 10_000,  # 实测 26.6 KB
    "samples/assets/urban/scene.json": 30_000,  # 实测 81.1 KB
    "samples/assets/urban/scene_layout.json": 25_000,  # 实测 61.7 KB
    "samples/assets/urban/semantic_entities.json": 15_000,  # 实测 38.4 KB
    "samples/replay/core_run.json": 8_000,  # 实测 20.4 KB
    "samples/eval/latest.json": 5_000,  # 实测 14.9 KB
}

#: `/map2d` 单文件页面。这是唯一有「事故实测线」的产物：
#: 正常 770–820 KB；掉到 157 KB 说明底图没进构建。缺失（干净副本）不算失败。
MAP2D_PAGE = ROOT / "dist" / "map2d" / "index.html"
MAP2D_MIN_BYTES = 600_000


def _git_available() -> bool:
    return (ROOT / ".git").exists() and shutil.which("git") is not None


def test_osm_assets_present_with_plausible_size() -> None:
    """OSM 底图必须在 `samples/assets/osm/` 下，且体积在同一数量级。"""

    problems: list[str] = []
    for name, floor in OSM_REQUIRED.items():
        path = OSM_DIR / name
        if not path.is_file():
            problems.append(f"缺失：samples/assets/osm/{name}")
            continue
        size = path.stat().st_size
        if size < floor:
            problems.append(
                f"samples/assets/osm/{name} 只有 {size} 字节（下限 {floor}）："
                f"文件在但内容只剩空壳"
            )
    assert not problems, (
        "OSM 底图资产不完整。底图曾在 out/ 下被 .gitignore 忽略，导致 clone 后"
        "页面静默降级，务必放在 samples/assets/osm/：\n" + "\n".join(problems)
    )


def test_osm_assets_are_not_gitignored() -> None:
    """入库资产不能被 ignore 规则命中——这是「本地正常、clone 全丢」的根因。

    需要 `.git` 才能判定；干净副本（如 `git archive` 导出）里没有 `.git`，
    此时跳过：文件能存在就已经证明它被分发了。
    """

    if not _git_available():
        pytest.skip("无 .git（干净副本），跳过 ignore 规则检查")

    paths = [f"samples/assets/osm/{name}" for name in OSM_REQUIRED]
    result = subprocess.run(
        ["git", "check-ignore", "--", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # 退出码 0 = 至少一个被忽略；1 = 都没被忽略；其它 = git 自身出错
    assert result.returncode in (0, 1), f"git check-ignore 执行失败：{result.stderr.strip()}"
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "这些底图资产被 .gitignore 命中了，clone 之后会丢：\n" + "\n".join(ignored)
    )


def test_generated_artifacts_are_not_empty_shells() -> None:
    """入库的生成产物不能变成空壳（生成器坏掉时体积会掉一个档）。"""

    problems: list[str] = []
    for rel, floor in ARTIFACT_REQUIRED.items():
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"缺失：{rel}（重跑 scripts/reproduce.py 生成）")
            continue
        size = path.stat().st_size
        if size < floor:
            problems.append(f"{rel} 只有 {size} 字节（下限 {floor}），疑似产出空壳")
    assert not problems, "生成产物不完整：\n" + "\n".join(problems)


def test_map2d_page_keeps_basemap_when_built() -> None:
    """`dist/map2d/index.html` 存在时，体积必须远超「底图丢失」的事故线。

    `dist/` 不入库，干净副本里没有这个文件，因此缺失时跳过而不是失败。
    """

    if not MAP2D_PAGE.is_file():
        pytest.skip("dist/map2d/index.html 未构建（该目录不入库），跳过")

    size = MAP2D_PAGE.stat().st_size
    assert size >= MAP2D_MIN_BYTES, (
        f"dist/map2d/index.html 只有 {size} 字节（下限 {MAP2D_MIN_BYTES}）。"
        "正常 770–820 KB；掉到 157 KB 量级说明底图没进构建，"
        "检查 samples/assets/osm/ 是否还在。"
    )


def test_repository_layout_keeps_sources_out_of_output_dirs() -> None:
    """源码与真源不能住在输出目录里——`out/`、`dist/`、`artifacts/` 都不入库。

    只做「指向性」检查：真源路径必须落在 `samples/`、`src/`、`scripts/`、`docs/`
    这些入库目录下。这条拦的是「新写了生成器，把真源顺手放进 out/」。
    """

    truth_dirs = ("samples/", "src/", "scripts/", "docs/", "tests/", "deploy/")
    output_dirs = ("out/", "dist/", "artifacts/")

    # 用 as_posix 比较：Windows 上 relative_to 给的是反斜杠，直接 startswith("samples/") 会假失败
    osm_rel = OSM_DIR.relative_to(ROOT).as_posix()
    assert any(osm_rel.startswith(d) for d in truth_dirs), (
        f"底图真源必须位于入库目录，实际是 {osm_rel}"
    )
    for rel in ARTIFACT_REQUIRED:
        assert not any(rel.startswith(d) for d in output_dirs), (
            f"{rel} 位于输出目录，clone 后会丢；生成产物要落到 samples/ 下"
        )
