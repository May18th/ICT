"""Contracts for the hybrid retrieval layer.

Three fields carry the whole design:

- ``locators`` keeps every chunk pointing back at a real file and line range, so
  an answer can be audited instead of trusted.
- ``parent_id`` lets retrieval match on a small unit but hand the model a larger
  one. This is the cheapest fix for the "chunk is too small to answer from"
  failure mode, and it costs nothing at build time.
- ``signals`` records the per-channel score, so a bad ranking can be diagnosed
  query by query instead of guessed at.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ..contracts import StrictModel


class ChunkKind(StrEnum):
    TEXT = "text"
    CODE = "code"


class RagChunk(StrictModel):
    """The unit that gets indexed and matched."""

    chunk_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    kind: ChunkKind
    heading_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)
    locators: list[str] = Field(min_length=1)
    symbols: list[str] = Field(default_factory=list)
    token_estimate: int = Field(ge=1)

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.heading_path) if self.heading_path else self.source_path


class RagParent(StrictModel):
    """The unit that gets handed to the model after a child is matched."""

    parent_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    kind: ChunkKind
    heading_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)
    locators: list[str] = Field(min_length=1)
    child_ids: list[str] = Field(default_factory=list)


class Hit(StrictModel):
    chunk_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    #: 资产号，与台账（``urban_dataset``）同一口径。拼 ``asset_id#locator`` 就是
    #: ``evidence_locate`` 认的 ``evidence_ref``，检索结果因此可以直接被引用核验。
    asset_id: str = Field(default="")
    source_path: str = Field(min_length=1)
    kind: ChunkKind
    heading_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)
    context_text: str = Field(min_length=1)
    locators: list[str] = Field(min_length=1)
    score: float
    signals: dict[str, float] = Field(default_factory=dict)


class Filter(StrictModel):
    """Metadata pre-filter, applied before ranking rather than after."""

    path_prefix: str | None = None
    kind: ChunkKind | None = None


class SearchTrace(StrictModel):
    """Diagnostics for one query. Cheap to compute, expensive to guess."""

    lexical_candidates: int = 0
    vector_candidates: int = 0
    fused_candidates: int = 0
    reranked_candidates: int = 0
    reranker: str = "none"
    embedder: str = "none"


class SearchResponse(StrictModel):
    query: str = Field(min_length=1)
    hits: list[Hit] = Field(default_factory=list)
    trace: SearchTrace = Field(default_factory=SearchTrace)


class CorpusStats(StrictModel):
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    parents: int = Field(ge=0)
    code_chunks: int = Field(ge=0)
    text_chunks: int = Field(ge=0)
    avg_chars: float = Field(ge=0)
    unparsed: list[str] = Field(default_factory=list)
