"""Heading-aware chunking with exact source-line locators.

Two decisions worth stating:

- Chunks never cross a heading. A chunk that spans "5 处置顺序" and "6 职责索引"
  makes every downstream citation ambiguous, which is exactly the failure mode
  the provenance layer is supposed to prevent.
- Identical text is merged into one chunk with **multiple locators** (`locators`,
  not `locator`), because the same sentence often appears in several documents
  and each occurrence must stay citable.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import Field

from .contracts import StrictModel
from .data_asset import DataAsset

MAX_CHARS = 400
SENTENCE_BREAKS = "。；;！？!?\n"


class Chunk(StrictModel):
    chunk_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)
    token_estimate: int = Field(ge=1)
    locators: list[str] = Field(min_length=1)
    content_hash: str = Field(min_length=1)


def _content_hash(text: str) -> str:
    material = " ".join(text.split())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    """Chinese-heavy estimate: ~2 chars per token. Explicitly an estimate."""
    return max(1, (len(text) + 1) // 2)


def _split_lines(lines: list[tuple[int, str]], max_chars: int) -> list[tuple[int, int, list[str]]]:
    """Pack lines into (start_line, end_line, lines) pieces under max_chars."""
    pieces: list[tuple[int, int, list[str]]] = []
    buffer: list[str] = []
    start = lines[0][0] if lines else 1
    end = start
    length = 0
    for line_no, text in lines:
        addition = len(text) + 1
        if buffer and length + addition > max_chars:
            pieces.append((start, end, buffer))
            buffer, length = [], 0
            start = line_no
        buffer.append(text)
        end = line_no
        length += addition
    if buffer:
        pieces.append((start, end, buffer))
    return pieces


def _split_long_line(line_no: int, text: str, max_chars: int) -> list[tuple[int, int, str]]:
    """Split a single oversized line on sentence boundaries, same line number."""
    pieces: list[tuple[int, int, str]] = []
    buffer = ""
    for char in text:
        buffer += char
        if len(buffer) >= max_chars and char in SENTENCE_BREAKS:
            pieces.append((line_no, line_no, buffer.strip()))
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        pieces.append((line_no, line_no, remainder))
    return [(start, end, text) for start, end, text in pieces if text]


def chunk_text(
    asset: DataAsset,
    text: str,
    *,
    max_chars: int = MAX_CHARS,
    markdown: bool = True,
) -> list[Chunk]:
    """Chunk a text asset; `markdown=False` treats every line as body text."""
    lines = text.splitlines()
    if not lines:
        return []

    sections: list[tuple[list[str], list[tuple[int, str]]]] = []
    stack: list[tuple[int, str]] = []
    current: list[tuple[int, str]] = []

    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if markdown and stripped.startswith("#"):
            if current:
                sections.append(([title for _, title in stack], current))
                current = []
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        if not stripped:
            continue
        current.append((line_no, stripped))
    if current:
        sections.append(([title for _, title in stack], current))

    merged: dict[str, Chunk] = {}
    for heading_path, section_lines in sections:
        for start, end, piece_lines in _split_lines(section_lines, max_chars):
            body = "\n".join(piece_lines).strip()
            if not body:
                continue
            body_pieces = (
                _split_long_line(start, body, max_chars)
                if len(body) > max_chars and len(piece_lines) == 1
                else [(start, end, body)]
            )
            for piece_start, piece_end, piece_text in body_pieces:
                digest = _content_hash(piece_text)
                locator = f"{asset.locator}#line:{piece_start}-{piece_end}"
                if digest in merged:
                    if locator not in merged[digest].locators:
                        merged[digest].locators.append(locator)
                    continue
                merged[digest] = Chunk(
                    chunk_id=f"chk-{digest}",
                    asset_id=asset.asset_id,
                    heading_path=list(heading_path),
                    text=piece_text,
                    token_estimate=_estimate_tokens(piece_text),
                    locators=[locator],
                    content_hash=digest,
                )

    return list(merged.values())


def chunk_stats(chunks: list[Chunk]) -> dict[str, Any]:
    if not chunks:
        return {"chunks": 0, "avg_chars": 0, "max_chars": 0, "multi_locator": 0}
    sizes = [len(chunk.text) for chunk in chunks]
    return {
        "chunks": len(chunks),
        "avg_chars": round(sum(sizes) / len(sizes), 1),
        "max_chars": max(sizes),
        "multi_locator": sum(1 for chunk in chunks if len(chunk.locators) > 1),
    }
