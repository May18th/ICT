"""Index persistence.

The on-disk format is deliberately boring: two JSONL files and one ``.npy``.
That makes an index diffable, greppable and inspectable with tools the project
already has, and it means a broken index is diagnosed by reading a file rather
than by re-running a build.

The manifest records the embedder fingerprint and a corpus fingerprint. Both are
there to answer one question at query time: *is this index still describing the
text and the model that produced it?* A silent mismatch here is the single most
common way a retrieval system degrades without anyone noticing.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .corpus import Corpus
from .embedding import Embedder
from .types import RagChunk, RagParent

INDEX_VERSION = 1
CHUNKS_FILE = "chunks.jsonl"
PARENTS_FILE = "parents.jsonl"
VECTORS_FILE = "vectors.npy"
MANIFEST_FILE = "manifest.json"

ProgressFn = Callable[[int, int], None]


class IndexStateError(RuntimeError):
    """Index missing, unreadable or of an incompatible version."""


@dataclass
class Bundle:
    chunks: list[RagChunk]
    parents: list[RagParent]
    vectors: np.ndarray
    manifest: dict

    @property
    def parent_map(self) -> dict[str, RagParent]:
        return {parent.parent_id: parent for parent in self.parents}


def corpus_fingerprint(chunks: list[RagChunk]) -> str:
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda item: item.chunk_id):
        digest.update(chunk.chunk_id.encode("utf-8"))
        digest.update(chunk.text.encode("utf-8"))
    return digest.hexdigest()[:32]


def build_bundle(
    corpus: Corpus,
    embedder: Embedder,
    *,
    progress: ProgressFn | None = None,
) -> Bundle:
    if not corpus.chunks:
        raise IndexStateError("corpus produced no chunks; nothing to index")

    texts = [chunk.text for chunk in corpus.chunks]
    vectors = _embed_in_batches(embedder, texts, progress)
    if vectors.shape[0] != len(corpus.chunks):
        raise IndexStateError(
            f"embedder returned {vectors.shape[0]} vectors for {len(corpus.chunks)} chunks"
        )

    manifest = {
        "version": INDEX_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "embedder": {
            "name": embedder.name,
            "dim": int(vectors.shape[1]) if vectors.size else 0,
            "fingerprint": embedder.fingerprint(),
        },
        "counts": {"chunks": len(corpus.chunks), "parents": len(corpus.parents)},
        "corpus_fingerprint": corpus_fingerprint(corpus.chunks),
        "stats": corpus.stats.model_dump() if corpus.stats else {},
    }
    return Bundle(
        chunks=list(corpus.chunks),
        parents=list(corpus.parents),
        vectors=vectors,
        manifest=manifest,
    )


def _embed_in_batches(embedder: Embedder, texts: list[str], progress: ProgressFn | None) -> np.ndarray:
    step = 64
    blocks: list[np.ndarray] = []
    for start in range(0, len(texts), step):
        block = embedder.embed_documents(texts[start : start + step])
        blocks.append(block)
        if progress:
            progress(min(start + step, len(texts)), len(texts))
    if not blocks:
        return np.zeros((0, 0), dtype=np.float32)
    return np.vstack(blocks).astype(np.float32)


def save_bundle(bundle: Bundle, directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    _write_jsonl(target / CHUNKS_FILE, [chunk.model_dump() for chunk in bundle.chunks])
    _write_jsonl(target / PARENTS_FILE, [parent.model_dump() for parent in bundle.parents])
    np.save(target / VECTORS_FILE, bundle.vectors)
    payload = dict(bundle.manifest)
    payload["counts"] = {
        "chunks": len(bundle.chunks),
        "parents": len(bundle.parents),
    }
    (target / MANIFEST_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load_bundle(directory: str | Path) -> Bundle:
    target = Path(directory)
    manifest_path = target / MANIFEST_FILE
    if not manifest_path.is_file():
        raise IndexStateError(f"no index at {target} (run `build` first)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if version != INDEX_VERSION:
        raise IndexStateError(f"index version {version} != expected {INDEX_VERSION}; rebuild it")

    chunks = [RagChunk.model_validate(row) for row in _read_jsonl(target / CHUNKS_FILE)]
    parents = [RagParent.model_validate(row) for row in _read_jsonl(target / PARENTS_FILE)]
    vectors = np.load(target / VECTORS_FILE)
    if vectors.shape[0] != len(chunks):
        raise IndexStateError(
            f"vector count {vectors.shape[0]} does not match chunk count {len(chunks)}"
        )
    return Bundle(chunks=chunks, parents=parents, vectors=vectors, manifest=manifest)


def verify_bundle(bundle: Bundle, embedder: Embedder | None = None) -> list[str]:
    """Return human-readable warnings; empty list means the index is consistent."""
    warnings: list[str] = []
    recorded = bundle.manifest.get("embedder", {})
    if embedder is not None and recorded.get("fingerprint") != embedder.fingerprint():
        warnings.append(
            f"index built with {recorded.get('fingerprint')} but query embedder is "
            f"{embedder.fingerprint()}; vector scores are meaningless until you rebuild"
        )
    recorded_dim = recorded.get("dim")
    if recorded_dim and bundle.vectors.size and bundle.vectors.shape[1] != recorded_dim:
        warnings.append(
            f"vector width {bundle.vectors.shape[1]} != manifest dim {recorded_dim}"
        )
    if corpus_fingerprint(bundle.chunks) != bundle.manifest.get("corpus_fingerprint"):
        warnings.append("chunk text no longer matches the manifest fingerprint; rebuild recommended")
    return warnings


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise IndexStateError(f"missing index file: {path}")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def default_index_dir(project_root: str | Path) -> Path:
    return Path(os.environ.get("RAG_INDEX_DIR", Path(project_root) / "artifacts" / "rag_index"))
