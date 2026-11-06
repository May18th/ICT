"""Hybrid search: two recall channels, one fusion step, one optional rerank.

The design follows from a failure observed in single-channel systems:

- Vector-only recall misses exact tokens. Ask this corpus about ``vd02`` or
  ``evidence_check`` and a bi-encoder happily returns thematically similar text
  about something else.
- Lexical-only recall misses paraphrase. Ask about "排水能力不足" and BM25 will
  not find "内涝风险" even though they answer the same question.

Reciprocal Rank Fusion joins them without needing the two score scales to be
comparable, which is the part that usually goes wrong when people add
``lex + vec`` with hand-tuned weights.

Each hit keeps its child text (the precise match) next to its parent text (the
readable context). Retrieval precision and answer context are separate concerns
and conflating them is what forces the "chunk size" trade-off in naive systems.
"""

from __future__ import annotations

import numpy as np

from .embedding import Embedder
from .lexical import BM25Index, document_tokens
from .rerank import RerankItem, Reranker
from .store import Bundle
from .types import Filter, Hit, RagChunk, SearchResponse, SearchTrace

RRF_K = 60
DEFAULT_CANDIDATES = 50
LEXICAL = "lexical"
VECTOR = "vector"


class EmbedderMismatch(RuntimeError):
    pass


def _dedupe_by_chunk_id(
    fused: list[tuple[int, float, dict[str, float]]],
    chunks: list[RagChunk],
) -> list[tuple[int, float, dict[str, float]]]:
    """Keep the first occurrence of each ``chunk_id``; fused order is preserved."""
    seen: set[str] = set()
    kept: list[tuple[int, float, dict[str, float]]] = []
    for item in fused:
        chunk_id = chunks[item[0]].chunk_id
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        kept.append(item)
    return kept


class HybridSearcher:
    def __init__(
        self,
        bundle: Bundle,
        embedder: Embedder,
        *,
        reranker: Reranker | None = None,
        rrf_k: int = RRF_K,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> None:
        self.bundle = bundle
        self.embedder = embedder
        self.reranker = reranker
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight
        self.chunks = bundle.chunks
        self.parents = bundle.parent_map
        self.vectors = bundle.vectors

        if self.vectors.size and embedder.dim is not None and self.vectors.shape[1] != embedder.dim:
            raise EmbedderMismatch(
                f"index vectors are {self.vectors.shape[1]}-dim but embedder {embedder.name} "
                f"is {embedder.dim}-dim; rebuild the index"
            )

        self._tokens = [
            document_tokens(chunk.text, chunk.heading_path, chunk.symbols) for chunk in self.chunks
        ]
        self._bm25 = BM25Index(self._tokens)
        self._mask_cache: dict[tuple[str | None, str | None], np.ndarray] = {}

    # ------------------------------------------------------------------ helpers

    def _mask(self, filter: Filter | None) -> np.ndarray | None:
        if filter is None or (filter.path_prefix is None and filter.kind is None):
            return None
        key = (filter.path_prefix, filter.kind.value if filter.kind else None)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached
        keep = np.zeros(len(self.chunks), dtype=bool)
        for index, chunk in enumerate(self.chunks):
            if filter.path_prefix and not chunk.source_path.startswith(filter.path_prefix):
                continue
            if filter.kind is not None and chunk.kind != filter.kind:
                continue
            keep[index] = True
        self._mask_cache[key] = keep
        return keep

    @staticmethod
    def _top(scores: np.ndarray, keep: np.ndarray | None, k: int) -> list[tuple[int, float]]:
        if not scores.size or k <= 0:
            return []
        working = scores if keep is None else np.where(keep, scores, -np.inf)
        k = min(k, working.size)
        index = np.argpartition(-working, k - 1)[:k]
        index = index[np.argsort(-working[index])]
        return [(int(i), float(working[i])) for i in index if np.isfinite(working[i])]

    def _vector_scores(self, query: str) -> np.ndarray:
        if not self.vectors.size:
            return np.zeros(len(self.chunks), dtype=np.float32)
        query_vector = self.embedder.embed_query(query)
        if query_vector.shape[0] != self.vectors.shape[1]:
            raise EmbedderMismatch(
                f"query vector is {query_vector.shape[0]}-dim, index is "
                f"{self.vectors.shape[1]}-dim; rebuild the index"
            )
        # Both sides are L2-normalised, so the dot product already is cosine.
        return self.vectors @ query_vector.astype(np.float32)

    # ------------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        candidates: int = DEFAULT_CANDIDATES,
        filter: Filter | None = None,
        use_parent: bool = True,
        channels: tuple[str, ...] = (LEXICAL, VECTOR),
        reranker: Reranker | None = None,
    ) -> SearchResponse:
        if not query.strip():
            raise ValueError("query must not be blank")
        keep = self._mask(filter)
        active_reranker = reranker if reranker is not None else self.reranker

        query_tokens = document_tokens(query, [], [])
        lexical_ranked: list[tuple[int, float]] = []
        vector_ranked: list[tuple[int, float]] = []

        if LEXICAL in channels:
            lexical_scores = self._bm25.scores(query_tokens)
            lexical_ranked = self._top(lexical_scores, keep, candidates)
        if VECTOR in channels:
            vector_ranked = self._top(self._vector_scores(query), keep, candidates)

        fused = self._fuse(lexical_ranked, vector_ranked)
        if not fused:
            return SearchResponse(
                query=query,
                hits=[],
                trace=SearchTrace(
                    lexical_candidates=len(lexical_ranked),
                    vector_candidates=len(vector_ranked),
                    reranker=active_reranker.name if active_reranker else "none",
                    embedder=self.embedder.name,
                ),
            )

        # 同一段落出现在多个文件时 chunk_id 相同（`chunking._content_hash` 只哈希正文），
        # 不去重的话一份文档会占掉好几个结果位，rerank 也在给重复内容打分。
        fused = _dedupe_by_chunk_id(fused, self.chunks)
        shortlist = fused[:candidates]
        items = [
            RerankItem(
                text=self.chunks[index].text,
                heading=self.chunks[index].breadcrumb,
                prior=prior,
            )
            for index, prior, _ in shortlist
        ]
        ordered = (
            active_reranker.rerank(query, items, top_k)
            if active_reranker is not None
            else [(position, items[position].prior) for position in range(min(top_k, len(items)))]
        )

        hits = [
            self._build_hit(
                shortlist[position][0],
                score=score,
                signals={
                    "lexical": shortlist[position][2].get(LEXICAL, 0.0),
                    "vector": shortlist[position][2].get(VECTOR, 0.0),
                    "rrf": shortlist[position][1],
                },
                use_parent=use_parent,
            )
            for position, score in ordered
            if position < len(shortlist)
        ]

        return SearchResponse(
            query=query,
            hits=hits,
            trace=SearchTrace(
                lexical_candidates=len(lexical_ranked),
                vector_candidates=len(vector_ranked),
                fused_candidates=len(fused),
                reranked_candidates=len(ordered),
                reranker=active_reranker.name if active_reranker else "none",
                embedder=self.embedder.name,
            ),
        )

    def _fuse(
        self,
        lexical_ranked: list[tuple[int, float]],
        vector_ranked: list[tuple[int, float]],
    ) -> list[tuple[int, float, dict[str, float]]]:
        """Reciprocal Rank Fusion: scale-free, so no score normalisation is needed."""
        merged: dict[int, tuple[float, dict[str, float]]] = {}
        for channel, ranked, weight in (
            (LEXICAL, lexical_ranked, self.lexical_weight),
            (VECTOR, vector_ranked, self.vector_weight),
        ):
            best = ranked[0][1] if ranked else 0.0
            for rank, (index, score) in enumerate(ranked):
                contribution = weight / (self.rrf_k + rank + 1)
                total, signals = merged.get(index, (0.0, {}))
                signals[channel] = score / best if best else 0.0
                merged[index] = (total + contribution, signals)

        ordered = sorted(merged.items(), key=lambda pair: (-pair[1][0], pair[0]))
        peak = ordered[0][1][0] if ordered else 1.0
        return [
            (index, total / peak if peak else 0.0, signals)
            for index, (total, signals) in ordered
        ]

    def _build_hit(
        self,
        index: int,
        *,
        score: float,
        signals: dict[str, float],
        use_parent: bool,
    ) -> Hit:
        chunk = self.chunks[index]
        parent = self.parents.get(chunk.parent_id)
        context = parent.text if (use_parent and parent is not None) else chunk.text
        return Hit(
            chunk_id=chunk.chunk_id,
            parent_id=chunk.parent_id,
            asset_id=chunk.asset_id,
            source_path=chunk.source_path,
            kind=chunk.kind,
            heading_path=chunk.heading_path,
            text=chunk.text,
            context_text=context,
            locators=chunk.locators,
            score=round(score, 6),
            signals={key: round(value, 6) for key, value in signals.items()},
        )
