#!/usr/bin/env python
"""Fetch the Kong et al. (2023) Zenodo asset bundle and verify its checksum.

数据不进 git：仓里只留本脚本与期望的 sha256。Zenodo 记录带永久 DOI，
所以任何一次 clone 都能重新拉回同一份字节。

    python experiments/literature_repro/external_kong2023/fetch_kong2023.py --dest D:/_extern/kong2023

许可：CC BY 4.0（Zenodo 记录 7894921 标注 open + cc-by-4.0）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

RECORD_URL = "https://zenodo.org/api/records/7894921"
FILE_URL = "https://zenodo.org/api/records/7894921/files/Assets.zip/content"
EXPECTED_SHA256 = "32a07f8e467598ce5923f2e34b3edb32a5fff20918dc742daaaadf1986c82e76"
EXPECTED_BYTES = 2_878_463


def download(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "Assets.zip"
    if zip_path.exists() and _sha256(zip_path) == EXPECTED_SHA256:
        print(f"[skip] 已存在且校验通过：{zip_path}")
        return zip_path

    print(f"[get ] {FILE_URL}")
    with urllib.request.urlopen(FILE_URL, timeout=120) as response:
        zip_path.write_bytes(response.read())

    actual = _sha256(zip_path)
    if actual != EXPECTED_SHA256:
        raise SystemExit(
            f"校验失败：期望 {EXPECTED_SHA256}，实际 {actual}。"
            "Zenodo 记录被更新过，需重新核对口径后再改期望值。"
        )
    print(f"[ok  ] sha256 {actual}")
    return zip_path


def unzip(zip_path: Path, dest: Path) -> Path:
    root = dest / "Assets"
    if root.is_dir() and any(root.iterdir()):
        print(f"[skip] 已解压：{root}")
        return root
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.filename.startswith("__MACOSX") or info.filename.endswith(".DS_Store"):
                continue
            archive.extract(info, dest)
    print(f"[ok  ] 解压到 {root}")
    return root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("_external/kong2023"),
        help="数据落地目录（默认在工作区外，不要指向 samples/）",
    )
    args = parser.parse_args()

    if "samples" in args.dest.parts:
        print("拒绝写入 samples/：该目录只读。", file=sys.stderr)
        return 2

    zip_path = download(args.dest)
    root = unzip(zip_path, args.dest)
    print(f"[done] {root}")
    print(f"[meta] Zenodo record: {RECORD_URL}")
    print(f"[meta] size on disk: {EXPECTED_BYTES} bytes (zip)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
