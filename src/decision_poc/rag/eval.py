"""Offline evaluation.

Building a labelled set by hand does not scale down to a weekend PoC, so the
labels come from the corpus structure instead: a section heading is a query that
a human actually wrote, and the chunks under it are the answer. That is a
biased sample (headings are shorter and cleaner than real questions), but it is
generated in seconds, it never drifts, and it is good enough to decide between
configurations, which is the only thing it is used for.

Reported per configuration, because "RAG works" is not a measurement:

- ``hit@1``      — did the single best result land on an answer chunk
- ``recall@k``   — how much of the section was recovered
- ``mrr@10``     — how high the first correct result sat
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .rerank import IdentityReranker
from .search import HybridSearcher
from .types import ChunkKind, RagChunk, RagParent

DEFAULT_MIN_QUERY_CHARS = 4
DEFAULT_MAX_GOLD = 6
DEFAULT_K = 10


@dataclass(frozen=True)
class EvalCase:
    query: str
    gold: frozenset[str]
    source_path: str
    origin: str


@dataclass
class ConfigResult:
    name: str
    hit_at_1: float
    recall_at_k: float
    mrr_at_10: float
    evaluated: int


@dataclass
class EvalReport:
    cases: int = 0
    skipped_short: int = 0
    results: list[ConfigResult] = field(default_factory=list)


def cases_from_corpus(
    chunks: list[RagChunk],
    parents: list[RagParent],
    *,
    min_query_chars: int = DEFAULT_MIN_QUERY_CHARS,
    max_gold: int = DEFAULT_MAX_GOLD,
    include_code: bool = False,
) -> tuple[list[EvalCase], int]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    merged: dict[str, set[str]] = {}
    origin: dict[str, tuple[str, str]] = {}
    skipped = 0

    for parent in parents:
        heading = parent.heading_path[-1] if parent.heading_path else ""
        if not heading or len(heading) < min_query_chars:
            skipped += 1
            continue
        if parent.kind is ChunkKind.CODE and not include_code:
            skipped += 1
            continue
        gold = {child for child in parent.child_ids if child in by_id}
        if not gold or len(gold) > max_gold:
            skipped += 1
            continue
        merged.setdefault(heading, set()).update(gold)
        origin.setdefault(heading, (parent.source_path, parent.kind.value))

    cases = [
        EvalCase(
            query=query,
            gold=frozenset(gold),
            source_path=origin[query][0],
            origin=origin[query][1],
        )
        for query, gold in sorted(merged.items())
        if len(gold) <= max_gold
    ]
    return cases, skipped


def _score(ranked: list[str], case: EvalCase, k: int) -> tuple[float, float, float]:
    """Score one case.

    ``ranked`` is de-duplicated first, because ``chunk_id`` comes from
    ``chunking._content_hash``: two documents containing the same paragraph share
    an id, so a single answer can occupy several result slots. Counting those
    twice pushed recall above 1.0 (264 of 1010 cases before the fix).
    """
    top = list(dict.fromkeys(ranked))[:k]
    hits = sum(1 for chunk_id in top if chunk_id in case.gold)
    recall = hits / len(case.gold) if case.gold else 0.0
    hit_at_1 = 1.0 if top and top[0] in case.gold else 0.0
    reciprocal = 0.0
    for position, chunk_id in enumerate(top[:10], start=1):
        if chunk_id in case.gold:
            reciprocal = 1.0 / position
            break
    return hit_at_1, recall, reciprocal


def run_config(
    name: str,
    searcher: HybridSearcher,
    cases: list[EvalCase],
    *,
    k: int = DEFAULT_K,
    channels: tuple[str, ...] = ("lexical", "vector"),
    reranker: object | None = None,
    use_parent: bool = False,
) -> ConfigResult:
    if not cases:
        return ConfigResult(name=name, hit_at_1=0.0, recall_at_k=0.0, mrr_at_10=0.0, evaluated=0)
    totals = [0.0, 0.0, 0.0]
    for case in cases:
        response = searcher.search(
            case.query,
            top_k=k,
            candidates=max(k * 5, 30),
            channels=channels,
            reranker=reranker,  # type: ignore[arg-type]
            use_parent=use_parent,
        )
        ranked = [hit.chunk_id for hit in response.hits]
        hit_at_1, recall, reciprocal = _score(ranked, case, k)
        totals[0] += hit_at_1
        totals[1] += recall
        totals[2] += reciprocal
    count = len(cases)
    return ConfigResult(
        name=name,
        hit_at_1=round(totals[0] / count, 4),
        recall_at_k=round(totals[1] / count, 4),
        mrr_at_10=round(totals[2] / count, 4),
        evaluated=count,
    )


def compare(searcher: HybridSearcher, cases: list[EvalCase], *, k: int = DEFAULT_K) -> EvalReport:
    """The four configurations worth knowing: each one isolates a single stage.

    ``IdentityReranker`` (not ``None``) is passed to the non-rerank rows on
    purpose: ``HybridSearcher.search`` treats ``reranker=None`` as "fall back to
    the searcher's own reranker", so passing ``None`` made ``hybrid`` and
    ``hybrid+rerank`` identical and hid whatever reranking actually buys.
    """
    identity = IdentityReranker()
    report = EvalReport(cases=len(cases))
    report.results = [
        run_config("vector-only", searcher, cases, k=k, channels=("vector",), reranker=identity),
        run_config("lexical-only", searcher, cases, k=k, channels=("lexical",), reranker=identity),
        run_config("hybrid", searcher, cases, k=k, reranker=identity),
        run_config("hybrid+rerank", searcher, cases, k=k),
    ]
    return report
