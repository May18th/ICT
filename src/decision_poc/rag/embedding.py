"""Embedding backends.

The layer is pluggable for one practical reason: the index is only useful if it
can be rebuilt the day the provider changes, so nothing above this file may
assume a specific vendor.

Two implementations ship:

- ``api`` talks to any OpenAI-compatible ``/embeddings`` endpoint (SiliconFlow,
  DashScope compatible mode, OpenAI, a local vLLM). This is the one to use.
- ``hash`` is a deterministic character-n-gram projection. It carries no
  semantics at all and exists only so the pipeline, the store format and the CLI
  can be exercised without a key. Its name says so in ``fingerprint()``, which
  the store records, so an index built with it can never be mistaken for a real
  one.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Protocol

import numpy as np

try:  # 可选依赖：只有云端 embedding 通道需要它，离线 hash 模式不该为它付代价
    import httpx
except ImportError:  # pragma: no cover - 最小镜像里确实没有 httpx
    httpx = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "BAAI/bge-m3"
EMBED_BATCH = 32
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
HASH_DIM = 256
HASH_NGRAM = 2


class EmbedderError(RuntimeError):
    """Raised when an embedding backend cannot produce vectors at all."""


class Embedder(Protocol):
    name: str
    dim: int | None

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...

    def fingerprint(self) -> str: ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


class ApiEmbedder:
    """OpenAI-compatible embeddings client."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        query_prefix: str = "",
        batch_size: int = EMBED_BATCH,
        timeout: float = 60.0,
    ) -> None:
        self.model = model or os.environ.get("RAG_EMBED_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("RAG_EMBED_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("RAG_EMBED_API_KEY", "")
        self.query_prefix = query_prefix or os.environ.get("RAG_EMBED_QUERY_PREFIX", "")
        self.batch_size = batch_size
        self.timeout = timeout
        self.name = f"api:{self.model}"
        self.dim: int | None = None
        if httpx is None:
            raise EmbedderError(
                "cloud embedding needs httpx, which is not installed in this "
                "environment; use --embed hash for the offline pipeline."
            )
        if not self.api_key:
            raise EmbedderError(
                "RAG_EMBED_API_KEY is not set. Set it, or pass --embed hash to "
                "exercise the pipeline offline (hash vectors carry no semantics)."
            )

    def fingerprint(self) -> str:
        return f"{self.name}|prefix={self.query_prefix}"

    def _post(self, batch: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        payload = {"model": self.model, "input": batch, "encoding_format": "float"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise EmbedderError(f"embedding endpoint returned {response.status_code}")
                response.raise_for_status()
                body = response.json()
                rows = sorted(body["data"], key=lambda row: row.get("index", 0))
                return [row["embedding"] for row in rows]
            except (httpx.HTTPError, KeyError, ValueError, EmbedderError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_SECONDS * (2**attempt))
        raise EmbedderError(f"embedding failed after {MAX_RETRIES} attempts: {last_error}")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        rows: list[list[float]] = []
        for index in range(0, len(texts), self.batch_size):
            batch = [text if text.strip() else " " for text in texts[index : index + self.batch_size]]
            rows.extend(self._post(batch))
        matrix = np.asarray(rows, dtype=np.float32)
        self.dim = int(matrix.shape[1])
        return _normalize(matrix)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([f"{self.query_prefix}{text}"])[0]


class HashEmbedder:
    """Deterministic n-gram hashing. Semantics: none. Purpose: offline smoke tests."""

    name = f"hash-{HASH_DIM}"
    dim = HASH_DIM

    def fingerprint(self) -> str:
        return f"{self.name}|ngram={HASH_NGRAM}|NO-SEMANTICS"

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(HASH_DIM, dtype=np.float32)
        compact = "".join(text.lower().split())
        if not compact:
            return vector
        for index in range(max(1, len(compact) - HASH_NGRAM + 1)):
            gram = compact[index : index + HASH_NGRAM]
            slot = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest(), "big")
            vector[slot % HASH_DIM] += 1.0
        return vector

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, HASH_DIM), dtype=np.float32)
        return _normalize(np.vstack([self._vector(text) for text in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


def build_embedder(kind: str | None = None, **kwargs: object) -> Embedder:
    """``api`` requires a key and fails loudly; ``hash`` must be asked for by name."""
    resolved = (kind or os.environ.get("RAG_EMBED_KIND", "")).strip().lower()
    if resolved == "hash":
        return HashEmbedder()
    if resolved == "api":
        return ApiEmbedder(**kwargs)  # type: ignore[arg-type]
    if os.environ.get("RAG_EMBED_API_KEY"):
        return ApiEmbedder(**kwargs)  # type: ignore[arg-type]
    return HashEmbedder()
