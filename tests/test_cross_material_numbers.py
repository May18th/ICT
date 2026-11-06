"""跨材料数字同源守卫（视频分镜 / PPT 生成脚本 / 价值文档 / 页面）。

为什么要单独一组
----------------
已有两道守卫各守一段：`evidence_check` 守仓库里 108 项对外数字，`test_office_deliverables`
守已生成的 `.pptx`/`.docx` 容器。**还没生成成片的材料**（3 分钟分镜、PPT 生成脚本、
价值文档）一直是无人区——它们最容易被手改成"更好听"的数字，然后在录屏那天跟代码对不上。

三个关键数字的权威源都在代码里，不在文档里：

- **缺口 2 台** → `deduction.resource_gaps`（本次推演）
- **3040 m** → `road_graph.impact("R-101")`（与 CF1 同一套最短路计算）
- **40 分钟** → `resource_inventory.csv` 里跨区调拨的备注原文

所以这里不写死数字，先算出来再拿去比对——数量变了，测试自动跟着变。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from decision_poc.road_graph import RoadGraph
from decision_poc.web_demo import build_demo_payload, build_html

ROOT = Path(__file__).resolve().parents[1]

STORYBOARD = ROOT / "docs" / "competition" / "video-storyboard-3min.md"
VALUE_DOC = ROOT / "docs" / "competition" / "value-evidence.md"
PPT_SCRIPT = ROOT / "scripts" / "build_answer_ppt.py"
INVENTORY = ROOT / "samples" / "assets" / "urban" / "resource_inventory.csv"


@pytest.fixture(scope="module")
def payload():
    return build_demo_payload()


@pytest.fixture(scope="module")
def travel():
    return RoadGraph.load().impact("R-101")


def _text(path: Path) -> str:
    assert path.is_file(), f"材料缺失：{path}"
    return path.read_text(encoding="utf-8")


def test_gap_number_is_identical_in_every_material(payload):
    gaps = payload["deduction"]["resource_gaps"]
    gap = sum(int(item["gap"]) for item in gaps)
    needle = f"缺口 {gap}"
    for path in (STORYBOARD, VALUE_DOC, PPT_SCRIPT):
        assert needle in _text(path), f"{path.name} 里的缺口数字与推演不一致（应为 {needle}）"


def test_3040_comes_from_the_road_graph(travel):
    # 先确认这个数确实算得出来，再去比对材料——顺序反了就成了"文档说 3040 所以是 3040"
    assert travel.delta_meters == 3040
    assert travel.baseline.meters == 820
    assert travel.after_closure.meters == 3860
    for path in (STORYBOARD, VALUE_DOC):
        assert "3040" in _text(path), f"{path.name} 里的绕行代价与路网计算不一致"


def test_forty_minutes_comes_from_the_ledger():
    text = _text(INVENTORY)
    assert "跨区调用需 40 分钟" in text, "40 分钟的出处必须是台账备注，不能是文案约定"
    for path in (STORYBOARD, VALUE_DOC):
        assert "40 分钟" in _text(path), f"{path.name} 少了 40 分钟这个锚点"


def test_page_does_not_hardcode_any_of_them():
    """页面上这些数字全部来自 /api/scheduler，写死就等于给自己埋一个漂移源。"""
    html = build_html()
    for needle in ("3040", "缺口 2", "40 分钟", "可用 6"):
        assert needle not in html, f"页面硬编码了「{needle}」，应改为从接口取"
