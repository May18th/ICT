"""打包「给队友的交付包」：把要发的文档重命名、修内部链接、并压成 zip。

为什么要脚本
-----------
交付包里的文件名带序号（`00-`、`01-`…），而仓库里的文档互相引用的是原名
（`docs/competition/队友A-材料线.md`）。手工改一遍，下次文档一更新就得再改一遍，
而且很容易改出 `04-04-录屏分镜.md` 这种双前缀（真的发生过两次）。

所以这里统一做三件事：
1. 按 PACKAGE 复制并重命名；
2. 链接替换分三层——带路径的先转占位符、裸名用正则并排除已有序号、最后统一还原；
3. 指向包外素材的引用改成一句说明，不留死链。

用法：
    .venv/Scripts/python.exe scripts/build_handoff_package.py
    .venv/Scripts/python.exe scripts/build_handoff_package.py --no-zip

产物：out/交付包-YYYY-MM-DD/ 与同名 .zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "competition"
CAPTURES = ROOT / "docs" / "reports" / "captures" / "nexent"

#: 仓库文件名 -> 包内文件名。只放队友开工要用的，不放过程记录与内部审计。
PACKAGE: list[tuple[str, str]] = [
    ("队友说明书.md", "00-队友说明书.md"),
    ("队友A-材料线.md", "01-队友A-材料线.md"),
    ("队友B-录屏线.md", "02-队友B-录屏线.md"),
    ("内部词替换清单.md", "03-措辞修改清单.md"),
    ("录屏分镜.md", "04-录屏分镜.md"),
    ("交付交接包.md", "05-交付交接包.md"),
    ("线上评审动线.md", "06-线上评审动线.md"),
    ("平台配置快照.md", "07-平台配置与版本.md"),
]

#: 原样复制的二进制材料。
BINARIES: list[tuple[pathlib.Path, str]] = [
    (SRC / "初赛作品信息收集表-内容稿-v0.1.docx", "初赛作品信息收集表-内容稿-v0.1.docx"),
    (SRC / "初赛答辩-PPT-v0.1.pptx", "初赛答辩-PPT-v0.1.pptx"),
    (CAPTURES / "agents.png", "agents.png"),
]

#: 带路径的引用（先替换，避免与裸名互相级联）。
FULL_LINKS: list[tuple[str, str]] = [
    ("docs/competition/初赛作品信息收集表-内容稿-v0.1.docx", "初赛作品信息收集表-内容稿-v0.1.docx"),
    ("docs/competition/初赛答辩-PPT-v0.1.pptx", "初赛答辩-PPT-v0.1.pptx"),
    ("docs/competition/内部词替换清单.md", "03-措辞修改清单.md"),
    ("docs/competition/交付交接包.md", "05-交付交接包.md"),
    ("docs/competition/录屏分镜.md", "04-录屏分镜.md"),
    ("docs/competition/线上评审动线.md", "06-线上评审动线.md"),
    ("docs/competition/平台配置快照.md", "07-平台配置与版本.md"),
    ("docs/competition/队友A-材料线.md", "01-队友A-材料线.md"),
    ("docs/competition/队友B-录屏线.md", "02-队友B-录屏线.md"),
    ("docs/competition/队友说明书.md", "00-队友说明书.md"),
]

#: 裸文件名（正则替换，并排除前面已经有 `NN-` 的情况）。
BARE_LINKS: list[tuple[str, str]] = [
    ("队友A-材料线.md", "01-队友A-材料线.md"),
    ("队友B-录屏线.md", "02-队友B-录屏线.md"),
    ("队友说明书.md", "00-队友说明书.md"),
    ("内部词替换清单.md", "03-措辞修改清单.md"),
    ("录屏分镜.md", "04-录屏分镜.md"),
    ("线上评审动线.md", "06-线上评审动线.md"),
    ("平台配置快照.md", "07-平台配置与版本.md"),
    ("交付交接包.md", "05-交付交接包.md"),
]

#: 包外素材 / 内部口径：改成一句人话，不留断链。
OUTSIDE: list[tuple[str, str]] = [
    ("`docs/competition/skills-mcp-design.md`", "项目仓库内的 `skills-mcp-design.md`（技术素材，不随包发）"),
    ("docs/competition/skills-mcp-design.md", "项目仓库内的 skills-mcp-design.md（技术素材，不随包发）"),
    ("`docs/competition/ppt-outline-10p.md`", "项目仓库内的 `ppt-outline-10p.md`（内部素材，不随包发）"),
    ("docs/competition/ppt-outline-10p.md", "项目仓库内的 ppt-outline-10p.md（内部素材，不随包发）"),
    ("六节名称来自项目记录 `winning-gap-audit.md::81`", "六节名称按官方模板"),
    ("`收集表.docx` + 导出的 `.pdf`", "`初赛作品信息收集表-内容稿-v0.1.docx` + 导出的 PDF"),
    ("`答辩PPT.pptx` + 导出的 `.pdf`（如果要求交 PDF）", "`初赛答辩-PPT-v0.1.pptx` + 导出的 PDF（如要求）"),
]

#: 面向评委/队友时不该出现的内部叫法。
WORDS: list[tuple[str, str]] = [
    ("内部词替换清单", "措辞修改清单"),
    ("内部词", "内部叫法"),
]

#: 示例文件名/后缀写法，检查断链时跳过。
ALLOWED_REFS = {
    "开发设计文档-v1.docx",
    "开发设计文档-v1.pdf",
    ".docx",
    ".pptx",
    ".pdf",
    ".docx/.pptx",
    # 这两份是包外素材，正文里已改写成「项目仓库内的 …（不随包发）」，不是死链
    "skills-mcp-design.md",
    "ppt-outline-10p.md",
}


def _rewrite(text: str) -> str:
    """重写链接与内部叫法。三层处理，避免级联。"""

    for old, new in OUTSIDE:
        text = text.replace(old, new)

    for i, (old, _new) in enumerate(FULL_LINKS):
        text = text.replace(old, f"\x01{i}\x01")
    for i, (_old, new) in enumerate(FULL_LINKS):
        text = text.replace(f"\x01{i}\x01", new)

    for old, new in BARE_LINKS:
        # `(?<!\d\d-)`：前面已经是「01-」这种序号就不再改名
        text = re.sub(r"(?<!\d\d-)" + re.escape(old), new, text)

    for old, new in WORDS:
        text = text.replace(old, new)
    return text


def _dead_links(folder: pathlib.Path) -> list[tuple[str, str]]:
    """检查包内 md 里还有没有指向包外的文件名引用。"""

    names = {p.name for p in folder.iterdir()}
    bad: list[tuple[str, str]] = []
    for md in sorted(folder.glob("*.md")):
        for m in re.finditer(r"([\w\-\u4e00-\u9fa5./]+\.(?:md|docx|pptx|png))", md.read_text("utf-8")):
            ref = m.group(1)
            if ref.startswith(("scripts/", "src/", "docs/", "tests/", "samples/", "deploy/")):
                continue
            if ref in ALLOWED_REFS or ref.split("/")[-1] in names:
                continue
            bad.append((md.name, ref))
    return sorted(set(bad))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不压缩")
    ap.add_argument("--out", default="", help="输出目录名，默认按日期")
    args = ap.parse_args()

    name = args.out or f"交付包-{dt.date.today():%Y-%m-%d}"
    dst = ROOT / "out" / name
    if dst.exists():
        for p in dst.iterdir():
            p.unlink()
    dst.mkdir(parents=True, exist_ok=True)

    for old, new in PACKAGE:
        text = _rewrite((SRC / old).read_text("utf-8"))
        (dst / new).write_text(text, "utf-8")
        print(f"  md   {new}  {len(text):>6} 字符")

    for src, new in BINARIES:
        if not src.exists():
            print(f"  跳过 {new}（源文件不存在：{src.relative_to(ROOT)}）")
            continue
        shutil.copy2(src, dst / new)
        print(f"  文件 {new}  {src.stat().st_size:>7} 字节")

    bad = _dead_links(dst)
    for a, b in bad:
        print(f"  ⚠ 包外引用：{a} -> {b}")
    if not bad:
        print("  链接检查：包内引用全部可跳转")

    if not args.no_zip:
        zpath = dst.with_suffix(".zip")
        if zpath.exists():
            zpath.unlink()
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(dst.iterdir()):
                z.write(p, p.name)
        print(f"\n打包完成：{zpath.relative_to(ROOT)}  {zpath.stat().st_size / 1024 / 1024:.2f} MB")

    print(f"目录：{dst.relative_to(ROOT)}  {len(list(dst.iterdir()))} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
