#!/usr/bin/env python3
"""交付体检：一条命令回答"我现在在哪、能不能交"。

用法：
    python scripts/delivery_check.py          # quick，只读文件与快照，几秒
    python scripts/delivery_check.py --full   # 追加跑测试与自检（约 1 分钟）
    python scripts/delivery_check.py --json out/delivery.json

设计原则
--------
1. **每项都要有证据路径**，不接受"我记得做过了"。
2. 分成两档判据，别混：`技术` 决定能不能跑，`材料` 决定评委能不能看懂。
   两档都缺不了——缺任何一项就交不出去。
3. **业务价值实测、真实用户评分不在范围内**（2026-11-14 定）：
   本作品的使用者是个位数、且这是初赛；这两项没有可信样本，硬凑等于编数。
   它们照旧列出来并实时读状态，但一律标「范围外」，不进判决、不参与扣分。
   材料里因此**不得宣称省人力/省时间**——这条是禁令，不是待办。
4. 只读。不写平台、不改文件、不跑会失败的命令（失败也只记为缺口）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OK, WARN, MISS, SKIP = "ok", "warn", "missing", "skip"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _grep(path: Path, pattern: str, limit: int = 0) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    hits = [ln.strip() for ln in text.splitlines() if re.search(pattern, ln)]
    return hits[:limit] if limit else hits


# --- 各项检查 ------------------------------------------------------------


def check_selftest(full: bool) -> dict:
    """108 项对外数字自检。--full 时真跑；否则明确标 SKIP，不拿旧报告冒充结论。"""
    if not full:
        return {"name": "对外数字自检", "state": SKIP,
                "detail": "未检查（加 --full 实跑，约 10 秒）",
                "evidence": "python -m decision_poc.evidence_check"}
    if full:
        try:
            out = subprocess.run(
                [sys.executable, "-m", "decision_poc.evidence_check"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=180,
                env={**__import__("os").environ, "PYTHONPATH": "src"},
            )
            m = re.search(r"(\d+)/(\d+) 项一致.*?指纹\s*([0-9a-f]+)", out.stdout)
            if m and m.group(1) == m.group(2):
                return {"name": "对外数字自检", "state": OK,
                        "detail": f"{m.group(1)}/{m.group(2)}，指纹 {m.group(3)}",
                        "evidence": "python -m decision_poc.evidence_check"}
            return {"name": "对外数字自检", "state": MISS,
                    "detail": (out.stdout or out.stderr)[-300:],
                    "evidence": "python -m decision_poc.evidence_check"}
        except Exception as exc:  # noqa: BLE001
            return {"name": "对外数字自检", "state": MISS, "detail": f"跑不起来：{exc}", "evidence": "—"}
    return {"name": "对外数字自检", "state": SKIP, "detail": "未检查", "evidence": "—"}


def check_tests(full: bool) -> dict:
    if not full:
        return {"name": "测试套件", "state": SKIP,
                "detail": "未检查（加 --full 实跑，约 40 秒）", "evidence": "pytest -q"}
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=900,
        )
        tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        passed = re.search(r"(\d+) passed", out.stdout)
        failed = re.search(r"(\d+) failed", out.stdout)
        if failed:
            return {"name": "测试套件", "state": MISS, "detail": tail, "evidence": "pytest -q"}
        if passed:
            return {"name": "测试套件", "state": OK,
                    "detail": f"{passed.group(1)} 项通过" + ("（含 skip）" if "skipped" in out.stdout else ""),
                    "evidence": "pytest -q"}
        return {"name": "测试套件", "state": WARN, "detail": tail or "无输出", "evidence": "pytest -q"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "测试套件", "state": MISS, "detail": f"跑不起来：{exc}", "evidence": "—"}


def check_main_chain() -> dict:
    """主链路 8/8：读最近一次真机证据，不重跑（重跑要花 token）。"""
    cands = sorted(
        ROOT.glob("samples/eval/nexent_urban*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for path in cands:
        data = _load_json(path)
        if not isinstance(data, dict) or "checks" not in data:
            continue
        checks = data.get("checks") or []
        passed = sum(1 for c in checks if c.get("passed"))
        state = OK if passed == len(checks) == 8 else MISS
        return {"name": "主链路（真机 8/8）", "state": state,
                "detail": f"{passed}/{len(checks)} 项一致，conversation {data.get('conversation_id')}，"
                          f"attempts={data.get('attempts_used')}",
                "evidence": _rel(path), "mtime": _mtime(path)}
    return {"name": "主链路（真机 8/8）", "state": MISS, "detail": "没有可解析的证据文件", "evidence": "samples/eval/"}


def check_collab() -> dict:
    for path in sorted(ROOT.glob("samples/nexent/live/collab*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        attempts = data.get("attempts") or data.get("runs") or []
        if not isinstance(attempts, list):
            continue
        okn = sum(1 for a in attempts if a.get("fingerprint_match"))
        if attempts:
            return {"name": "协同链路（指纹一致）", "state": OK if okn == len(attempts) else WARN,
                    "detail": f"{okn}/{len(attempts)} 次指纹逐字一致",
                    "evidence": _rel(path), "mtime": _mtime(path)}
    return {"name": "协同链路（指纹一致）", "state": MISS, "detail": "无证据文件", "evidence": "samples/nexent/live/"}


def check_platform_snapshot() -> dict:
    path = ROOT / "artifacts/ledger/platform_versions.json"
    data = _load_json(path)
    if not isinstance(data, dict):
        return {"name": "平台版本快照", "state": WARN,
                "detail": "未生成（跑 snapshot_platform_versions.py）", "evidence": _rel(path)}
    agents = data.get("platform_agents") or []
    if not agents:
        return {"name": "平台版本快照", "state": WARN, "detail": "快照里没有 agent", "evidence": _rel(path)}
    return {"name": "平台版本快照", "state": OK,
            "detail": " / ".join(
                f"{str(a.get('name','?')).replace('urban_','')} v{a.get('current_version')}"
                for a in agents[:6]),
            "evidence": _rel(path), "mtime": _mtime(path)}


def check_materials() -> list[dict]:
    items = []
    docx = ROOT / "docs/competition/初赛作品信息收集表-内容稿-v0.1.docx"
    pptx = ROOT / "docs/competition/初赛答辩-PPT-v0.1.pptx"
    for name, path, note in (
        ("作品信息收集表（Word）", docx, "需套官方模板后导出 PDF"),
        ("初赛答辩 PPT", pptx, "需套官方模板/改版式"),
    ):
        if path.is_file():
            items.append({"name": name, "state": OK,
                          "detail": f"{path.stat().st_size // 1024} KB，{_mtime(path)}（{note}）",
                          "evidence": _rel(path)})
        else:
            items.append({"name": name, "state": MISS, "detail": "文件不存在", "evidence": "docs/competition/"})
    return items


def check_recording() -> dict:
    """操作演示录屏：文件名带 demo/录屏/walkthrough 的才算，场景渲染片段不算。"""
    pat = re.compile(r"(demo|walkthrough|录屏|luping|xuanpian)", re.I)
    found = []
    for ext in ("*.mp4", "*.webm", "*.mov"):
        for p in ROOT.rglob(ext):
            if "node_modules" in str(p) or "dist-offline" in str(p):
                continue
            if pat.search(p.name):
                found.append(p)
    if found:
        p = max(found, key=lambda x: x.stat().st_mtime)
        return {"name": "操作演示录屏", "state": OK,
                "detail": f"{p.name}，{_mtime(p)}", "evidence": _rel(p)}
    return {"name": "操作演示录屏", "state": MISS,
            "detail": "只有场景渲染片段，没有「提问→调工具→出结论」的录屏", "evidence": "—"}


def check_online_review_doc() -> dict:
    path = ROOT / "docs/competition/线上评审动线.md"
    if path.is_file():
        return {"name": "线上评审动线", "state": OK, "detail": f"{_mtime(path)}", "evidence": _rel(path)}
    return {"name": "线上评审动线", "state": MISS, "detail": "缺失", "evidence": "—"}


def check_real_feedback() -> dict:
    for cand in (ROOT / "reviews", ROOT / "samples/reviews"):
        if cand.is_dir():
            n = len([p for p in cand.glob("*") if p.is_file()])
            if n >= 3:
                return {"name": "真实用户评分（3–5 人）", "state": OK, "detail": f"{n} 份", "evidence": _rel(cand)}
            return {"name": "真实用户评分（3–5 人）", "state": MISS,
                    "detail": f"仅 {n} 份，需要 ≥3 人", "evidence": _rel(cand)}
    return {"name": "真实用户评分（3–5 人）", "state": MISS, "detail": "样本 0", "evidence": "reviews/"}


def check_value_measurement() -> dict:
    """业务价值实测：必须是真填了数据的记录，模板空行不算。"""
    import csv as _csv

    for cand in ROOT.rglob("value-measurement*.csv"):
        try:
            with cand.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(_csv.DictReader(fh))
        except Exception:
            continue
        filled = [
            r for r in rows
            if str(r.get("minutes") or "").strip() not in ("", "0", "0.0")
            and str(r.get("started_at") or "").strip()
        ]
        if filled:
            mins = [float(r["minutes"]) for r in filled]
            return {"name": "业务价值实测（省多少时间）", "state": OK,
                    "detail": f"{len(filled)} 条实测，方案成形时间 {min(mins):.1f}–{max(mins):.1f} 分钟",
                    "evidence": _rel(cand)}
        return {"name": "业务价值实测（省多少时间）", "state": MISS,
                "detail": f"模板已建但 {len(rows)} 行全空（minutes=0、无计时），等于没测",
                "evidence": _rel(cand)}
    return {"name": "业务价值实测（省多少时间）", "state": MISS,
            "detail": "没有实测记录；材料里不得宣称「省人力/省时间」", "evidence": "—"}


def check_mcp_count() -> dict:
    """MCP 工具数口径：从 setup_nexent.py 的 OUR_TOOLS 读，避免和平台漂移。"""
    path = ROOT / "deploy/nexent/setup_nexent.py"
    hits = _grep(path, r"^OUR_TOOLS\s*=", limit=1)
    if not hits:
        return {"name": "MCP 工具口径", "state": WARN, "detail": "未找到 OUR_TOOLS 定义", "evidence": _rel(path)}
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        block = src.split("OUR_TOOLS =", 1)[1].split("}", 1)[0]
        n = len(re.findall(r'"([a-z_]+)"', block))
        return {"name": "MCP 工具口径", "state": OK,
                "detail": f"{n} 个（11 在线决策 + 4 离线渲染）", "evidence": _rel(path)}
    except Exception:
        return {"name": "MCP 工具口径", "state": WARN, "detail": "解析失败", "evidence": _rel(path)}


# --- 主流程 --------------------------------------------------------------

HARD_GROUPS = ("技术", "材料")
#: 范围外分组：留在这里是为了「什么时候想补就能看到现状」，不是因为它是待办。
OUT_OF_SCOPE_GROUP = "范围外（初赛不计）"
OUT_OF_SCOPE_REASON = "不在参赛范围：使用者个位数、初赛阶段无可信样本；材料不宣称即可"


def _out_of_scope(item: dict) -> dict:
    """把一项标成范围外：保留实测明细，但状态不计入判决。"""

    return {
        "name": item["name"],
        "state": SKIP,
        "detail": f"{item['detail']}｜{OUT_OF_SCOPE_REASON}",
        "evidence": item.get("evidence") or "—",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="真跑测试与自检")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    args = ap.parse_args()

    rows: list[tuple[str, list[dict]]] = [
        ("技术", [
            check_tests(args.full),
            check_selftest(args.full),
            check_main_chain(),
            check_collab(),
            check_platform_snapshot(),
            check_mcp_count(),
        ]),
        ("材料", [
            check_online_review_doc(),
            check_recording(),
            *check_materials(),
        ]),
        (OUT_OF_SCOPE_GROUP, [
            _out_of_scope(check_real_feedback()),
            _out_of_scope(check_value_measurement()),
        ]),
    ]

    print("=" * 74)
    print("交付体检 ·", datetime.now().strftime("%Y-%m-%d %H:%M"), "（--full" + ("）" if args.full else " 关闭，测试/自检未实跑）"))
    print("=" * 74)
    icon = {OK: "✔", WARN: "!", MISS: "✘", SKIP: "·"}
    gaps: dict[str, list[str]] = {}
    for group, items in rows:
        print(f"\n[{group}]")
        for it in items:
            print(f"  {icon[it['state']]} {it['name']}：{it['detail']}")
            if it.get("evidence") and it["evidence"] != "—":
                print(f"      证据 {it['evidence']}")
            if it["state"] not in (OK, SKIP):
                gaps.setdefault(group, []).append(f"{it['name']}：{it['detail']}")

    hard = [g for g in HARD_GROUPS if gaps.get(g)]

    print("\n" + "=" * 74)
    verdict = "还不能交" if hard else "可以交付（技术+材料齐）"
    print("判决：", verdict)
    if hard:
        print("\n必须先补（缺了交不出去）：")
        for g in hard:
            for g_item in gaps[g]:
                print(f"  ✘ {g_item}")
    print("\n范围外（不补也不扣分，只是照实列出）：业务价值实测、真实用户评分。"
          "材料里不得宣称「省人力/省时间」。")
    print("=" * 74)

    if args.json:
        out = ROOT / args.json if not Path(args.json).is_absolute() else Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "full": args.full,
            "verdict": verdict,
            "groups": [{"group": g, "items": items} for g, items in rows],
            "blocking": gaps.get("技术", []) + gaps.get("材料", []),
            "out_of_scope": [it["name"] for g, items in rows
                             if g == OUT_OF_SCOPE_GROUP for it in items],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("JSON →", _rel(out))

    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
