"""Lexical retrieval: BM25 over a tokenizer tuned for Chinese technical text.

Why not a word-segmentation library: this corpus mixes Chinese prose with
identifiers like ``evidence_check`` and ``vd02``, and a dictionary segmenter
splits the identifiers wrong while still needing a domain dictionary for the
prose. Character bigrams plus identifier splitting needs neither, and it is
what makes the lexical channel worth having next to the vector channel:

- ``evidence_check`` also matches ``evidence`` and ``check``.
- ``数据门禁`` matches on the overlapping bigram ``门禁`` even when the query
  only says ``门禁``.

The index is rebuilt from chunks at load time rather than persisted. At this
corpus size that costs well under a second and removes an entire class of
"stale side-car file" bugs.
"""

from __future__ import annotations

import re

import numpy as np

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:[.:]\d+)*|[\u4e00-\u9fff]+")
_IDENT_SPLIT_RE = re.compile(r"_+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
_CJK_START = "\u4e00"
_CJK_END = "\u9fff"


def _is_cjk(char: str) -> bool:
    return _CJK_START <= char <= _CJK_END


def _split_identifier(raw: str) -> list[str]:
    """``get_UserID`` -> [get, user, id, get_userid]."""
    parts: list[str] = []
    for piece in _IDENT_SPLIT_RE.split(raw):
        if not piece:
            continue
        pieces = _CAMEL_RE.findall(piece)
        if pieces:
            parts.extend(sub.lower() for sub in pieces)
        else:
            parts.append(piece.lower())
    whole = raw.lower()
    if len(parts) > 1 and whole not in parts:
        parts.append(whole)
    return parts


def _cjk_grams(sequence: str) -> list[str]:
    """Unigrams carry short queries; bigrams carry most of the signal."""
    grams = list(sequence)
    grams.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return grams


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group()
        head = raw[0]
        if _is_cjk(head):
            tokens.extend(_cjk_grams(raw))
        elif head.isascii() and (head.isalpha() or head == "_"):
            tokens.extend(_split_identifier(raw))
        else:
            tokens.append(raw.lower())
    return tokens


def document_tokens(text: str, heading_path: list[str], symbols: list[str]) -> list[str]:
    """Body tokens plus one extra pass over the breadcrumb.

    The extra pass is a cheap field boost: a chunk whose heading is exactly what
    the query asks about should outrank a chunk that merely mentions the words.
    """
    tokens = tokenize(text)
    for label in [*heading_path, *symbols]:
        tokens.extend(tokenize(label))
    return tokens


class BM25Index:
    """Sparse BM25 with an inverted index kept as numpy arrays."""

    def __init__(
        self,
        documents: list[list[str]],
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.size = len(documents)
        self.k1 = k1
        self.b = b
        self.doc_len = np.zeros(self.size, dtype=np.float32)
        postings: dict[str, tuple[list[int], list[float]]] = {}
        for doc_id, tokens in enumerate(documents):
            self.doc_len[doc_id] = len(tokens)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                doc_ids, tfs = postings.setdefault(token, ([], []))
                doc_ids.append(doc_id)
                tfs.append(float(count))
        self.avgdl = float(self.doc_len.mean()) if self.size and self.doc_len.mean() else 1.0
        self._postings: dict[str, tuple[np.ndarray, np.ndarray]] = {
            token: (np.asarray(ids, dtype=np.int32), np.asarray(tfs, dtype=np.float32))
            for token, (ids, tfs) in postings.items()
        }
        self._idf: dict[str, float] = {
            token: float(np.log(1.0 + (self.size - len(ids) + 0.5) / (len(ids) + 0.5)))
            for token, (ids, _) in self._postings.items()
        }

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        scores = np.zeros(self.size, dtype=np.float32)
        if not self.size:
            return scores
        for token in dict.fromkeys(query_tokens):
            entry = self._postings.get(token)
            if entry is None:
                continue
            doc_ids, tfs = entry
            idf = self._idf[token]
            norm = self.k1 * (1.0 - self.b + self.b * self.doc_len[doc_ids] / self.avgdl)
            scores[doc_ids] += idf * tfs * (self.k1 + 1.0) / (tfs + norm)
        return scores

    def top_k(self, scores: np.ndarray, k: int) -> list[tuple[int, float]]:
        if k <= 0 or not scores.size:
            return []
        k = min(k, scores.size)
        # argpartition first: sorting the whole array for a top-50 is wasted work.
        candidates = np.argpartition(-scores, k - 1)[:k]
        ordered = candidates[np.argsort(-scores[candidates])]
        return [(int(index), float(scores[index])) for index in ordered if scores[index] > 0]


def coverage(query_tokens: list[str], text_tokens: list[str]) -> float:
    """Share of distinct query terms present in the document.

    Used by the offline reranker as a second signal next to BM25: BM25 rewards
    repetition of one rare term, coverage rewards answering the whole question.
    """
    wanted = set(query_tokens)
    if not wanted:
        return 0.0
    return len(wanted & set(text_tokens)) / len(wanted)
