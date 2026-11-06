"""Command line entry point.

Four verbs, because those are the four things actually done with an index:
build it, ask it something, measure it, inspect it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from .corpus import DEFAULT_EXCLUDES, load_corpus, load_registered_corpus
from .embedding import EmbedderError, build_embedder
from .eval import cases_from_corpus, compare
from .rerank import RerankError, build_reranker
from .search import HybridSearcher
from .store import (
    IndexStateError,
    build_bundle,
    default_index_dir,
    load_bundle,
    save_bundle,
    verify_bundle,
)
from .types import ChunkKind, Filter


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            continue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m decision_poc.rag",
        description="Hybrid retrieval: BM25 + vectors + RRF + rerank.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="chunk a corpus, embed it and write an index")
    build.add_argument("--root", default=".", help="corpus root (default: cwd)")
    build.add_argument("--out", default=None, help="index directory")
    build.add_argument("--embed", choices=["api", "hash"], default=None)
    build.add_argument("--no-code", action="store_true", help="index markdown only")
    build.add_argument("--exclude", action="append", default=None, help="directory name to skip")
    build.add_argument(
        "--registered",
        action="store_true",
        help="按业务资产台账登记（asset_id/locator 与台账一致，供 evidence_locate 直接消费）",
    )
    build.add_argument("--quiet", action="store_true")

    query = sub.add_parser("query", help="search an index")
    query.add_argument("text")
    query.add_argument("--index", default=None)
    query.add_argument("--top", type=int, default=5)
    query.add_argument("--candidates", type=int, default=50)
    query.add_argument("--embed", choices=["api", "hash"], default=None)
    query.add_argument("--rerank", choices=["heuristic", "api", "none"], default="heuristic")
    query.add_argument("--path-prefix", default=None, help="only search paths starting with this")
    query.add_argument("--kind", choices=["text", "code"], default=None)
    query.add_argument("--no-parent", action="store_true", help="show the chunk, not its section")
    query.add_argument("--json", action="store_true")

    evaluation = sub.add_parser("eval", help="measure configurations on auto-generated cases")
    evaluation.add_argument("--index", default=None)
    evaluation.add_argument("--embed", choices=["api", "hash"], default=None)
    evaluation.add_argument("--rerank", choices=["heuristic", "api", "none"], default="heuristic")
    evaluation.add_argument("--top-k", type=int, default=10)
    evaluation.add_argument("--limit", type=int, default=0, help="cap the number of cases")
    evaluation.add_argument("--include-code", action="store_true")

    stats = sub.add_parser("stats", help="show an index manifest")
    stats.add_argument("--index", default=None)
    return parser


def _index_dir(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "index", None) or getattr(args, "out", None)
    return Path(explicit) if explicit else default_index_dir(Path.cwd())


def _cmd_build(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    if args.registered:
        # 台账口径：asset_id / locator 与 samples/assets/urban 的登记完全一致，
        # 检索命中可以直接拼成 evidence_ref 交给 evidence_locate 复核。
        corpus = load_registered_corpus(args.root if args.root != "." else None)
    else:
        corpus = load_corpus(
            args.root,
            excludes=DEFAULT_EXCLUDES + tuple(args.exclude or ()),
            include_code=not args.no_code,
        )
    if corpus.stats is None or not corpus.chunks:
        print("语料为空，没有可索引的内容", file=sys.stderr)
        return 2
    stats = corpus.stats
    print(
        f"语料：{stats.documents} 个文件 / {stats.chunks} 个 chunk / {stats.parents} 个 parent"
        f"（文本 {stats.text_chunks}，代码 {stats.code_chunks}，均长 {stats.avg_chars} 字）"
    )
    if stats.unparsed:
        print(f"降级处理 {len(stats.unparsed)} 个文件（前 3 个）：", file=sys.stderr)
        for item in stats.unparsed[:3]:
            print(f"  - {item}", file=sys.stderr)

    embedder = build_embedder(args.embed)
    print(f"embedder：{embedder.name}")

    interactive = sys.stdout.isatty()

    def progress(done: int, total: int) -> None:
        if args.quiet or not interactive:
            return
        width = 24
        filled = int(width * done / total) if total else width
        bar = "#" * filled + "." * (width - filled)
        sys.stdout.write(f"\r嵌入 [{bar}] {done}/{total}")
        sys.stdout.flush()

    bundle = build_bundle(corpus, embedder, progress=progress)
    if interactive and not args.quiet:
        sys.stdout.write("\n")
    target = save_bundle(bundle, _index_dir(args))
    elapsed = time.perf_counter() - started
    dim = bundle.vectors.shape[1] if bundle.vectors.size else 0
    print(f"索引已写入 {target}（{dim} 维，耗时 {elapsed:.1f}s）")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    bundle = load_bundle(_index_dir(args))
    embedder = build_embedder(args.embed)
    for warning in verify_bundle(bundle, embedder):
        print(f"警告：{warning}", file=sys.stderr)
    reranker = None if args.rerank == "none" else build_reranker(args.rerank)
    searcher = HybridSearcher(bundle, embedder, reranker=reranker)

    filter = None
    if args.path_prefix or args.kind:
        filter = Filter(
            path_prefix=args.path_prefix,
            kind=ChunkKind(args.kind) if args.kind else None,
        )

    response = searcher.search(
        args.text,
        top_k=args.top,
        candidates=args.candidates,
        filter=filter,
        use_parent=not args.no_parent,
    )

    if args.json:
        print(response.model_dump_json(indent=2))
        return 0

    trace = response.trace
    print(
        f"query: {response.query}\n"
        f"通道：lexical {trace.lexical_candidates} / vector {trace.vector_candidates} / "
        f"融合 {trace.fused_candidates} / rerank {trace.reranker}\n"
    )
    if not response.hits:
        print("没有命中。")
        return 0

    for position, hit in enumerate(response.hits, start=1):
        breadcrumb = " > ".join(hit.heading_path) if hit.heading_path else "(无标题)"
        signals = " ".join(f"{key}={value:.2f}" for key, value in hit.signals.items())
        print(f"{position}. score={hit.score:.4f}  {hit.source_path}  [{hit.kind.value}]")
        print(f"   {breadcrumb}")
        print(f"   {hit.locators[0]}")
        print(f"   {signals}")
        body = hit.context_text.strip()
        preview = body if len(body) <= 400 else body[:400] + " …"
        for line in preview.splitlines():
            print(f"   | {line}")
        print()
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    bundle = load_bundle(_index_dir(args))
    embedder = build_embedder(args.embed)
    for warning in verify_bundle(bundle, embedder):
        print(f"警告：{warning}", file=sys.stderr)
    reranker = None if args.rerank == "none" else build_reranker(args.rerank)
    searcher = HybridSearcher(bundle, embedder, reranker=reranker)

    cases, skipped = cases_from_corpus(
        bundle.chunks, bundle.parents, include_code=args.include_code
    )
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有生成出评测用例（语料里的标题太短或小节过大）", file=sys.stderr)
        return 2

    print(f"评测用例：{len(cases)} 条（跳过 {skipped} 个小节）")
    print(f"embedder：{embedder.name} / reranker：{searcher.reranker.name if searcher.reranker else 'none'}")
    print()
    report = compare(searcher, cases, k=args.top_k)
    header = f"{'config':<16}{'hit@1':>8}{'recall@' + str(args.top_k):>12}{'mrr@10':>10}{'n':>6}"
    print(header)
    print("-" * len(header))
    for result in report.results:
        print(
            f"{result.name:<16}{result.hit_at_1:>8.4f}{result.recall_at_k:>12.4f}"
            f"{result.mrr_at_10:>10.4f}{result.evaluated:>6}"
        )
    best = max(report.results, key=lambda item: item.recall_at_k)
    print()
    print(f"recall@{args.top_k} 最高：{best.name}（{best.recall_at_k:.4f}）")
    if embedder.name.startswith("hash"):
        print("注意：hash embedder 无语义，向量通道的分数不可当作检索质量结论。")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    bundle = load_bundle(_index_dir(args))
    print(json.dumps(bundle.manifest, ensure_ascii=False, indent=2))
    for warning in verify_bundle(bundle):
        print(f"警告：{warning}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8()
    args = _parser().parse_args(argv)
    handlers = {
        "build": _cmd_build,
        "query": _cmd_query,
        "eval": _cmd_eval,
        "stats": _cmd_stats,
    }
    try:
        return handlers[args.command](args)
    except (IndexStateError, EmbedderError, RerankError, NotADirectoryError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
