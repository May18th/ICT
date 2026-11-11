#!/usr/bin/env python3
"""把策略演进台账同步进 Nexent 知识库，让平台上的 Agent 能回答"当前策略是什么版本"。

**为什么单独建一个索引**：`urban-flood-assets` 装的是业务资产事实（泵站、道路、组织职责），
策略演进是"元数据"。混进去以后问"泵站流量"会命中"v1.2 偏差 0"，检索质量直接垮掉。

**方向是单向的**：本地 `artifacts/feedback/history.jsonl` 是唯一事实源，平台侧只是镜像。
同步失败不影响本地闭环，也不会反向改写任何本地数据。

**幂等**：文档 `path_or_url` 用 `policy-ledger://<final_label>`，同一版本重复同步会在 ES 里
产生同键文档；用 `--latest-only` 时只同步最新一条，避免台账被灌成几十条重复。

用法：
    python deploy/nexent/sync_policy_ledger.py --dry-run
    python deploy/nexent/sync_policy_ledger.py --apply --latest-only
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_knowledge import http, signin  # noqa: E402  复用同一套登录/请求

HISTORY = Path(__file__).resolve().parents[2] / "artifacts" / "feedback" / "history.jsonl"
INDEX_NAME = "urban-policy-ledger"
EMBEDDING_MODEL_ID = 3


def read_runs() -> list[dict]:
    if not HISTORY.is_file():
        return []
    rows: list[dict] = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 半截行跳过，不让一条坏记录拖垮同步
    return rows


def latest_per_label(runs: list[dict]) -> list[dict]:
    """每个 final_label 只留一条：取**首次达成该版本**的那次记录。

    不能简单取"最后一次出现"——同一版本反复提交时会命中记忆，那一轮的
    train_drop 是 0、采纳轮次是 0，写进台账会变成"这一版什么都没做"，
    把真正的收益（首次收敛时 0.2264→0）盖掉。
    """
    best: dict[str, dict] = {}
    for run in runs:
        label = run.get("final_label")
        if not label:
            continue
        current = best.get(label)
        # 降幅更大者优先；打平时保留较早的一条（首次达成）
        if current is None or float(run.get("train_drop") or 0) > float(
            current.get("train_drop") or 0
        ):
            best[label] = run
    # 按版本号升序，读起来是"从 v1.1 走到 v1.2"
    return sorted(best.values(), key=lambda r: str(r.get("final_label")))


def to_doc(run: dict) -> dict:
    label = run.get("final_label") or "unknown"
    ts = str(run.get("ts") or "")[:19].replace("T", " ")
    train = run.get("train_drop")
    holdout = run.get("holdout_drop")
    review_before = run.get("review_before")
    review_after = run.get("review_after")

    lines = [
        f"# 策略演进台账：{label}",
        "",
        f"- 策略标签：{label}",
        f"- 最近一次触发：{ts}（UTC）",
        f"- 触发反馈：{run.get('feedback_id')}",
        f"- 现场输入：{run.get('district')} 水深 {run.get('depth_cm')}cm，"
        f"判定 {run.get('outcome')}，建议维度 {run.get('dimension') or '无'}",
        f"- 训练集偏差降幅：{train if train is not None else '—'}",
        f"- 留出集偏差降幅：{holdout if holdout is not None else '—'}",
        f"- 专家评审偏差："
        f"{(f'{review_before} → {review_after}') if review_before is not None else '—'}",
        f"- 采纳轮次：{run.get('promoted_rounds')}",
        "",
        "说明：偏差越低表示策略与该场景的处置基准越接近。"
        "训练集 8 例、留出集 4 例，评测集为冻结基准，不随反馈变动。",
    ]
    content = "\n".join(lines)
    return {
        "title": f"策略演进台账 {label}",
        "content": content,
        "filename": "policy-ledger.md",
        "path_or_url": f"policy-ledger://{label}",
        "source_type": "text",
    }


INDEX_CACHE = Path(__file__).resolve().parents[2] / "artifacts" / "policy" / "ledger_index_id.txt"


def ensure_index(jwt: str) -> str | None:
    """建索引，返回真实 id（`N-<uuid>` 形式；用知识库名去写会 404）。

    索引 id 没法从接口按名字反查（`GET /api/indices` 只给 id 列表，`check_exist` 只给状态），
    所以首次创建后把 id 缓存到 `artifacts/policy/ledger_index_id.txt`，后续复用。
    缓存失效（索引被删）时打一次 `GET /{id}/files` 探测，404 就重建。
    """
    if INDEX_CACHE.is_file():
        cached = INDEX_CACHE.read_text(encoding="utf-8").strip()
        if cached:
            st_probe, _ = http(f"/api/indices/{cached}/files", None, "GET", jwt)
            if st_probe == 200:
                return cached
            print(f"缓存索引 {cached} 已失效（{st_probe}），重建")

    st, body = http(
        f"/api/indices/{INDEX_NAME}", {"embedding_model_id": EMBEDDING_MODEL_ID}, "POST", jwt
    )
    index_id = None
    if st == 200:
        try:
            index_id = json.loads(body).get("id")
        except json.JSONDecodeError:
            index_id = None
    elif st == 409:
        # 已存在但没缓存 id：只能让操作者手工指定，不猜。
        print(
            f"索引 {INDEX_NAME} 已存在但没有缓存 id。\n"
            f"跑一次下面这条查到 id，写进 {INDEX_CACHE}：\n"
            f"  curl -s $BASE/api/indices → 逐个 GET /api/indices/<id>/files 试，"
            f"或看 nexent.knowledge_record_t.index_name",
        )
        return None
    else:
        print(f"建索引失败 {st}: {body[:200]}")
        return None

    if index_id:
        INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_CACHE.write_text(index_id + "\n", encoding="utf-8")
        print(f"索引 id 已缓存：{INDEX_CACHE}")
    return index_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入；不加只打印")
    ap.add_argument("--dry-run", action="store_true", help="与不加 --apply 等价，显式声明用")
    ap.add_argument("--latest-only", action="store_true", help="只同步最新版本一条")
    ap.add_argument("--limit", type=int, default=0, help="最多同步几条（0=不限）")
    ap.add_argument("--no-replace", dest="replace", action="store_false",
                    help="不先删同键旧文档（默认会删，保证索引里每个版本只有一条）")
    args = ap.parse_args()

    runs = read_runs()
    if not runs:
        print(f"没有台账记录：{HISTORY} 不存在或为空")
        return 1

    docs = [to_doc(run) for run in latest_per_label(runs)]
    if args.latest_only:
        docs = docs[-1:]
    if args.limit > 0:
        docs = docs[-args.limit:]

    print(f"台账 {len(runs)} 条 → 按版本归并 {len(docs)} 条待同步")
    for doc in docs:
        print(f"  - {doc['title']}  ({doc['path_or_url']})")

    if not args.apply:
        print("\n（dry-run，未写入。加 --apply 才真正落到 Nexent）")
        return 0

    jwt = signin()
    index_id = ensure_index(jwt)
    if not index_id:
        return 1
    print(f"\n索引就绪：{INDEX_NAME} -> {index_id}")

    # 先按 path_or_url 删同键旧文档，否则每次同步都在索引里堆一条同版本记录，
    # 检索时会返回一堆内容不一致的"同一版本"。删不到（404）属正常，直接忽略。
    if args.replace:
        for doc in docs:
            key = urllib.parse.quote(doc["path_or_url"], safe="")
            st_del, _ = http(
                f"/api/indices/{index_id}/documents?path_or_url={key}&scope=full",
                None,
                "DELETE",
                jwt,
            )
            print(f"  清理旧文档 {doc['path_or_url']} -> {st_del}")

    st, body = http(f"/api/indices/{index_id}/documents", docs, "POST", jwt)
    if st != 200:
        print(f"写入失败 {st}: {body[:300]}")
        return 1
    print(f"写入完成：{st} {body[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
