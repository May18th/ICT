"""Hybrid retrieval over a local corpus: BM25 + vectors + RRF + rerank.

Quick start::

    export RAG_EMBED_API_KEY=sk-...
    python -m decision_poc.rag build --root .
    python -m decision_poc.rag query "数据门禁阈值怎么配"
    python -m decision_poc.rag eval

Without a key, ``--embed hash`` exercises every stage with vectors that carry
no semantics. Rankings from that mode mean nothing; the plumbing checks out.
"""

from .corpus import Corpus, load_corpus
from .embedding import ApiEmbedder, Embedder, EmbedderError, HashEmbedder, build_embedder
from .eval import EvalCase, EvalReport, cases_from_corpus, compare, run_config
from .rerank import ApiReranker, HeuristicReranker, IdentityReranker, RerankItem, build_reranker
from .search import HybridSearcher
from .store import (
    Bundle,
    IndexStateError,
    build_bundle,
    default_index_dir,
    load_bundle,
    save_bundle,
    verify_bundle,
)
from .types import ChunkKind, CorpusStats, Filter, Hit, RagChunk, RagParent, SearchResponse

__all__ = [
    "ApiEmbedder",
    "ApiReranker",
    "Bundle",
    "ChunkKind",
    "Corpus",
    "CorpusStats",
    "Embedder",
    "EmbedderError",
    "EvalCase",
    "EvalReport",
    "Filter",
    "HashEmbedder",
    "HeuristicReranker",
    "Hit",
    "HybridSearcher",
    "IdentityReranker",
    "IndexStateError",
    "RagChunk",
    "RagParent",
    "RerankItem",
    "SearchResponse",
    "build_bundle",
    "build_embedder",
    "build_reranker",
    "cases_from_corpus",
    "compare",
    "default_index_dir",
    "load_bundle",
    "load_corpus",
    "run_config",
    "save_bundle",
    "verify_bundle",
]
