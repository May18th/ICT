# -*- coding: utf-8 -*-
"""生成离线镜像包清单：sha256 + 体积，写 images/SHA256SUMS 与 deploy/offline/image-list.txt。

用法：python make_manifest.py <images_dir>
"""
import hashlib
import sys
from pathlib import Path

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()

def main() -> None:
    images_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "images")
    tars = sorted(images_dir.glob("*.tar"))
    if not tars:
        raise SystemExit(f"no *.tar under {images_dir}")

    sums_path = images_dir / "SHA256SUMS"
    list_path = images_dir.parent / "image-list.txt"

    lines, names = [], []
    total = 0
    for tar in tars:
        size = tar.stat().st_size
        total += size
        digest = sha256(tar)
        lines.append(f"{digest}  {tar.name}")
        names.append(tar.name)
        print(f"{tar.name}  {size/1e6:.0f}MB  {digest[:12]}…")
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # tar 文件名还原镜像名：repo_tag.tar → repo:tag（前若干段为 repo）
    # image-list.txt 由 save 脚本同源维护，这里从 tar 名反推仅作展示用途；
    # 权威清单见 deploy/offline/OFFLINE.md 附录。
    list_path.write_text(
        "\n".join(n[:-4].rsplit("_", 1)[0].replace("_", "/", 1) + ":" + n[:-4].rsplit("_", 1)[1] for n in names)
        + "\n",
        encoding="utf-8",
    )
    print(f"total: {total/1e9:.2f} GB across {len(tars)} tars")
    print(f"wrote: {sums_path.name}, image-list.txt")

if __name__ == "__main__":
    main()
