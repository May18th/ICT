"""把技能包打成可直接分发的 zip，并给出与内容绑定的校验清单。

为什么要打包
------------
技能包里只有 `SKILL.md`，本身就是文本；散在仓库里评审查不到、换环境装不上。
打成一个 zip 后三件事成立：

1. 换城市/换团队时整包拿走，不必再从 Git 历史里捡文件；
2. zip 里带 `MANIFEST.json`，逐文件记 sha256，拆包改过一眼可见；
3. 打包是确定性的（固定时间戳、固定顺序），同样的输入每次哈希都一样。

边界
----
- 只读 `samples/nexent/skill/*/SKILL.md`，不改内容（这几个文件的哈希被仓库校验记账）；
- 产物落 `out/skills/`，不进仓库；
- 不打包任何台账数据：数据随城市走，技能包不该带。

用法
----
```powershell
$env:PYTHONPATH = "src"
uv run --locked python scripts/pack_skills.py                    # 打包
uv run --locked python scripts/pack_skills.py --out out/skills   # 指定目录
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "samples" / "nexent" / "skill"

#: 固定时间戳：让同一份内容每次打出的 zip 逐字节一致
FIXED_STAMP = (2026, 1, 1, 0, 0, 0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect() -> list[tuple[str, bytes]]:
    """按目录名排序收集技能文件，路径保持 `skill/<name>/SKILL.md` 结构。"""

    items: list[tuple[str, bytes]] = []
    for path in sorted(SKILL_ROOT.glob("*/SKILL.md")):
        arcname = f"skills/{path.parent.name}/SKILL.md"
        items.append((arcname, path.read_bytes()))
    if not items:
        raise SystemExit(f"{SKILL_ROOT} 下没有 */SKILL.md")
    return items


def pack(out_dir: Path) -> tuple[Path, dict[str, object]]:
    items = collect()
    manifest = {
        "schema_version": "urban-skill-pack-v1",
        "created_by": "scripts/pack_skills.py",
        "files": [
            {"path": arcname, "sha256": _sha256(data), "bytes": len(data)}
            for arcname, data in items
        ],
        "note": "包内只含 SKILL.md（frontmatter: name / description）；台账与资产数据不随包分发。",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "urban-skills.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in items:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
        info = zipfile.ZipInfo("MANIFEST.json", date_time=FIXED_STAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    manifest["archive"] = str(target)
    manifest["archive_sha256"] = _sha256(target.read_bytes())
    manifest["archive_bytes"] = target.stat().st_size
    return target, manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="技能包 zip（确定性打包）")
    parser.add_argument("--out", default="out/skills")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    target, manifest = pack(out_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"zip: {target}")
    print(f"打包时间: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
