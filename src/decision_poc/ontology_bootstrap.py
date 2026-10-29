"""Run the ontology bootstrap over the sample catalogue.

    uv run --locked python -m decision_poc.ontology_bootstrap

Reads the registered data assets, induces candidate entities/relations with
provenance, and writes `samples/assets/ontology_candidates.json`. If a KG
snapshot exists at `samples/assets/kg_snapshot.json` the merge plan is printed
too, so the delta can be reviewed before touching the live graph.
"""

import json
import sys
from pathlib import Path

from .asset_manifest import build_catalog_manifest
from .ontology import build_bundle, bundle_payload, merge_summary, plan_merge

CANDIDATES = Path("samples") / "assets" / "ontology_candidates.json"
SNAPSHOT = Path("samples") / "assets" / "kg_snapshot.json"


def load_snapshot(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("entities", data) if isinstance(data, dict) else data
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:] or [Path.cwd()])[0])
    try:
        assets = build_catalog_manifest(root)
    except FileNotFoundError as exc:
        print(f"缺少样例资产：{exc}", file=sys.stderr)
        return 2

    bundle = build_bundle(assets, root)
    payload = bundle_payload(bundle, asset_count=len(assets))

    target = root / CANDIDATES
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    stats = payload["stats"]
    print(f"candidates: {target}")
    print(
        f"  资产 {stats['asset_count']} 份 → 实体 {stats['entity_count']} / "
        f"关系 {stats['relation_count']}"
    )
    print(
        "  决策分布 "
        + " ".join(f"{name}={count}" for name, count in stats["decision_counts"].items())
    )
    print(
        f"  溯源覆盖 {stats['provenance_rate']:.0%}  跨资产证据 {stats['cross_asset_candidates']} 条"
    )
    print(
        f"  类型覆盖 {stats['type_coverage']:.0%} "
        f"({len(stats['types_covered'])}/{stats['seed_type_count']})  "
        f"人工复核 {stats['review_load_per_asset']} 条/资产"
    )
    for warning in payload["warnings"][:8]:
        print(f"  ! {warning}")

    snapshot = load_snapshot(root / SNAPSHOT)
    if snapshot:
        ops = plan_merge(bundle, snapshot)
        print(f"  合并计划（对照 {len(snapshot)} 条快照）：{merge_summary(ops)}")
    else:
        print(f"  无快照（{SNAPSHOT} 不存在），全部按新增处理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
