"""重建公网 Demo 发布包（`deploy-webdemo/`）。

为什么要有这个脚本
------------------
发布包是 2026-11-06 手工攒的，之后 `web_demo.py` 从 641 行涨到 8325 行，
包没跟着更新——线上表现为：`/map2d` 404、`/workbench` 404、`/status` 404、
页面标题还是旧的「数字孪生」。手工复制发现不了这种漂移（复制成功 ≠ 内容更新），
所以固化成脚本：每次改完跑一遍，`--check` 能对比出差异。

用法
----
    python scripts/build_webdemo_package.py            # 预生成 map2d + 重建发布包
    python scripts/build_webdemo_package.py --check    # 只报告差异，不写盘
    python scripts/build_webdemo_package.py --no-map2d # 跳过 2.5D 大屏预生成

发布包内容
----------
`src/` 全量（发布入口 import 得到）、`samples/` 全量（页面读台账，缺一个就 500）、
`assets/`（三维沙盘用）、`artifacts/`（进化/反馈/工作台的历史数据）、
`dist/map2d/index.html`（**预生成**：发布包不带 scripts/，靠产物直接返回）。
`main.py` / `requirements.txt` 由本脚本写入；`.env` 保留原文件（含密钥，不入库）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "deploy-webdemo"

#: 要同步的目录：(包内相对路径, 源路径)
#:
#: `artifacts/` 整个目录有 3 GB / 7.9 万个文件（p1-nexent 等阶段产物），
#: 但页面只读三个子目录：`/evolution` 读 evolution、反馈读 feedback、工作台读 workbench。
#: 只带这三个，其余不进包——否则上传一次要几十分钟，而且全是演示用不到的东西。
SYNC_DIRS: tuple[tuple[str, Path], ...] = (
    ("src", ROOT / "src"),
    ("samples", ROOT / "samples"),
    ("assets", ROOT / "assets"),
    ("artifacts/evolution", ROOT / "artifacts" / "evolution"),
    ("artifacts/feedback", ROOT / "artifacts" / "feedback"),
    ("artifacts/workbench", ROOT / "artifacts" / "workbench"),
    ("dist/map2d", ROOT / "dist" / "map2d"),
)

#: 复制时跳过的目录名 / 后缀
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

#: 单文件同步：`/map2d` 在发布包里需要现场生成，这两个脚本缺一不可。
#:
#: 发布工具会排除构建产物目录（`dist/` 传不上去），所以线上必然是「无产物 →
#: 现场生成」这条路径。`build_map2d.py` 生成页面，`build_event_timeline.py`
#: 从台账重算 7 步时刻——缺后者时 `out/map2d/event_timeline.json` 不存在，
#: 时标只能为 null，状态带会显示「回放 --:--」、7 步时刻全空（实测过）。
#: 只带这两个，不整目录带 `scripts/`：其余脚本是文档/训练/审计工具，与页面无关。
SYNC_FILES: tuple[tuple[str, Path], ...] = (
    ("scripts/build_map2d.py", ROOT / "scripts" / "build_map2d.py"),
    ("scripts/build_event_timeline.py", ROOT / "scripts" / "build_event_timeline.py"),
)

MAIN_PY = '''"""发布入口：读取沙箱注入的 PORT 环境变量，绑定 0.0.0.0 提供服务。

可选：应用目录下的 .env 会加载进进程环境（用于 DEEPSEEK_API_KEY 等）。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.web_demo import serve  # noqa: E402

if __name__ == "__main__":
    serve("0.0.0.0", int(os.environ.get("PORT", "8787")))
'''

REQUIREMENTS = "pydantic==2.14.1\npydantic-ai-slim[openai]==2.61.0\n"


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in SKIP_DIRS or Path(n).suffix in SKIP_SUFFIXES}


def count_files(base: Path) -> tuple[int, int]:
    """统计文件数与体积，跳过不参与发布的缓存文件。

    必须和 `_ignore` 用同一套规则：源目录里有 `__pycache__`、包内没有，
    按全量数会永远「197 vs 93 不一致」，`--check` 就失去意义了。
    """
    files = bytes_ = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts) or path.suffix in SKIP_SUFFIXES:
            continue
        files += 1
        bytes_ += path.stat().st_size
    return files, bytes_


def _fmt(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB"


def build_map2d() -> bool:
    script = ROOT / "scripts" / "build_map2d.py"
    if not script.is_file():
        print("  [SKIP] 找不到 scripts/build_map2d.py")
        return False
    print("  预生成 2.5D 大屏 …", end="", flush=True)
    proc = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        print(" 失败")
        print((proc.stderr or proc.stdout)[-600:])
        return False
    artifact = ROOT / "dist" / "map2d" / "index.html"
    print(f" 完成（{_fmt(artifact.stat().st_size)}）")
    return True


def git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="重建公网 Demo 发布包")
    ap.add_argument("--check", action="store_true", help="只对比差异，不写盘")
    ap.add_argument("--no-map2d", action="store_true", help="跳过 2.5D 大屏预生成")
    args = ap.parse_args()

    if not args.no_map2d and not args.check:
        build_map2d()

    print()
    rows: list[tuple[str, int, int, int, int]] = []
    for rel, src in SYNC_DIRS:
        if not src.is_dir():
            print(f"  [WARN] 源目录不存在：{src}")
            continue
        dst = OUT / rel
        s_files, s_bytes = count_files(src)
        d_files, d_bytes = (count_files(dst) if dst.is_dir() else (0, 0))
        rows.append((rel, d_files, s_files, d_bytes, s_bytes))

    print(f"  {'目录':<14}{'包内':>7}{'源':>7}   {'包内体积':>10}{'源体积':>10}")
    drift = 0
    for rel, d_files, s_files, d_bytes, s_bytes in rows:
        same = d_files == s_files and d_bytes == s_bytes
        mark = "  " if same else "≠ "
        if not same:
            drift += 1
        print(f"{mark}{rel:<14}{d_files:>7}{s_files:>7}   {_fmt(d_bytes):>10}{_fmt(s_bytes):>10}")

    # 生成器脚本单独比：漏了时标脚本线上时刻会全空，只看目录 diff 发现不了
    stale_files: list[str] = []
    for rel, src in SYNC_FILES:
        dst = OUT / rel
        if not (src.is_file() and dst.is_file() and dst.read_bytes() == src.read_bytes()):
            stale_files.append(rel)

    if args.check:
        parts = []
        if drift:
            parts.append(f"{drift} 个目录与源码不一致")
        if stale_files:
            parts.append("生成器需更新：" + "、".join(stale_files))
        print("\n结论：" + ("；".join(parts) if parts else "发布包与源码一致"))
        return 0 if not (drift or stale_files) else 1

    # 保留 .env：里面是密钥，且不在版本管理里，删了就没了
    env_backup = None
    env_path = OUT / ".env"
    if env_path.is_file():
        env_backup = env_path.read_bytes()

    print()
    for rel, src in SYNC_DIRS:
        if not src.is_dir():
            continue
        dst = OUT / rel
        if dst.exists():
            shutil.rmtree(dst)   # 先删再拷：合并复制会留下已删除的旧文件，正是漂移的成因
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=_ignore)
        print(f"  同步 {rel}")

    for rel, src in SYNC_FILES:
        if not src.is_file():
            print(f"  [WARN] 源文件不存在：{src}")
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  同步 {rel}")

    (OUT / "main.py").write_text(MAIN_PY, encoding="utf-8")
    (OUT / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    if env_backup is not None:
        env_path.write_bytes(env_backup)
    else:
        print("  [WARN] 原发布包没有 .env，线上调用大模型会失败（本地开发可忽略）")

    total_files, total_bytes = count_files(OUT)
    info = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git": git_rev(),
        "files": total_files,
        "bytes": total_bytes,
        "dirs": [rel for rel, _ in SYNC_DIRS],
        "generators": [rel for rel, _ in SYNC_FILES],
        "map2d_prebuilt": (OUT / "dist" / "map2d" / "index.html").is_file(),
        "note": (
            "线上走「无产物 → 现场生成」：发布工具排除 dist/，/map2d 由包内 "
            "scripts/build_map2d.py + build_event_timeline.py 现算（时刻来自台账）。"
            "包内不带 out/map2d/hydro_curve.json，水位无定量层，页面自动标注「示意节奏」。"
            "见 web_demo.build_map2d_html"
        ),
    }
    (OUT / "BUILD_INFO.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成：{total_files} 个文件 / {_fmt(total_bytes)} → {OUT}")
    print(f"构建信息：{OUT / 'BUILD_INFO.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
