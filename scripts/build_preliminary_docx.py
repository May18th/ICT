"""生成初赛「作品信息收集表」内容稿（Word）。

定位：官方模板原文尚未落库（工作区无 .docx），本文件是可粘贴的**内容稿**，
不是官方版式；套版时按模板逐节搬运即可。

运行（使用 bundled python，含 python-docx / Pillow）：
    <bundled-python> scripts/build_preliminary_docx.py
产物：
    docs/competition/初赛作品信息收集表-内容稿-v0.1.docx
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]  # agent_poc/
WORKSPACE = ROOT.parent
OUT = ROOT / "docs" / "competition" / "初赛作品信息收集表-内容稿-v0.1.docx"
ASSETS = ROOT / "artifacts" / "docx-assets"

IMAGES = [
    (
        WORKSPACE / "blender-ue-scene" / "out" / "S32-annotated" / "S32-final-main.png",
        "图 1  断面主图：积水 30cm ≥ 封路阈值 25cm，封路 / 基坑 / 抽排在同一断面争资源",
    ),
    (
        WORKSPACE / "blender-ue-scene" / "out" / "S32-annotated" / "S32-job-hop-2-owner.png",
        "图 2  六跳第 2 跳（owner）：18 个高亮对象由 RenderJob 驱动，画面右下角印 snapshot_id 与策略版本",
    ),
    (
        WORKSPACE / "blender-ue-scene" / "out" / "S32-annotated" / "S32-final-pumps.png",
        "图 3  3 台移动泵车 + DF-PS-01 泵站：职责空白 C2 与抽排能力同框",
    ),
    (
        ROOT / "docs" / "reports" / "captures" / "nexent" / "skill-space.png",
        "图 4  平台技能仓库：两个 SKILL.md 已上传到 Nexent（skill_id=2/3，正文逐位一致）",
    ),
    (
        ROOT / "docs" / "reports" / "captures" / "nexent" / "mcp-space.png",
        "图 5  平台 MCP：urban-twin-mcp 连通性校验成功，12 个决策/可视化工具",
    ),
]

FONT_EAST = "微软雅黑"


def set_font(run, size: float | None = None, bold: bool | None = None, color: RGBColor | None = None) -> None:
    run.font.name = FONT_EAST
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_EAST)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def init_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = FONT_EAST
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_EAST)
    for level in (1, 2, 3):
        style = doc.styles[f"Heading {level}"]
        style.font.name = FONT_EAST
        style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_EAST)


def h(doc: Document, text: str, level: int = 1):
    paragraph = doc.add_heading(level=level)
    run = paragraph.add_run(text)
    set_font(run, size={1: 15, 2: 12.5, 3: 11}[level], bold=True, color=RGBColor(0x1F, 0x3B, 0x57))
    return paragraph


def p(doc: Document, text: str, bold_prefix: str | None = None, size: float = 10.5):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(4)
    if bold_prefix:
        run = paragraph.add_run(bold_prefix)
        set_font(run, size=size, bold=True)
    run = paragraph.add_run(text)
    set_font(run, size=size)
    return paragraph


def bullet(doc: Document, text: str):
    paragraph = doc.add_paragraph(style="List Bullet")
    paragraph.paragraph_format.space_after = Pt(2)
    run = paragraph.add_run(text)
    set_font(run, size=10.5)
    return paragraph


def table(doc: Document, header: list[str], rows: list[list[str]], widths: list[float] | None = None):
    tbl = doc.add_table(rows=1, cols=len(header))
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    tbl.allow_autofit = False
    # 固定列宽：必须写 tblLayout=fixed，否则 Word/LibreOffice 会按内容自动撑开导致出界
    tbl_pr = tbl._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        from docx.oxml import OxmlElement

        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    for index, text in enumerate(header):
        cell = tbl.rows[0].cells[index]
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        set_font(run, size=9, bold=True)
    for row in rows:
        cells = tbl.add_row().cells
        for index, text in enumerate(row):
            cells[index].text = ""
            run = cells[index].paragraphs[0].add_run(text)
            set_font(run, size=8.5)
    if widths:
        # 总宽压到 15.5cm（正文宽 17cm 留余量）：LibreOffice/字体替换会把表撑宽，固定布局 + 显式 tblW 才稳
        from docx.oxml import OxmlElement

        factor = 15.5 / sum(widths)
        widths = [round(w * factor, 3) for w in widths]
        for index, width in enumerate(widths):
            tbl.columns[index].width = Cm(width)
        for row in tbl.rows:
            for index, width in enumerate(widths):
                row.cells[index].width = Cm(width)
        total_twips = int(sum(widths) / 2.54 * 1440)
        tbl_w = tbl_pr.find(qn("w:tblW"))
        if tbl_w is None:
            tbl_w = OxmlElement("w:tblW")
            tbl_pr.append(tbl_w)
        tbl_w.set(qn("w:type"), "dxa")
        tbl_w.set(qn("w:w"), str(total_twips))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return tbl


def prepare_image(source: Path, name: str, max_width: int = 1280) -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    target = ASSETS / name
    image = Image.open(source).convert("RGB")
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), Image.LANCZOS)
    image.save(target, "JPEG", quality=88)
    return target


def code_block(doc: Document, lines: list[str]):
    for line in lines:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.left_indent = Cm(0.5)
        run = paragraph.add_run(line)
        run.font.name = "Consolas"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_EAST)
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def build() -> Path:
    doc = Document()
    init_styles(doc)
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)

    # 封面 / 抬头
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("第十一届华为 ICT 大赛 · 创新赛 赛题 3\n作品信息收集表（内容稿 v0.1）")
    set_font(run, size=18, bold=True, color=RGBColor(0x1F, 0x3B, 0x57))

    note = doc.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = note.add_run(
        "赛题：基于 ModelEngine / Nexent 打造可进化决策智能体\n"
        "本文件为内容稿，版式待按官方模板 1 套版；所有数字可由仓库命令复现"
    )
    set_font(run, size=10, color=RGBColor(0x66, 0x66, 0x66))

    table(
        doc,
        ["项", "内容"],
        [
            ["作品名称", "城市内涝跨部门事件推演 · 可进化决策智能体（UrbanTwin Decision Agent）"],
            ["队伍信息", "〔待填：学校 / 队名 / 3 名学生 / 1 名指导教师〕"],
            ["赛题", "第十一届华为 ICT 大赛 创新赛 · 赛题 3（基于 ModelEngine / Nexent 打造可进化决策智能体）"],
            ["提交形态", "初赛模板 1（Word）· 内容按官方五考点组织"],
            ["版本", "内容稿 v0.1 · 2026-10-31"],
        ],
        widths=[3.2, 13.5],
    )

    # 一、作品概述
    h(doc, "一、作品概述")
    p(
        doc,
        "在 Nexent 上搭了一个会从反馈里越推越准的决策智能体：推演可回溯、策略可回滚、经验可沉淀。",
        "一句话定位：",
    )
    p(
        doc,
        "围绕一个具体场景——暴雨内涝下的跨部门协同处置——把「语义理解 → 跨部门推演 → 可信反馈 → 策略进化」"
        "跑成可量化、可复现、可在平台上运行的闭环；技术证据链（六跳 / 溯源 / 进化 / 平台真机）均已落地。",
        "作品说明：",
    )

    h(doc, "1.1 与官方五考点的对应", 2)
    table(
        doc,
        ["官方考点", "本作品的回答", "证据位置"],
        [
            [
                "① 多模态数据资产认知",
                "15 个合成资产（8 表 / 6 文 / 1 图）统一登记：asset_id、sha256、locator、版本、有效期、可见性；解析 15/15，未识别列 0",
                "samples/assets/urban、docs/data-asset-spec.md",
            ],
            [
                "② 低资源 KG 本体半自动构建",
                "种子本体（10 类型 / 11 谓词）→ 规则抽取 → 置信度分档 → 人工确认 → 版本发布/回滚；改 1 格只产 2 条候选，回滚哈希逐位一致",
                "docs/ontology-bootstrap.md、src/decision_poc/ontology.py",
            ],
            [
                "③ MCP / Skills 编排",
                "14 个 MCP 工具（9 决策语义 + 4 可视化 + 1 协同）+ 5 个 Skill 编排 + 绑定表；Nexent 真机上由平台 Agent 实际调用",
                "src/decision_poc/mcp_server.py、samples/nexent/skills.json",
            ],
            [
                "④ 跨文档多跳推理与可溯源",
                "六跳推演（规则 28 条全部带原文行号并在加载时校验）+ append-only 哈希链账本；引用完整率 100%，篡改可定位",
                "src/decision_poc/deduction.py、provenance.py",
            ],
            [
                "⑤ 基于 Nexent 可运行",
                "Nexent v2.7.0 真机：注册 13 工具、发布 urban_deduction_agent（v10）、一次对话 conversation_id=78，六跳结论与离线 8/8 一致",
                "samples/nexent/live/、docs/reports/nexent-live-integration-report.md",
            ],
        ],
        widths=[3.4, 9.0, 4.6],
    )

    h(doc, "1.2 核心创新点：决策策略的受控进化", 2)
    p(
        doc,
        "前三个能力解决「看得懂、推得演、信得过」，赛题真正追问的是「智能体自己从反馈里变更好了没有」。"
        "本作品把稀缺资源（移动泵车）的分配做成一层有版本、可评测、可回滚的策略，并接进受控进化循环：",
    )
    for line in (
        "偏差归因 → 生成候选策略 → 训练集选优 → 评测门禁（留出集不退化 + 不违反人工评审）→ 采纳 / 拒绝 → 版本标签与回滚；",
        "人工评审作为第二反馈通道：评审给出的修正转成自包含评审用例进入门禁，候选违反即被拒；",
        "语义化版本标签：每次采纳产出 flood-response-v1.N，带触发反馈 ID、触发评审 ID 与预期改善，可回滚。",
    ):
        bullet(doc, line)
    p(
        doc,
        "反馈信号口径统一为「受控评测环境下的反馈信号」（冻结 ground truth + 人工评审），"
        "不宣称真实市政数据回流；人工评审为角色扮演，不代表真实部门批复。",
    )

    # 二、数据集处理
    h(doc, "二、数据集处理")
    table(
        doc,
        ["处理环节", "做法", "结果"],
        [
            ["采集/构造", "按统一 Schema 构造 15 个资产：道路/管线/泵站/雨水口/气象预警/工单/预案/规程/施工方案/示意图", "12 种类型，3 种模态"],
            ["解析", "CSV/TSV 行列解析（utf-8-sig→gb18030→latin-1 回退）、MD/TXT 取首段、PNG 读 IHDR", "15/15 full/partial 解析，未识别列 0"],
            ["登记与治理", "内容寻址 asset_id、sha256、locator（{locator}#row:n / #line:n）、版本、有效期、可见性", "检索命中 100%，越权可拒答"],
            ["本体化", "种子本体 → 8 类抽取器（规则+词典）→ 置信度分档 0.80/0.55 → 人工确认 → 合并计划", "实测 101 三元组 / 8 谓词落地，溯源覆盖 100%"],
            ["时态语义", "as_of 查询 + snapshot_hash 锚定：同题在不同时间点结论不同（2021 已废止预案 vs 2025 修订预案）", "22 例评测含过期/时态用例，拒答有据"],
        ],
        widths=[3.0, 9.2, 4.8],
    )

    # 三、关键代码
    h(doc, "三、关键代码")
    table(
        doc,
        ["模块", "职责"],
        [
            ["src/decision_poc/deduction.py", "六跳推演引擎：规则数据化（每条带原文行号，加载时校验关键词）、冲突检测、资源缺口"],
            ["src/decision_poc/provenance.py", "claim 引用闸门 + append-only 哈希链 + 三层反向索引，verify 可定位篡改断点"],
            ["src/decision_poc/policy_evolution.py", "进化内核：偏差归因、候选策略、评测门禁、版本标签与回滚"],
            ["src/decision_poc/experience.py", "分层决策记忆（Tenant/User/Agent）+ 人工评审 → 评审用例"],
            ["src/decision_poc/visualize.py", "P10 可视化契约：VisualSnapshot → RenderJob（引擎无关演出单）+ ack 状态"],
            ["visualization/unreal/Content/Python/urban_twin_state.py", "UE 侧消费者：apply_job 演场景/高亮/面板，写本地 ack"],
        ],
        widths=[6.2, 10.8],
    )
    p(doc, "代码片段 1：渲染作业结构（引擎侧唯一需要认的字段，src/decision_poc/visualize.py）", "关键实现：")
    code_block(
        doc,
        [
            "class RenderJob(StrictModel):",
            '    schema_version: str = JOB_SCHEMA      # urban-twin-render-job-v1',
            "    job_id: str",
            "    snapshot_id: str                     # 同一决策 + 同一策略 → 同一 id",
            "    status: str = \"pending\"",
            "    scene: dict[str, Any]                # scenario / steps / actions / policy_version",
            "    panel: dict[str, Any]                # 分配方案面板文本",
            "    metrics: dict[str, Any]",
            "    warnings: list[str]",
        ],
    )
    p(doc, "代码片段 2：引擎侧认领与回写（visualization/unreal/Content/Python/urban_twin_state.py）", "关键实现：")
    code_block(
        doc,
        [
            "def apply_job(job: dict) -> dict:",
            "    scene = job.get(\"scene\") or {}",
            "    panel = job.get(\"panel\") or {}",
            "    apply_scenario(scene.get(\"scenario\", \"flood\"))",
            "    steps = scene.get(\"steps\") or []",
            "    if steps:",
            "        payload = {\"steps\": steps, \"actions\": scene.get(\"actions\") or []}",
            "        show_step(0, payload)",
            "    ...",
            "    return job",
        ],
    )

    # 四、可量化指标
    h(doc, "四、可量化指标")
    p(
        doc,
        "以下数字全部由仓库命令生成并冻结期望（evidence_check 106/106），任一数字漂移即失败；不接受为过检放宽期望。",
    )
    table(
        doc,
        ["指标", "结果", "核对方式"],
        [
            ["单元测试", "814 项通过", "uv run --locked pytest -q"],
            ["对外数字自检", "106/106 项一致（期望指纹 886a4c07762cdf96）", "python -m decision_poc.evidence_check"],
            ["P8 事件评测", "22/22 通过（改进前基线 17/22）", "python -m decision_poc.urban_eval"],
            ["六跳完整率 / 引用完整率 / 检索命中率", "100% / 100% / 100%", "docs/reports/p8-evaluation-report.md"],
            ["数据资产解析", "15/15，未识别列 0", "python -m decision_poc.health"],
            ["本体三元组 / 谓词", "101 三元组 / 8 谓词（种子 10 类型 / 11 谓词）", "samples/assets/ontology_candidates.json"],
            ["推演规则", "28 条，全部带原文行号并在加载时校验", "samples/rules/urban_deduction_rules.json"],
            ["P9 进化（训练 / 留出）", "偏差 0.2264→0.0000 / 0.2182→0.0000", "docs/reports/p9-policy-evolution-report.md"],
            ["人工评审第二信号", "评审偏差 0.1171→0.0000，违反评审的候选全部被拒", "同上"],
            ["分层记忆 warm start", "第二会话起点偏差 0.0000，轮次 3→1", "同上"],
            ["策略版本", "flood-response-v1.0 → v1.2，可回滚；A-03 4→5 台 / B-01 2→1 台", "同上"],
            ["Nexent 真机", "13 工具 / Agent v10 / conversation_id=78 / 六跳 8/8 一致 / 2 次工具调用", "docs/reports/nexent-live-integration-report.md"],
            ["P10 可视化契约", "RenderJob snap-7f476f6c5880ee60 → 引擎演出 → ack（engine=blender-eevee），delivery=acked", "python -c \"from decision_poc.visualize import delivery_state; print(delivery_state('snap-7f476f6c5880ee60'))\""],
        ],
        widths=[4.4, 7.4, 5.2],
    )

    # 五、演示截图
    h(doc, "五、演示截图")
    p(
        doc,
        "以下截图由 RenderJob 驱动渲染：同一事件、逐跳高亮，每张图右下角印 snapshot_id 与策略版本，可当场与 acks 目录核对。",
    )
    for source, caption in IMAGES:
        if not source.exists():
            p(doc, f"〔缺图：{source.name}〕")
            continue
        prepared = prepare_image(source, source.stem + ".jpg")
        doc.add_picture(str(prepared), width=Cm(14.0))
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = cap.add_run(caption)
        set_font(run, size=9, color=RGBColor(0x55, 0x55, 0x55))

    # 六、复现与部署
    h(doc, "六、复现与部署")
    bullet(doc, "离线复现：python scripts/reproduce.py（17 步全过，约 30 秒）；python -m decision_poc.evidence_check（106/106）")
    bullet(doc, "Web 演示：python -m decision_poc.web_demo --port 8787（推演面板 /，进化对比面板 /evolution）")
    bullet(doc, "Nexent 平台：deploy/nexent/README.md（本机 Docker 单环境装配与复现步骤）")
    bullet(doc, "可视化闭环：决策层写 RenderJob → 引擎侧消费者逐跳演出 → 写 ack（两套引擎实现：unreal / blender-eevee）")
    p(
        doc,
        "部署说明与复现步骤均以仓库 README 为准；数字与期望绑定，改数字必须同步期望文件，不允许为过检放宽期望。",
    )

    # 七、边界声明
    h(doc, "七、边界声明（答辩前必读，避免被追问时口径不一致）")
    for line in (
        "反馈来源：受控评测环境下的反馈信号（冻结 ground truth + 人工评审），不是真实市政数据回流；",
        "人工评审：演示中为角色扮演，不代表真实部门批复；",
        "分层记忆：离线同构实现，未在 Nexent 平台真机验证；",
        "数据：samples/ 为合成数据，底图 OSM 部分为真实数据（ODbL 1.0），材料中分开声明；",
        "Nexent：只在小规模本机 Docker 演示环境验证，不宣称多 Agent 协同；",
        "可视化：六跳帧/验收图为 Blender EEVEE 渲染，不是 UE 实时视口；UE 侧管线已跑通但未出成品图。",
    ):
        bullet(doc, line)

    # 八、待补清单
    h(doc, "八、待补清单（套版前必须完成）")
    table(
        doc,
        ["优先级", "事项", "状态"],
        [
            ["P0", "拿到官方模板 1 / 模板 2 与报名通知原文（工作区目前无任何 .docx）", "阻塞在用户侧"],
            ["P0", "本内容稿按模板 1 套版，补齐封面信息与官方字段", "待做"],
            ["P1", "代码提交并推送（工作区 80+ 改动未提交；复现核验按 README 跑到旧 commit 数字会对不上）", "待用户授权"],
            ["P1", "公网/云上 Demo 地址（当前仅 localhost:8787）", "待做"],
            ["P1", "SKILL.md 上传 Nexent 技能仓库", "待做"],
            ["P2", "真实用户评分 3–5 人（进化前后各一次）", "待组织"],
            ["P2", "平台界面截图 / 演示录屏（答辩材料）", "待做"],
            ["P2", "应用价值量化三组对照（决策耗时 / CF1 通行代价 / 缺口上报提前量）", "待做"],
        ],
        widths=[1.6, 11.4, 4.0],
    )

    # 页脚
    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("本内容稿由仓库材料自动整理；官方模板落库后按其字段重排，不改动任何证据数字。")
    set_font(run, size=8.5, color=RGBColor(0x88, 0x88, 0x88))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"written {path} ({path.stat().st_size} bytes)")
