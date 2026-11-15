"""城市可移植性审计：找出代码里写死的城市常量，按「注释 / 字符串 / 代码行」分类。

用途：回答「换一个城市要改什么」。判据很简单——
  * 只在注释和文案里出现 → 换城市改文案；
  * 出现在**非字符串的代码行**（常量、比较、默认值）→ 换城市会算错或指向不存在的台账。

用法：
    python scripts/audit_city_constants.py            # 摘要 + 代码行明细
    python scripts/audit_city_constants.py --all      # 连字符串行一起打印
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "decision_poc"
SCRIPTS = ROOT / "scripts"

# 城市 / 场景 / 台账里写死的标识
PATTERNS = [
    r"兴华路", r"滨河路", r"建设大道", r"兴华支路", r"民生街", r"环城北路", r"工业大道",
    r"\bA-03\b", r"\bB-01\b",
    r"\bR-10[1-5]\b", r"\bR-20[12]\b",
    r"成都", r"兴华",
    r"\bDF-IN-\d+", r"\bPS-\d+", r"\bPL-[A-Z]{3}-\d+",
    r"\bDN\d{3}\b",
]
RX = re.compile("|".join(PATTERNS))


def scan(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    # 先标出所有字符串字面量所在行（用 tokenize，标不出就当普通代码行）
    str_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            # 3.12 起 f-string 拆成 FSTRING_START/MIDDLE/END，不再是单个 STRING；
            # 只认 STRING 会把 f 字符串里的中文当成代码行（实测误报 4 行）。
            name = tokenize.tok_name.get(tok.type, "")
            if name.startswith("STRING") or name.startswith("FSTRING"):
                for ln in range(tok.start[0], tok.end[0] + 1):
                    str_lines.add(ln)
    except Exception:
        pass
    out = []
    for i, line in enumerate(lines, 1):
        if not RX.search(line):
            continue
        s = line.strip()
        if s.startswith("#"):
            kind = "comment"
        elif i in str_lines:
            # 粗略区分：整行只由字符串/括号构成 → 文档串或纯文案
            kind = "string"
        else:
            kind = "code"
        out.append({"file": str(path.relative_to(ROOT)), "line": i, "kind": kind,
                    "hits": sorted(set(RX.findall(line))), "text": s[:150]})
    return out


def main() -> None:
    show_all = "--all" in sys.argv
    rows = []
    for p in sorted(list(SRC.glob("*.py")) + list(SCRIPTS.glob("*.py"))):
        if p.name.startswith("test_"):
            continue
        rows += scan(p)
    by = {}
    for r in rows:
        by.setdefault(r["file"], {"code": 0, "string": 0, "comment": 0})
        by[r["file"]][r["kind"]] += 1
    print("== 每文件命中行数（code=非字符串行的代码）==")
    for f, c in sorted(by.items(), key=lambda kv: -(kv[1]["code"] * 100 + kv[1]["string"])):
        print(f"  {c['code']:>4} code  {c['string']:>4} str  {c['comment']:>4} cmt   {f}")
    print(f"\n合计 {len(rows)} 行命中，其中 code {sum(1 for r in rows if r['kind']=='code')} 行\n")
    print("== code 行明细（换城市会不会算错，看这里）==")
    for r in rows:
        if r["kind"] == "code" or (show_all and r["kind"] == "string"):
            print(f"  {r['kind']:<7} {r['file']}:{r['line']}  [{'/'.join(r['hits'])}]  {r['text']}")


if __name__ == "__main__":
    main()
