"""演示页 HTML 结构守卫：每个浏览器都会把未闭合的 <style> 之后的内容当 CSS 文本吞掉。

背景（2026-11-07 实测）：`dddb16b` 加的 `<style>#flood-scene{...}` 漏了闭合标签，
`<main>` 到「动态内涝推演」整段被浏览器当作样式表内容，主页对所有浏览器呈现空白。
HTTP 层交互测试（16/16）全绿也发现不了，只有真渲染才看得见。本文件把"能被浏览器渲染"
降级成可在 CI 里断言的结构不变量。
"""

from __future__ import annotations

import re

from decision_poc.web_demo import build_html


def test_style_tags_balanced() -> None:
    html = build_html()
    opens = html.count("<style>")
    closes = html.count("</style>")
    assert opens == closes, f"<style> {opens} 个但 </style> {closes} 个：未闭合会吞掉后续内容"


def test_no_nested_style_block() -> None:
    """两个 <style> 之间必须存在 </style>——否则后一个整块变成非法 CSS 文本。"""
    positions = [(m.start(), m.group()) for m in re.finditer(r"</?style", build_html())]
    depth = 0
    for offset, tag in positions:
        if tag == "<style":
            depth += 1
            assert depth == 1, f"offset {offset} 出现嵌套 <style>：前一个块未闭合"
        else:
            depth -= 1
            assert depth == 0, f"offset {offset} 出现多余 </style>"

    assert depth == 0, "结尾仍有未闭合的 <style>"


def test_style_blocks_contain_no_markup() -> None:
    """任何 <style> 块内都不许出现正文标签——出现了就说明有块未闭合、把正文吞成了 CSS。"""
    html = build_html()
    blocks = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert blocks, "未找到样式块"
    for block in blocks:
        for tag in ("<main", "<section", "<div", "<h1", "<header", "<details"):
            assert tag not in block, f"样式块内出现正文标签 {tag}：存在未闭合的 <style>"


def test_scenario_controls_exist() -> None:
    """情景切换与前端口径徽章的挂载点必须存在。"""
    html = build_html()
    assert "switchScenario('real_20260801')" in html
    assert 'id="quant-refs"' in html or "id=quant-refs" in html
    assert "playRealRain()" in html
