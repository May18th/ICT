#!/usr/bin/env python3
"""台账同步状态：查平台知识库里那两篇台账文档的实际状态，落成本地快照。

**为什么要单独一步**：`/evolution` 页要显示"平台上的台账是不是最新"，但页面进程不该为了
一个状态显示去登录平台、更不该在平台没起时整页报错。这里把状态查出来落成文件，页面只读文件。

**判断"是否落后"**：拿本地 `platform_versions.json` 的 `generated_at` 与平台上
`platform-versions://snapshot` 文档的 `create_time` 比——快照比文档新，说明改了还没同步。

**只读**：只调 GET，不写平台。

用法：
    export NEXENT_SUPER_ADMIN_PASSWORD=...
    python deploy/nexent/check_ledger_sync.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_knowledge import http, signin  # noqa: E402  复用同一套登录/请求

ROOT = Path(__file__).resolve().parents[2]
INDEX_CACHE = ROOT / "artifacts" / "policy" / "ledger_index_id.txt"
PLATFORM_SNAPSHOT = ROOT / "artifacts" / "ledger" / "platform_versions.json"
OUT = ROOT / "artifacts" / "ledger" / "ledger_sync.json"
INDEX_NAME = "urban-policy-ledger"


def _iso_from_ms(value: object) -> str | None:
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_iso(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(jwt: str, index_id: str) -> dict:
    status, body = http(f"/api/indices/{index_id}/files", None, "GET", jwt)
    if status != 200:
        raise RuntimeError(f"查询索引文件失败 {status}: {str(body)[:200]}")
    # http() 对 GET 返回的是原始字符串，不是解析好的对象——这里补一次解析
    payload: object = body
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"索引文件接口返回的不是 JSON：{str(body)[:200]}") from exc
    raw = payload.get("files") if isinstance(payload, dict) else None
    documents: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        documents.append(
            {
                "path_or_url": item.get("path_or_url"),
                "file": item.get("file"),
                "status": item.get("status"),
                "chunk_count": item.get("chunk_count"),
                "platform_created_at": _iso_from_ms(item.get("create_time")),
                "has_error": bool(item.get("has_error_info")),
                "error_reason": item.get("error_reason"),
            }
        )
    documents.sort(key=lambda doc: str(doc.get("path_or_url") or ""))

    local_generated = None
    if PLATFORM_SNAPSHOT.is_file():
        try:
            local_generated = json.loads(
                PLATFORM_SNAPSHOT.read_text(encoding="utf-8")
            ).get("generated_at")
        except (UnicodeDecodeError, json.JSONDecodeError):
            local_generated = None

    # 只对平台版本那篇判定落后：策略台账由 history.jsonl 驱动，判据不同
    snapshot_doc = next(
        (doc for doc in documents if str(doc.get("path_or_url", "")).startswith("platform-versions://")),
        None,
    )
    stale = False
    compared_at = None
    if snapshot_doc and local_generated:
        local_dt = _parse_iso(local_generated)
        remote_dt = _parse_iso(snapshot_doc.get("platform_created_at"))
        compared_at = {"local_snapshot": local_generated, "platform_document": snapshot_doc.get("platform_created_at")}
        if local_dt and remote_dt:
            stale = local_dt > remote_dt

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index_name": INDEX_NAME,
        "index_id": index_id,
        "documents": documents,
        "document_count": len(documents),
        "compared_at": compared_at,
        "stale": stale,
        "hint": (
            "本地快照比平台文档新，需要重新同步：python deploy/nexent/sync_policy_ledger.py --apply"
            if stale
            else "平台文档不落后于本地快照"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="台账同步状态（只读）")
    parser.add_argument("--out", default=str(OUT), help="落盘路径")
    parser.add_argument("--json", action="store_true", help="只打印，不写文件")
    args = parser.parse_args(argv)

    if not INDEX_CACHE.is_file():
        print(f"没有索引缓存 {INDEX_CACHE}，先跑 sync_policy_ledger.py --apply", file=sys.stderr)
        return 1
    index_id = INDEX_CACHE.read_text(encoding="utf-8").strip()
    if not index_id:
        print("索引缓存为空", file=sys.stderr)
        return 1

    try:
        payload = collect(signin(), index_id)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 探针要能给出可读原因
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"索引 {payload['index_name']} -> {payload['index_id']}，文档 {payload['document_count']} 篇")
    for doc in payload["documents"]:
        print(
            f"  - {doc['file']:<22} {str(doc['status']):<10} chunks={doc['chunk_count']}  "
            f"{doc['platform_created_at'] or '—'}  {doc['path_or_url']}"
        )
    print(f"落后判定：{'落后，需重新同步' if payload['stale'] else '不落后'}（{payload['hint']}）")
    print(f"状态已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
