# -*- coding: utf-8 -*-
"""用 python-pptx 生成初赛答辩 PPT（10 页），内容严格取自 docs/competition/ppt-outline-10p.md。"""
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "out" / "ppt-assets"
OUT = ROOT / "docs" / "competition" / "初赛答辩-PPT-v0.1.pptx"

# --- 主题 ---------------------------------------------------------------
BG = RGBColor(0x0F, 0x1B, 0x2D)
PANEL = RGBColor(0x17, 0x27, 0x3F)
ACCENT = RGBColor(0xFF, 0x7A, 0x3D)
TEAL = RGBColor(0x2D, 0xD4, 0xBF)
TEXT = RGBColor(0xF2, 0xF5, 0xFA)
MUTED = RGBColor(0x8F, 0xA3, 0xBF)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CN = "微软雅黑"
MONO = "Consolas"

W, H = Inches(13.333), Inches(7.5)
prs = Presentation()
prs.slide_width, prs.slide_height = W, H
BLANK = prs.slide_layouts[6]


def _rect(slide, x, y, w, h, fill, line=None, radius=False):
    shp = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, x, y, w, h
    )
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(1)
    shp.shadow.inherit = False
    return shp


def _rect_alpha(slide, x, y, w, h, fill, alpha):
    """半透明矩形（alpha 0-1 为不透明度）：用于图片上压暗，保证文字可读。"""
    from lxml import etree
    from pptx.oxml.ns import qn

    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    shp.line.fill.background()
    shp.shadow.inherit = False
    solid = shp.fill._xPr.find(qn("a:solidFill"))
    srgb = solid.find(qn("a:srgbClr"))
    alpha_el = etree.SubElement(srgb, qn("a:alpha"))
    alpha_el.set("val", str(int(alpha * 100000)))
    return shp


def _text(slide, x, y, w, h, s, size=18, color=TEXT, bold=False, font=CN,
          align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.0, italic=False):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = spacing
    r = p.add_run()
    r.text = s
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    r.font.color.rgb = color
    r.font.name = font
    return tb


def _bullets(slide, x, y, w, h, items, size=18, color=TEXT, gap=10):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        p.line_spacing = 1.15
        r = p.add_run()
        r.text = "· " + item
        r.font.size = Pt(size)
        r.font.color.rgb = color
        r.font.name = CN
    return tb


def _fit_image(slide, path, box_x, box_y, box_w, box_h, align="center"):
    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(box_w / iw, box_h / ih)
    w, h = int(iw * scale), int(ih * scale)
    x = box_x + (box_w - w) // 2
    y = box_y + (box_h - h) // 2
    return slide.shapes.add_picture(str(path), x, y, width=w, height=h)


def _cover_image(slide, path):
    return slide.shapes.add_picture(str(path), 0, 0, width=W, height=H)


def _notes(slide, remember, evidence):
    tf = slide.notes_slide.notes_text_frame
    tf.text = f"【记住】{remember}\n【证据】{evidence}"


def _footer(slide, page, evidence):
    _text(slide, Inches(0.62), Inches(7.02), Inches(9.8), Inches(0.32),
          evidence, size=10, color=MUTED, font=MONO)
    _text(slide, Inches(12.05), Inches(7.02), Inches(0.7), Inches(0.32),
          f"{page}/10", size=10, color=MUTED, font=MONO, align=PP_ALIGN.RIGHT)


def _header(slide, kicker, title, accent=ACCENT):
    _rect(slide, 0, 0, Inches(0.09), H, accent)
    _text(slide, Inches(0.62), Inches(0.55), Inches(11.8), Inches(0.34), kicker,
          size=12, color=accent, font=MONO)
    _text(slide, Inches(0.62), Inches(0.92), Inches(12.1), Inches(0.9), title,
          size=30, color=TEXT, bold=True)


# ======================= P1 封面 =======================
s = prs.slides.add_slide(BLANK)
_cover_image(s, ASSETS / "cover.png")
_rect_alpha(s, 0, 0, W, H, BG, 0.58)
_rect(s, 0, 0, Inches(0.12), H, ACCENT)
_text(s, Inches(0.9), Inches(1.05), Inches(11.6), Inches(0.4),
      "赛题 3 · 基于 ModelEngine Nexent 的可进化决策智能体", size=15, color=ACCENT, font=MONO)
_text(s, Inches(0.9), Inches(1.55), Inches(11.6), Inches(1.5),
      "一次暴雨，一条可追溯、可回滚的决策链", size=46, color=TEXT, bold=True, spacing=1.05)
_text(s, Inches(0.9), Inches(3.0), Inches(11.6), Inches(0.4),
      "2026 华为 ICT 大赛 · 创新赛 · 队伍名 [待填]", size=17, color=MUTED)
_bullets(s, Inches(0.9), Inches(3.95), Inches(11.2), Inches(2.2), [
    "事件进 → 六跳推演 → 部门动作，一条链",
    "每条结论带 asset_id#locator，可回到原文行号",
    "冲突不许被「综合判断」抹平",
    "反馈过门禁才改策略，改坏可回滚",
    "平台真机 8/8 · 776 测试 · 106 项自检",
], size=17, gap=8)
_text(s, Inches(0.9), Inches(6.72), Inches(11.6), Inches(0.3),
      "背景为离线渲染画面，非实时引擎", size=11, color=MUTED, font=MONO)
_notes(s, "一条链，不是一堆技术；评委先记人再记字。",
       "python -m decision_poc.evidence_check（106/106）")

# ======================= P2 问题与场景 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_cover_image(s, ASSETS / "p2.png")
_rect_alpha(s, 0, 0, Inches(7.4), H, BG, 0.93)
_header(s, "P2 · 问题与场景（0:20–1:00）", "一条路，三个部门，三种结论")
_bullets(s, Inches(0.62), Inches(2.0), Inches(6.1), Inches(4.3), [
    "结论不可追溯：一句「建议先封路」背后是三份预案、两张台账",
    "冲突被抹平：职责空白（泵站无运维主体）、权限交叉（道路/交通都能封路）在汇报里消失",
    "决策不进化：同类事件每次都从头推一遍，上次的教训没沉淀",
    "涉及部门：排水 / 道路 / 交通 / 燃气 / 供电，跨 5 个责任主体",
    "我们只做一件事：把这条链做成可复核、可回滚",
], size=15, gap=12)
_footer(s, 2, "docs/reports/p6-deduction-report.md（C1/C2/C3 各一段）")
_notes(s, "暴雨内涝是跨部门的事，冲突不能靠「综合判断」抹平。",
       "docs/reports/p6-deduction-report.md")

# ======================= P3 五个考点 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P3 · 五个考点对位（1:00–1:50）", "五个考点，五张能当场跑的证据")
rows = [
    ("① 多模态数据资产认知", "16 资产统一登记（asset_id+sha256+locator+版本+有效期）；解析 16/16、未识别列 0", "强（图像只登记，不做 OCR）"),
    ("② 低资源本体半自动构建", "种子本体→规则抽取→置信度分档→人工确认→版本发布/回滚；改 1 格只产 2 条候选", "强"),
    ("③ MCP / Skills 编排", "14 个 MCP 工具 + 5 Skill 编排 + 2 个已上传 SKILL.md；平台 Agent 实调工具", "强"),
    ("④ 跨文档多跳推理与溯源", "六跳 + 三类冲突 + 反事实重放 + 哈希链账本；引用完整率 100%", "强"),
    ("⑤ 基于 Nexent 可运行", "真机 14 工具注册、Agent 发布、六跳结论 8/8 一致（单环境单事件）", "强"),
]
tbl = s.shapes.add_table(len(rows) + 1, 3, Inches(0.62), Inches(1.95), Inches(12.1), Inches(4.3)).table
tbl.columns[0].width = Inches(2.7)
tbl.columns[1].width = Inches(7.4)
tbl.columns[2].width = Inches(2.0)
hdr = ["考点", "我们的回答", "证据强度"]
for j, h in enumerate(hdr):
    c = tbl.cell(0, j)
    c.text = ""
    p = c.text_frame.paragraphs[0]
    r = p.add_run(); r.text = h
    r.font.size = Pt(13); r.font.bold = True; r.font.color.rgb = BG; r.font.name = CN
    c.fill.solid(); c.fill.fore_color.rgb = ACCENT
for i, (a, b, c3) in enumerate(rows, start=1):
    for j, val in enumerate((a, b, c3)):
        c = tbl.cell(i, j)
        c.text = ""
        p = c.text_frame.paragraphs[0]
        r = p.add_run(); r.text = val
        r.font.size = Pt(11); r.font.color.rgb = TEXT; r.font.name = CN
        c.fill.solid(); c.fill.fore_color.rgb = PANEL if i % 2 else BG
        c.margin_left = c.margin_right = Inches(0.06)
_footer(s, 3, "data-gate / p3-evolution / p5-mcp / p4-graph-provenance / nexent-live-integration 五份报告")
_notes(s, "五个考点各留一张能当场跑的证据：前四个离线，第五个平台真机。",
       "docs/reports/ 下五份报告 + 每条命令可在 README 找到")

# ======================= P4 数据资产 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P4 · 事件与数据资产（1:50–2:25）", "数据是合成的，但每一条都标了出处")
_bullets(s, Inches(0.62), Inches(2.0), Inches(7.0), Inches(4.3), [
    "16 个资产：表格 9 / 文档 6 / 示意图 1；另有 2 个真实公开预案",
    "统一字段：asset_id（内容寻址）+ sha256 + locator + 版本 + 有效期 + 可见性",
    "处理链路：编码回退解析 → 表格标准化 → 切片 → 实体关系候选 → 人工确认",
    "实测：解析 16/16、未识别列 0、检索命中 100%",
    "边界：图像只登记尺寸与区域，不做 OCR",
], size=15, gap=12)
_rect(s, Inches(8.0), Inches(2.0), Inches(4.7), Inches(4.0), PANEL, radius=True)
_text(s, Inches(8.3), Inches(2.3), Inches(4.1), Inches(0.4), "locator 形态", size=13, color=TEAL, font=MONO)
_bullets(s, Inches(8.3), Inches(2.85), Inches(4.1), Inches(2.8), [
    "表格行：#row:n",
    "文档行：#line:a-b",
    "示意图：#region:x,y,w,h",
], size=14, color=TEXT, gap=12)
_footer(s, 4, "docs/reports/data-gate-report.md（来源分级表）")
_notes(s, "合成不藏，但每份登记来源/版本/有效期，结论能回到原文。",
       "docs/reports/data-gate-report.md + samples/assets/urban/resource_inventory.csv")

# ======================= P5 六跳与证据链 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P5 · 六跳推演与证据链（2:25–3:15）", "六跳固定，冲突一个都不许少")
_bullets(s, Inches(0.62), Inches(2.0), Inches(6.5), Inches(4.4), [
    "六跳固定：affected → owner → conflict → resource → action → residual_risk",
    "三类冲突全命中：C1 权限交叉 / C2 职责空白 / C3 资源争用，全部保留",
    "资源结论：库存 6 台、缺口 2 台（口径 v1，见脚注）",
    "反事实只给代价：先封路后隔离，多 3040 m / 慢 5.1 min",
    "溯源：引用完整率 100%，账本被篡改可定位首个断点",
], size=15, gap=12)
_fit_image(s, ROOT / "docs/reports/captures/S31-ue-highlight-step0.png",
           Inches(7.3), Inches(1.95), Inches(5.5), Inches(3.7))
_text(s, Inches(7.3), Inches(5.75), Inches(5.5), Inches(0.5),
      "回放高亮：结论 → 场景对象（离线渲染，非实时引擎）", size=10, color=MUTED)
_footer(s, 5, "python -m decision_poc.deduction --event WO-20261029-007 --as-of …")
_notes(s, "六跳固定、冲突必须全部露出来；反事实只给代价不给建议。",
       "python -m decision_poc.deduction / counterfactual --case CF1")

# ======================= P6 平台真机 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P6 · Nexent 平台真机（3:15–4:05）", "不是本地脚本装智能体，是平台上真调工具")
_bullets(s, Inches(0.62), Inches(2.0), Inches(5.6), Inches(4.3), [
    "MCP 服务 urban-twin-mcp 以 SSE 注册进平台，14 个工具",
    "Agent urban_deduction_agent（openPangu-2.0-Pro）已发布",
    "真机对话六跳结论与离线推演 8/8 一致，快照哈希一致",
    "平台侧调用日志可查：event_deduce 实际调用",
    "2 个 SKILL.md 已上传平台技能仓库（skill_id=2/3）",
], size=14, gap=11)
cap = ROOT / "docs/competition/video-clips"
shots = [
    (ROOT / "docs/reports/captures/nexent/mcp-space.png", "MCP：连通性校验成功 + 14 工具"),
    (ROOT / "docs/reports/captures/nexent/agents.png", "Agent：已发布"),
    (ROOT / "docs/reports/captures/nexent/skill-space.png", "Skill：2 个已上传"),
]
x0 = Inches(6.6)
for i, (p, label) in enumerate(shots):
    bx = x0 + Inches((i % 2) * 3.15)
    by = Inches(1.95 + (i // 2) * 2.35)
    _rect(s, bx, by, Inches(3.0), Inches(2.15), PANEL, radius=True)
    _fit_image(s, p, bx + Inches(0.08), by + Inches(0.08), Inches(2.84), Inches(1.62))
    _text(s, bx + Inches(0.08), by + Inches(1.76), Inches(2.84), Inches(0.3), label, size=9, color=MUTED)
_footer(s, 6, "docs/reports/captures/nexent/platform_ui.json（截图已断言）")
_notes(s, "平台真机跑出来的 8/8，不是本地脚本；不要写成「多智能体已上平台」。",
       "samples/nexent/live/conversation_78.json + captures/nexent/")

# ======================= P7 可进化与回滚 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P7 · 可进化与回滚（4:05–4:55）", "变好要过门禁，变坏要能退回")
_bullets(s, Inches(0.62), Inches(2.0), Inches(7.0), Inches(4.4), [
    "决策对象：跨片区抽排资源分配（推演只答「缺多少台」，不答「先给谁」）",
    "轮次 3：采纳 2 / 拒绝 1，第 3 轮候选被门禁挡下，active 版本不变",
    "回滚：退回上一版本后偏差逐位还原，不是近似值",
    "记忆 warm start：第二会话起点偏差 0.0000、轮次 3→1",
    "版本标签：flood-response-v1.0 → v1.1 → v1.2",
], size=14, gap=11)
# 数字卡
cards = [
    ("训练偏差", "0.2264 → 0.0000"),
    ("留出偏差", "0.2182 → 0.0000"),
    ("A/B 全序列", "0.2237 → 0.0000"),
]
for i, (k, v) in enumerate(cards):
    _rect(s, Inches(8.0), Inches(2.0 + i * 1.25), Inches(4.7), Inches(1.05), PANEL, radius=True)
    _text(s, Inches(8.3), Inches(2.16 + i * 1.25), Inches(4.1), Inches(0.3), k, size=11, color=TEAL, font=MONO)
    _text(s, Inches(8.3), Inches(2.5 + i * 1.25), Inches(4.1), Inches(0.4), v, size=20, color=ACCENT, bold=True, font=MONO)
_text(s, Inches(8.0), Inches(5.9), Inches(4.7), Inches(0.5),
      "训练/留出与 A/B 全序列是两套口径，同页出现须加括注", size=9, color=MUTED)
_footer(s, 7, "python scripts/evolution_ab.py（A/B + 哈希 + 拒绝/回滚）")
_notes(s, "进化不是越跑越好：候选先过门禁，回滚逐位回到 0.2264。",
       "python scripts/evolution_ab.py + docs/reports/p9-policy-evolution-report.md")

# ======================= P8 LLM 与漂移 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P8 · LLM 决策与漂移（4:55–5:35）", "我们没把裁决权交给大模型")
_bullets(s, Inches(0.62), Inches(2.0), Inches(7.2), Inches(4.3), [
    "边界设计：LLM 负责理解与表达，优先级与冲突裁决归确定性推演",
    "平台侧温度钉不住：N01（参照 opt-3）多次调用均未命中",
    "入仓 4 条原始记录，全部 selected_match:false、schema_valid:true",
    "直调端点对照：/api/llm-decision（t=0）→ 命中 opt-3，约 4.1 s",
    "失败降级：未配置密钥返回 configured=false，页面显示「未配置」",
], size=14, gap=11)
rec = [("llm_decision_platform.json", "opt-1", "✗ 未命中"),
       ("…_run1.json", "opt-2", "✗ 未命中"),
       ("…_run2.json", "opt-2", "✗ 未命中"),
       ("…_run3.json", "opt-2", "✗ 未命中")]
tbl = s.shapes.add_table(5, 3, Inches(8.1), Inches(2.0), Inches(4.6), Inches(2.6)).table
tbl.columns[0].width = Inches(2.5); tbl.columns[1].width = Inches(1.1); tbl.columns[2].width = Inches(1.0)
for j, h in enumerate(["原始记录", "选中", "判定"]):
    c = tbl.cell(0, j); c.text = ""
    r = c.text_frame.paragraphs[0].add_run(); r.text = h
    r.font.size = Pt(10); r.font.bold = True; r.font.color.rgb = BG; r.font.name = CN
    c.fill.solid(); c.fill.fore_color.rgb = ACCENT
for i, (f, o, m) in enumerate(rec, start=1):
    for j, val in enumerate((f, o, m)):
        c = tbl.cell(i, j); c.text = ""
        r = c.text_frame.paragraphs[0].add_run(); r.text = val
        r.font.size = Pt(9); r.font.color.rgb = TEXT; r.font.name = MONO if j == 0 else CN
        c.fill.solid(); c.fill.fore_color.rgb = PANEL if i % 2 else BG
        c.margin_left = c.margin_right = Inches(0.04)
_footer(s, 8, "samples/nexent/live/llm_decision_platform*.json（4 条）")
_notes(s, "模型会漂移、我们如实展示；这正是裁决权不能交给模型的证据。",
       "samples/nexent/live/llm_decision_platform.json + run1/2/3.json")

# ======================= P9 业务价值 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P9 · 业务价值量化（5:35–6:15）", "三个能当场复算的数，一个不敢给的百分比")
big = [("40 分钟", "跨区调拨时间 = 缺口最多可提前暴露的上限", TEAL),
       ("3040 m / 5.1 min", "处置顺序错时的绕行代价（含 R-103 占道降速）", ACCENT),
       ("缺口 2 台", "跳 5 输出，取舍按阈值与危险源排序", TEAL)]
for i, (num, desc, col) in enumerate(big):
    bx = Inches(0.62 + i * 4.15)
    _rect(s, bx, Inches(2.05), Inches(3.9), Inches(2.6), PANEL, radius=True)
    _text(s, bx + Inches(0.3), Inches(2.35), Inches(3.3), Inches(0.7), num, size=30, color=col, bold=True, font=MONO)
    _text(s, bx + Inches(0.3), Inches(3.25), Inches(3.3), Inches(1.1), desc, size=12, color=TEXT)
_bullets(s, Inches(0.62), Inches(5.0), Inches(12.1), Inches(1.6), [
    "不做的换算：没有交通流量数据，不乘「每天多少车次」，乘出来就是编的",
    "人工协同 vs 智能体辅助的 3 人对照实验尚未做，本节一律标「可复算中间量」，不给人力节省百分比",
], size=13, gap=8)
_footer(s, 9, "python -m decision_poc.counterfactual --case CF1")
_notes(s, "三个可复算中间量 + 一个不敢给的百分比；省多少人力没测就不说。",
       "counterfactual --case CF1 + docs/competition/value-evidence.md §3")

# ======================= P10 边界与复现 =======================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, W, H, BG)
_header(s, "P10 · 边界、可复现与后续（6:15–6:50）", "我们把边界写在页面上")
_bullets(s, Inches(0.62), Inches(2.0), Inches(7.2), Inches(4.5), [
    "边界：数据合成；专家反馈是角色扮演；真实用户评分样本 0；UE 未实机验收",
    "定量层未接管六跳缺口口径（仍有 80% 折算，即 v1 口径）",
    "可视化口径：3D 是「设计中有 + Blender 侧静帧/短片」，不是实时引擎",
    "可复现四条命令：pytest(776) / evidence_check(106/106) / urban_eval(22/22) / nexent_urban(8/8)",
    "后续：第二场真实公开事件 → 真实用户评分 3–5 人 → 协同层接 MCP/平台（设计中）",
], size=14, gap=11)
_fit_image(s, ROOT / "docs/reports/captures/S33-binding-v2.png",
           Inches(8.1), Inches(2.0), Inches(4.6), Inches(3.4))
_text(s, Inches(8.1), Inches(5.5), Inches(4.6), Inches(0.5),
      "决策→场景实体绑定：19 个对象里 16 个可画、3 个显式记 unrepresented", size=9, color=MUTED)
_footer(s, 10, "docs/reports/evidence-check-report.md（106 项逐条表）")
_notes(s, "边界主动写出来、四条命令谁都能当场跑；后续计划不写成已做。",
       "python -m decision_poc.evidence_check + docs/reports/evidence-check-report.md")

OUT.parent.mkdir(parents=True, exist_ok=True)
prs.save(OUT)
print("SAVED", OUT, "slides=", len(prs.slides))
