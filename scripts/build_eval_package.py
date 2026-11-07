# -*- coding: utf-8 -*-
"""build_eval_package: 把散落的评测材料打成一个自包含可提交单元。

输出 dist/eval-package/（git 外，与 dist-offline 同模式）：
    README.md          入口 = eval-dataset-spec.md（数据字典/真合成声明/消融/复跑）
    RUN.md             复跑命令（可复制粘贴执行）
    cases/urban_cases.json        推演评测集 22 例
    cases/allocation_cases.json   分配策略评测集 12 例
    results/latest.json           现状指标快照
    results/ablation.json         五级消融
    ground-truth.md               期望值口径依据（不由实现反推）
    MANIFEST.txt                  文件清单 + sha256

不做的事：不改任何评测数字；指纹以生成时 evidence_check 实跑为准，包内不做新断言。
"""
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "eval-package"

COPIES = [
    (Path("docs/competition/eval-dataset-spec.md"), Path("README.md")),
    (Path("samples/eval/urban_cases.json"), Path("cases/urban_cases.json")),
    (Path("samples/eval/allocation_cases.json"), Path("cases/allocation_cases.json")),
    (Path("samples/eval/latest.json"), Path("results/latest.json")),
    (Path("samples/eval/ablation.json"), Path("results/ablation.json")),
    (Path("docs/urban-scenario-ground-truth.md"), Path("ground-truth.md")),
]

RUN_MD = """# 复跑命令（在 agent_poc 仓库根执行）

```bash
# 1) 推演评测（22 例）——入口是 urban_eval，不是 evaluation（后者无 CLI）
PYTHONPATH=src uv run --locked python -m decision_poc.urban_eval \\
    --cases samples/eval/urban_cases.json

# 2) 分配策略评测（12 例，含 train/holdout）
PYTHONPATH=src uv run --locked python -m decision_poc.policy_evolution \\
    --cases samples/eval/allocation_cases.json

# 3) 五级消融对照
PYTHONPATH=src uv run --locked python -m decision_poc.ablation --check

# 4) 对外数字自检（108 项）
PYTHONPATH=src uv run --locked python -m decision_poc.evidence_check
```

本包由 `scripts/build_eval_package.py` 于 {ts} 生成；快照指纹以生成时 evidence_check 输出为准。
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    for src_rel, dst_rel in COPIES:
        src = ROOT / src_rel
        if not src.is_file():
            raise SystemExit(f"missing source: {src_rel}")
        dst = OUT / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    (OUT / "RUN.md").write_text(
        RUN_MD.format(ts=datetime.now().isoformat(timespec="seconds")), encoding="utf-8"
    )

    lines = []
    for p in sorted(OUT.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.txt":
            rel = p.relative_to(OUT)
            lines.append(f"{sha256(p)}  {rel.as_posix()}")
    (OUT / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"package: {OUT}")
    print(f"files: {len(lines)}  size: {total/1024:.0f} KB")


if __name__ == "__main__":
    main()
