#!/usr/bin/env python3
"""面试稿/材料自检：docx 与 pptx 里不该出现的内部词，逐处打出来。

为什么单开一个脚本
------------------
页面文案的检查在 `scripts/verify_ui_claims.py` 里（跑 Playwright 读真实 DOM），
但 Word / PPT 是二进制，grep 扫不到。这两份是要交给评委的文件，里面的
「挂载 / 判据 / 指纹 / 六跳」这类内部叫法比页面上更扎眼——评委读文档时没有
上下文可补，只能猜。

用法
----
    python scripts/check_internal_words.py                 # 默认扫两份材料
    python scripts/check_internal_words.py a.docx b.pptx   # 指定文件

需要 python-docx / python-pptx，用**系统 python**跑（`.venv` 里没装）。

退出码：0 = 干净，1 = 有命中（逐条打印位置，直接按行定位去改）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 词 → 建议说法。键是「不许出现」的写法，值给改法（None 表示要人判断）。
BANNED: dict[str, str | None] = {
    "挂载": "接入 / 提供",
    "判据": "判断项 / 一致项",
    "指纹": "校验值",
    "冻结": "既定 / 固定",
    "快照": "数据版本 / 数据副本",
    "复跑": "重跑",
    "内核": "规则引擎 / 决策引擎",
    "六跳": "六步",
    "入参": "输入参数",
}

DEFAULT_FILES = (
    "docs/competition/初赛作品信息收集表-内容稿-v0.1.docx",
    "docs/competition/初赛答辩-PPT-v0.1.pptx",
)


def scan_docx(path: Path) -> list[str]:
    from docx import Document

    hits: list[str] = []
    doc = Document(str(path))
    for idx, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if text:
            hits += _hits(f"段落 {idx}", text)
    for ti, table in enumerate(doc.tables):
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                text = cell.text.strip()
                if text:
                    hits += _hits(f"表 {ti + 1} 第 {ri + 1} 行 第 {ci + 1} 列", text)
    return hits


def scan_pptx(path: Path) -> list[str]:
    from pptx import Presentation

    hits: list[str] = []
    prs = Presentation(str(path))
    for si, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    hits += _hits(f"第 {si} 页", text)
    return hits


def _hits(where: str, text: str) -> list[str]:
    found = []
    for word, advice in BANNED.items():
        if word in text:
            hint = f"→ 改「{advice}」" if advice else "→ 需人工判断"
            found.append(f"{where}｜{word} {hint}｜{text[:90]}")
    return found


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [ROOT / p for p in DEFAULT_FILES]
    total = 0
    print("=" * 72)
    print("材料内部词自检（评委可见面禁词）")
    print("=" * 72)
    for path in targets:
        if not path.is_file():
            print(f"  [跳过] {path}（文件不存在）")
            continue
        suffix = path.suffix.lower()
        if suffix == ".docx":
            hits = scan_docx(path)
        elif suffix == ".pptx":
            hits = scan_pptx(path)
        else:
            print(f"  [跳过] {path.name}（只认 .docx / .pptx）")
            continue
        print(f"\n[{path.name}] {len(hits)} 处")
        for line in hits:
            print(f"  - {line}")
        total += len(hits)

    print("\n" + "=" * 72)
    if total:
        print(f"共 {total} 处需要改。词表来源：docs/competition/内部词替换清单.md")
        return 1
    print("没有命中，可以交。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
