"""Build the data-asset manifest for the sample catalog.

    uv run --locked python -m decision_poc.asset_manifest

Writes `samples/assets/manifest.json` and prints one line per asset. The
manifest is the hand-off contract: ingestion and retrieval read it, and the
decision layer cites `asset_id` / `locator` / `standard_refs` from it.
"""

import json
import sys
from pathlib import Path

from .data_asset import AssetSpec, DataAsset, build_manifest, manifest_payload

CATALOG = (
    AssetSpec(
        path=Path("samples/assets/equipment_ledger.csv"),
        doc_type="设备台账",
        version="v1",
    ),
    AssetSpec(
        path=Path("samples/assets/inspection_log.csv"),
        doc_type="巡检记录",
        equipment_id="A-101",
        version="v1",
        standard_refs=("Q/XX-2024-11 §5.3",),
    ),
    AssetSpec(
        path=Path("samples/assets/maintenance_procedure.md"),
        doc_type="检修规程",
        equipment_id="A-101",
        version="v1",
        standard_refs=("ISO 10816-3:2009 §7.2", "Q/XX-2024-11 §5.3"),
    ),
    AssetSpec(
        path=Path("samples/assets/vibration_spectrum.png"),
        doc_type="频谱图",
        equipment_id="A-101",
        version="v1",
        standard_refs=("ISO 10816-3:2009 §7.3",),
    ),
)


def _resolve(spec: AssetSpec, root: Path) -> AssetSpec:
    path = spec.path if spec.path.is_absolute() else root / spec.path
    return AssetSpec(
        path=path,
        doc_type=spec.doc_type,
        equipment_id=spec.equipment_id,
        version=spec.version,
        standard_refs=spec.standard_refs,
    )


def build_catalog_manifest(root: Path | None = None) -> list[DataAsset]:
    base = root or Path.cwd()
    return build_manifest(_resolve(spec, base) for spec in CATALOG)


def write_manifest(assets: list[DataAsset], target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest_payload(assets), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:] or [Path.cwd()])[0])
    try:
        assets = build_catalog_manifest(root)
    except FileNotFoundError as exc:
        print(f"缺少样例资产：{exc}", file=sys.stderr)
        return 2

    target = write_manifest(assets, root / "samples" / "assets" / "manifest.json")
    print(f"manifest: {target}  ({len(assets)} 项)")
    for asset in assets:
        equipment = asset.equipment_id or "-"
        print(
            f"  {asset.asset_id:<36} {asset.modality.value:<8} "
            f"{asset.doc_type:<6} {equipment:<6} {asset.summary[:40]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
