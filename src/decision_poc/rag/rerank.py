"""Reranking: the highest-value single stage in the pipeline.

Two backends:

- ``api`` calls an OpenAI-compatible ``/rerank`` endpoint (bge-reranker-v2-m3 on
  SiliconFlow, Jina, Cohere v2). A cross-encoder reads query and document
  together, which is why it fixes rankings a bi-encoder cannot.
- ``heuristic`` runs offline and scores three things: how much of the query the
  document covers, whether the query words appear in the breadcrumb, and the
  fused retrieval score. It is weaker than a cross-encoder but strictly stronger
  than trusting the fusion order.

Heading coverage deserves its own weight rather than being folded into body
coverage: when a section is literally titled after the question, that is far
more evidence than the same words appearing anywhere in the body.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

try:  # 可选依赖：只有云端 rerank 通道需要它
    import httpx
except ImportError:  # pragma: no cover - 最小镜像里确实没有 httpx
    httpx = None  # type: ignore[assignment]

from .lexical import coverage, document_tokens, tokenize

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
MAX_DOC_CHARS = 3000
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
COVERAGE_WEIGHT = 0.5
HEADING_WEIGHT = 0.35
PRIOR_WEIGHT = 0.15


class RerankError(RuntimeError):
    pass


@dataclass(frozen=True)
class RerankItem:
    text: str
    heading: str
    prior: float


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, items: list[RerankItem], top_k: int) -> list[tuple[int, float]]: ...


class IdentityReranker:
    """Keeps fusion order. Useful when measuring what reranking actually buys."""

    name = "none"

    def rerank(self, query: str, items: list[RerankItem], top_k: int) -> list[tuple[int, float]]:
        return [(index, item.prior) for index, item in enumerate(items)][:top_k]


class HeuristicReranker:
    name = "heuristic"

    def rerank(self, query: str, items: list[RerankItem], top_k: int) -> list[tuple[int, float]]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return [(index, item.prior) for index, item in enumerate(items)][:top_k]

        scored: list[tuple[int, float]] = []
        for index, item in enumerate(items):
            body = document_tokens(item.text, [], [])
            body_cover = coverage(query_tokens, body)
            head_cover = coverage(query_tokens, tokenize(item.heading)) if item.heading else 0.0
            value = (
                COVERAGE_WEIGHT * body_cover
                + HEADING_WEIGHT * head_cover
                + PRIOR_WEIGHT * item.prior
            )
            scored.append((index, value))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]


class ApiReranker:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.model = model or os.environ.get("RAG_RERANK_MODEL", DEFAULT_MODEL)
        self.base_url = (
            base_url
            or os.environ.get("RAG_RERANK_BASE_URL")
            or os.environ.get("RAG_EMBED_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = (
            api_key or os.environ.get("RAG_RERANK_API_KEY") or os.environ.get("RAG_EMBED_API_KEY", "")
        )
        self.timeout = timeout
        self.name = f"api:{self.model}"
        if not self.api_key:
            raise RerankError(
                "RAG_RERANK_API_KEY / RAG_EMBED_API_KEY is not set; use --rerank heuristic"
            )

    def rerank(self, query: str, items: list[RerankItem], top_k: int) -> list[tuple[int, float]]:
        if not items:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": [item.text[:MAX_DOC_CHARS] for item in items],
            "top_n": min(top_k, len(items)),
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        f"{self.base_url}/rerank", json=payload, headers=headers
                    )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise RerankError(f"rerank endpoint returned {response.status_code}")
                response.raise_for_status()
                results = response.json().get("results") or []
                pairs = [
                    (int(row.get("index", position)), float(row.get("relevance_score", 0.0)))
                    for position, row in enumerate(results)
                ]
                pairs.sort(key=lambda pair: (-pair[1], pair[0]))
                return pairs[:top_k]
            except (httpx.HTTPError, KeyError, ValueError, RerankError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_SECONDS * (2**attempt))
        raise RerankError(f"rerank failed after {MAX_RETRIES} attempts: {last_error}")


def build_reranker(kind: str | None = None, **kwargs: object) -> Reranker:
    resolved = (kind or os.environ.get("RAG_RERANK_KIND", "heuristic")).strip().lower()
    if resolved in {"none", "off", "identity"}:
        return IdentityReranker()
    if resolved == "api":
        return ApiReranker(**kwargs)  # type: ignore[arg-type]
    return HeuristicReranker()
