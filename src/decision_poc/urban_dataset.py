"""One-shot ingestion of the minimal urban waterlogging dataset (P1).

The demo dataset is 15 synthetic assets covering the three modalities the brief
requires (tables, documents, one schematic map). Every asset is registered
through `data_asset.identify_asset` so it gets a stable `asset_id` and a sha256
fingerprint, then attached to an `AssetGovernance` record carrying the fields the
brief mandates but `DataAsset` does not have (source org, publish time, validity
window, visibility, status).

    python -m decision_poc.urban_dataset            # writes manifest.json
    python -m decision_poc.urban_dataset --print    # prints instead of writing

Nothing here is real municipal data; see `samples/assets/urban/README.md`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .data_asset import DataAsset, identify_asset
from .governance import AssetGovernance, AssetStatus

MANIFEST_VERSION = "urban-1"
DEFAULT_DATASET_ROOT = Path(__file__).resolve().parents[2] / "samples" / "assets" / "urban"
TZ = "+08:00"


@dataclass(frozen=True)
class UrbanAssetSpec:
    file_name: str
    asset_type: str
    title: str
    source_org: str
    version: str
    published_at: str
    valid_from: str
    scope: str | None = None
    valid_to: str | None = None
    visibility: tuple[str, ...] = ()
    status: AssetStatus = AssetStatus.PUBLISHED
    standard_refs: tuple[str, ...] = ()
    note: str = ""


URBAN_ASSETS: tuple[UrbanAssetSpec, ...] = (
    UrbanAssetSpec(
        file_name="road_network.csv",
        asset_type="road_network",
        title="A-03 片区道路基础台账",
        source_org="区城建档案中心",
        version="v1",
        published_at="2026-01-15T09:00:00+08:00",
        valid_from="2026-01-15T00:00:00+08:00",
        scope="A-03",
        note="5 条道路，含高程与排水分区",
    ),
    UrbanAssetSpec(
        file_name="pipeline_ledger.csv",
        asset_type="pipeline_ledger",
        title="A-03 片区地下管线台账",
        source_org="区城建档案中心",
        version="v1",
        published_at="2026-02-01T09:00:00+08:00",
        valid_from="2026-02-01T00:00:00+08:00",
        scope="A-03",
        note="含排水/燃气/电力/通信/供水，管径与埋深可判风险",
    ),
    UrbanAssetSpec(
        file_name="drainage_facilities.csv",
        asset_type="drainage_facility",
        title="A-03 片区排水设施台账",
        source_org="区排水公司",
        version="v1",
        published_at="2026-09-10T09:00:00+08:00",
        valid_from="2026-09-10T00:00:00+08:00",
        scope="A-03",
        note="泵站与雨水口，DF-PS-01 运维主体空缺",
    ),
    UrbanAssetSpec(
        file_name="construction_plans.csv",
        asset_type="construction_plan",
        title="在建占道施工计划表",
        source_org="区道路管理中心",
        version="v3",
        published_at="2026-10-26T10:00:00+08:00",
        valid_from="2026-10-26T00:00:00+08:00",
        scope="A-03",
        note="CP-2026-041 燃气主干管更换，开挖 2.5m",
    ),
    UrbanAssetSpec(
        file_name="work_orders.csv",
        asset_type="work_order",
        title="设施维修工单记录",
        source_org="区道路管理中心",
        version="v1",
        published_at="2026-10-29T09:40:00+08:00",
        valid_from="2026-10-29T00:00:00+08:00",
        scope="A-03",
        note="含积水、泵站故障、基坑沉陷三类工单",
    ),
    UrbanAssetSpec(
        file_name="weather_alerts.csv",
        asset_type="weather_alert",
        title="气象预警发布记录",
        source_org="市气象台",
        version="v2",
        published_at="2026-10-29T07:30:00+08:00",
        valid_from="2026-10-29T07:30:00+08:00",
        scope="A-03",
        note="含一条已解除的黄色预警，用于过期证据测试",
    ),
    UrbanAssetSpec(
        file_name="department_duties.csv",
        asset_type="duty_list",
        title="部门职责清单",
        source_org="区应急管理局",
        version="v2",
        published_at="2026-01-05T09:00:00+08:00",
        valid_from="2026-01-05T00:00:00+08:00",
        scope="A-03",
        note="含封路权限交叉与泵站职责空缺",
    ),
    UrbanAssetSpec(
        file_name="resource_inventory.csv",
        asset_type="resource_inventory",
        title="防汛应急资源库存表",
        source_org="区应急管理局",
        version="v1",
        published_at="2026-10-01T09:00:00+08:00",
        valid_from="2026-10-01T00:00:00+08:00",
        scope="A-03",
        note="抽水泵 4 台，用于资源缺口核算",
    ),
    UrbanAssetSpec(
        file_name="plan_flood_emergency_v2.md",
        asset_type="emergency_plan",
        title="城区防汛排涝应急预案 v2.0",
        source_org="区应急管理局",
        version="v2.0",
        published_at="2025-05-20T09:00:00+08:00",
        valid_from="2025-06-01T00:00:00+08:00",
        scope="A-03",
        standard_refs=("YJ-FX-2025-002",),
        note="现行版，封路阈值 25cm，处置顺序先隔离风险源",
    ),
    UrbanAssetSpec(
        file_name="plan_flood_emergency_v1.md",
        asset_type="emergency_plan",
        title="城区防汛排涝应急预案 v1.0（已废止）",
        source_org="区应急管理局",
        version="v1.0",
        published_at="2021-04-15T09:00:00+08:00",
        valid_from="2021-05-01T00:00:00+08:00",
        valid_to="2025-06-01T00:00:00+08:00",
        scope="A-03",
        status=AssetStatus.DEPRECATED,
        standard_refs=("YJ-FX-2021-001",),
        note="封路阈值 30cm 且抽排优先，与 v2 冲突，用于过期与冲突测试",
    ),
    UrbanAssetSpec(
        file_name="procedure_waterlogging.md",
        asset_type="procedure",
        title="道路积水处置作业指引",
        source_org="区排水公司",
        version="v1.2",
        published_at="2026-03-10T09:00:00+08:00",
        valid_from="2026-03-15T00:00:00+08:00",
        scope="A-03",
        visibility=("ORG-DRAIN", "ORG-EMERG"),
        standard_refs=("ZY-DRN-2026-003",),
        note="内部文件，含移动泵折算口径",
    ),
    UrbanAssetSpec(
        file_name="procedure_gas_protection.md",
        asset_type="procedure",
        title="地下燃气管道施工保护规程",
        source_org="市燃气公司",
        version="v3.1",
        published_at="2025-09-01T09:00:00+08:00",
        valid_from="2025-09-15T00:00:00+08:00",
        scope="A-03",
        visibility=("ORG-GAS", "ORG-EMERG"),
        standard_refs=("GC-GAS-2025-011",),
        note="内部文件，排水公司不可见，用于权限过滤测试",
    ),
    UrbanAssetSpec(
        file_name="notice_road_closure.md",
        asset_type="public_notice",
        title="兴华路临时封闭公告",
        source_org="区道路管理中心",
        version="v1.0",
        published_at="2026-10-29T08:30:00+08:00",
        valid_from="2026-10-29T08:30:00+08:00",
        valid_to="2026-10-29T18:00:00+08:00",
        scope="R-101",
        standard_refs=("GG-JT-2026-1029-01",),
        note="绕行建议指向 R-103，而 R-103 正在占道施工",
    ),
    UrbanAssetSpec(
        file_name="notice_gas_construction.md",
        asset_type="public_notice",
        title="兴华路燃气管道施工公告",
        source_org="市燃气公司",
        version="v1.0",
        published_at="2026-10-27T16:00:00+08:00",
        valid_from="2026-10-29T08:00:00+08:00",
        valid_to="2026-10-29T18:00:00+08:00",
        scope="R-101",
        standard_refs=("GG-GAS-2026-041",),
        note="明确开挖 2.5m、检测频次 2 小时",
    ),
    UrbanAssetSpec(
        file_name="pipeline_map.png",
        asset_type="schematic_map",
        title="A-03 管线示意图",
        source_org="区城建档案中心",
        version="v1",
        published_at="2026-10-29T08:00:00+08:00",
        valid_from="2026-10-29T00:00:00+08:00",
        scope="A-03",
        note="示意图非地理配准；道路/管线台账为其结构化替代",
    ),
)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _governance_for(spec: UrbanAssetSpec, asset: DataAsset) -> AssetGovernance:
    return AssetGovernance(
        asset_type=spec.asset_type,
        title=spec.title,
        source_uri=f"demo://urban/{spec.file_name}",
        source_org=spec.source_org,
        published_at=_parse(spec.published_at),
        valid_from=_parse(spec.valid_from),
        valid_to=_parse(spec.valid_to) if spec.valid_to else None,
        visibility=list(spec.visibility),
        content_hash=asset.checksum_sha256,
        status=spec.status,
    )


def load_urban_assets(
    root: Path | str | None = None,
) -> list[tuple[DataAsset, AssetGovernance]]:
    """Register every dataset file; ordering is by asset_id for stable diffs."""
    base = Path(root) if root is not None else DEFAULT_DATASET_ROOT
    rows: list[tuple[DataAsset, AssetGovernance]] = []
    for spec in URBAN_ASSETS:
        path = base / spec.file_name
        asset = identify_asset(
            path,
            doc_type=spec.asset_type,
            equipment_id=spec.scope,
            version=spec.version,
            standard_refs=spec.standard_refs,
        )
        rows.append((asset, _governance_for(spec, asset)))
    return sorted(rows, key=lambda row: row[0].asset_id)


def build_urban_manifest(root: Path | str | None = None) -> dict[str, Any]:
    rows = load_urban_assets(root)
    type_counts: dict[str, int] = {}
    modality_counts: dict[str, int] = {}
    source_orgs: dict[str, int] = {}
    for asset, governance in rows:
        type_counts[governance.asset_type] = type_counts.get(governance.asset_type, 0) + 1
        modality_counts[asset.modality.value] = modality_counts.get(asset.modality.value, 0) + 1
        source_orgs[governance.source_org] = source_orgs.get(governance.source_org, 0) + 1
    return {
        "manifest_version": MANIFEST_VERSION,
        "asset_count": len(rows),
        "type_counts": dict(sorted(type_counts.items())),
        "modality_counts": dict(sorted(modality_counts.items())),
        "source_org_counts": dict(sorted(source_orgs.items())),
        "assets": [
            {
                **asset.model_dump(mode="json"),
                "governance": governance.model_dump(mode="json"),
            }
            for asset, governance in rows
        ],
    }


def write_urban_manifest(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else DEFAULT_DATASET_ROOT
    payload = build_urban_manifest(base)
    target = base / "manifest.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return target


def load_manifest_payload(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ingest the urban demo dataset")
    parser.add_argument("--root", default=None, help="dataset directory")
    parser.add_argument("--print", action="store_true", help="print summary only")
    args = parser.parse_args(argv)

    payload = build_urban_manifest(args.root)
    if args.print:
        print(json.dumps({k: v for k, v in payload.items() if k != "assets"}, ensure_ascii=False, indent=2))
        return 0
    target = write_urban_manifest(args.root)
    print(f"wrote {target}")
    print(
        f"assets={payload['asset_count']} types={len(payload['type_counts'])} "
        f"modalities={payload['modality_counts']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
