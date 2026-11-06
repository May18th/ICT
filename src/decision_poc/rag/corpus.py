"""Corpus loading: one representation per source type, one shared child/parent split.

Two source kinds need different treatment and the reason is worth stating:

- Markdown splits on headings. ``chunking.chunk_text`` already does this with
  exact line locators, so it is reused rather than reimplemented; a second
  chunker would drift from it.
- Python splits on AST symbols. Splitting code by character count decapitates
  functions, which is why every symbol becomes its own unit and only oversized
  symbols get cut further.

Both paths produce the same shape: small chunks to match on, larger parents to
read from. A parent is never synthesised text; it is a literal slice of the
source file, so every locator stays auditable.
"""

from __future__ import annotations

import ast
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from ..chunking import chunk_text
from ..data_asset import ENCODINGS, DataAsset, identify_asset
from .types import ChunkKind, CorpusStats, RagChunk, RagParent

MAX_PARENT_CHARS = 2400
MAX_CODE_CHARS = 1600
CODE_WINDOW_LINES = 60
TEXT_PATTERNS = ("*.md",)
CODE_PATTERNS = ("*.py",)
DEFAULT_EXCLUDES = (
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "pytest-of-",
    # 以下目录都是"同一份文档的另一份拷贝"，索引进来会让同一内容占多个坑：
    # chunk_id 由内容派生（`chk-{digest(asset_id, qualname, body)}`），副本与正本同 id，
    # 于是检索结果里重复占位、评测 recall 被重复计数放大（实测 264/1010 用例 recall>1）。
    ".worktrees",
    ".superpowers",
    "artifacts",
    ".rag_index",
    "deploy-webdemo",
)
_SYMBOL_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass
class Corpus:
    chunks: list[RagChunk] = field(default_factory=list)
    parents: list[RagParent] = field(default_factory=list)
    stats: CorpusStats | None = None


def _decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _digest(*parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _iter_files(root: Path, patterns: tuple[str, ...], excludes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if any(part in excludes for part in parts):
                continue
            found.append(path)
    return sorted(set(found))


def _read_source(path: Path) -> str:
    return _decode(path.read_bytes())


def _relocate(locators: list[str], asset_locator: str, rel: str) -> list[str]:
    """Rewrite ``asset locator#line:a-b`` as ``path#line:a-b``.

    The asset locator identifies the document but not where it lives, and a
    citation you cannot open is not a citation.
    """
    prefix = f"{asset_locator}#line:"
    relocated: list[str] = []
    for locator in locators:
        relocated.append(f"{rel}#line:{locator[len(prefix):]}" if locator.startswith(prefix) else locator)
    return relocated or [rel]


# --------------------------------------------------------------------------- markdown


def _text_units(
    path: Path, root: Path, doc_type: str, *, asset: DataAsset | None = None
) -> tuple[list[RagChunk], list[RagParent], str | None]:
    rel = path.relative_to(root).as_posix()
    try:
        text = _read_source(path)
    except OSError as exc:
        return [], [], f"{rel}: {exc}"

    # 传入已登记资产时不再自行识别：索引与台账必须给出同一个 asset_id / locator，
    # 否则检索命中拼不出可核验的 evidence_ref（`AS-UNK-*` 与 `AS-A-03-*` 对不上）。
    asset = asset or identify_asset(path, doc_type=doc_type, version="v1")
    raw_chunks = chunk_text(asset, text, markdown=True)
    if not raw_chunks:
        return [], [], None

    groups: OrderedDict[tuple[str, ...], list] = OrderedDict()
    for chunk in raw_chunks:
        groups.setdefault(tuple(chunk.heading_path), []).append(chunk)

    chunks: list[RagChunk] = []
    parents: list[RagParent] = []
    parent_of: dict[str, str] = {}

    for heading_path, members in groups.items():
        merged = "\n\n".join(member.text for member in members)
        if len(members) > 1 and len(merged) <= MAX_PARENT_CHARS:
            shared_id = f"par-{_digest(asset.asset_id, *heading_path)}"
            parents.append(
                RagParent(
                    parent_id=shared_id,
                    asset_id=asset.asset_id,
                    source_path=rel,
                    kind=ChunkKind.TEXT,
                    heading_path=list(heading_path),
                    text=merged,
                    locators=[loc for member in members for loc in member.locators],
                    child_ids=[member.chunk_id for member in members],
                )
            )
            parent_of.update({member.chunk_id: shared_id for member in members})
        else:
            # One chunk, or a section too long to merge safely: the child is its own parent.
            for member in members:
                own_id = f"par-{_digest(asset.asset_id, member.chunk_id)}"
                parents.append(
                    RagParent(
                        parent_id=own_id,
                        asset_id=asset.asset_id,
                        source_path=rel,
                        kind=ChunkKind.TEXT,
                        heading_path=list(heading_path),
                        text=member.text,
                        locators=member.locators,
                        child_ids=[member.chunk_id],
                    )
                )
                parent_of[member.chunk_id] = own_id

    for member in raw_chunks:
        chunks.append(
            RagChunk(
                chunk_id=member.chunk_id,
                asset_id=asset.asset_id,
                parent_id=parent_of[member.chunk_id],
                source_path=rel,
                kind=ChunkKind.TEXT,
                heading_path=list(member.heading_path),
                text=member.text,
                locators=member.locators,
                symbols=[],
                token_estimate=member.token_estimate,
            )
        )
    return chunks, parents, None


# --------------------------------------------------------------------------- python


@dataclass(frozen=True)
class _Unit:
    qualname: str
    start: int
    end: int
    depth: int


def _symbol_start(node: ast.AST) -> int:
    starts = [getattr(node, "lineno", 1)]
    for decorator in getattr(node, "decorator_list", []):
        starts.append(getattr(decorator, "lineno", starts[0]))
    return min(starts)


def _collect_units(node: ast.AST, prefix: str, depth: int, out: list[_Unit]) -> None:
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _SYMBOL_NODES):
            continue
        name = f"{prefix}{child.name}"
        end = getattr(child, "end_lineno", None) or getattr(child, "lineno", 1)
        out.append(_Unit(qualname=name, start=_symbol_start(child), end=end, depth=depth))
        if isinstance(child, ast.ClassDef):
            _collect_units(child, f"{name}.", depth + 1, out)


def _slice(lines: list[str], start: int, end: int) -> str:
    start = max(1, start)
    end = min(len(lines), end)
    if end < start:
        return ""
    return "\n".join(lines[start - 1 : end])


def _code_units(
    path: Path, root: Path, doc_type: str
) -> tuple[list[RagChunk], list[RagParent], str | None]:
    rel = path.relative_to(root).as_posix()
    try:
        text = _read_source(path)
    except OSError as exc:
        return [], [], f"{rel}: {exc}"

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return _code_fallback(text, rel, doc_type, f"{rel}: syntax error at line {exc.lineno}")

    asset = identify_asset(path, doc_type=doc_type, version="v1")
    lines = text.splitlines()
    units: list[_Unit] = []
    _collect_units(tree, "", 0, units)
    units.sort(key=lambda unit: unit.start)

    chunks: list[RagChunk] = []
    parents: list[RagParent] = []

    def add_chunk(body: str, start: int, end: int, qualname: str, parent_id: str) -> None:
        if not body.strip():
            return
        chunk_id = f"chk-{_digest(asset.asset_id, qualname, body)}"
        chunks.append(
            RagChunk(
                chunk_id=chunk_id,
                asset_id=asset.asset_id,
                parent_id=parent_id,
                source_path=rel,
                kind=ChunkKind.CODE,
                heading_path=[qualname] if qualname else [],
                text=body,
                locators=[f"{asset.locator}#line:{start}-{end}"],
                symbols=[qualname] if qualname else [],
                token_estimate=max(1, (len(body) + 1) // 2),
            )
        )

    header_end = (units[0].start - 1) if units else len(lines)
    header_body = _slice(lines, 1, header_end)
    if header_body.strip():
        parent_id = f"par-{_digest(asset.asset_id, 'module-header')}"
        parents.append(
            RagParent(
                parent_id=parent_id,
                asset_id=asset.asset_id,
                source_path=rel,
                kind=ChunkKind.CODE,
                heading_path=["<module>"],
                text=header_body,
                locators=[f"{asset.locator}#line:1-{header_end or 1}"],
            )
        )
        add_chunk(header_body, 1, header_end or 1, "", parent_id)
        parents[-1].child_ids = [chunk.chunk_id for chunk in chunks]

    for unit in units:
        body = _slice(lines, unit.start, unit.end)
        if not body.strip():
            continue
        parent_id = f"par-{_digest(asset.asset_id, unit.qualname)}"
        parents.append(
            RagParent(
                parent_id=parent_id,
                asset_id=asset.asset_id,
                source_path=rel,
                kind=ChunkKind.CODE,
                heading_path=[unit.qualname],
                text=body,
                locators=[f"{asset.locator}#line:{unit.start}-{unit.end}"],
            )
        )
        if len(body) <= MAX_CODE_CHARS:
            add_chunk(body, unit.start, unit.end, unit.qualname, parent_id)
        else:
            for start, end in _split_code(body, unit.start):
                add_chunk(_slice(lines, start, end), start, end, unit.qualname, parent_id)
        parents[-1].child_ids = [chunk.chunk_id for chunk in chunks if chunk.parent_id == parent_id]

    return chunks, parents, None


def _split_code(body: str, offset: int) -> list[tuple[int, int]]:
    """Split an oversized symbol on line windows, keeping absolute line numbers."""
    body_lines = body.splitlines()
    spans: list[tuple[int, int]] = []
    for index in range(0, len(body_lines), CODE_WINDOW_LINES):
        start = offset + index
        end = offset + min(index + CODE_WINDOW_LINES, len(body_lines)) - 1
        spans.append((start, end))
    return spans


def _code_fallback(
    text: str, rel: str, doc_type: str, warning: str
) -> tuple[list[RagChunk], list[RagParent], str | None]:
    """Unparsable file: keep it citable via line windows rather than dropping it."""
    lines = text.splitlines()
    if not lines:
        return [], [], warning
    digest = hashlib.sha256(f"{rel}".encode("utf-8")).hexdigest()[:16]
    parents: list[RagParent] = []
    chunks: list[RagChunk] = []
    for index in range(0, len(lines), CODE_WINDOW_LINES):
        start = index + 1
        end = min(index + CODE_WINDOW_LINES, len(lines))
        body = "\n".join(lines[index:end])
        if not body.strip():
            continue
        parent_id = f"par-{digest}-{start}"
        parents.append(
            RagParent(
                parent_id=parent_id,
                asset_id=f"AS-UNKNOWN-{doc_type}-{digest}",
                source_path=rel,
                kind=ChunkKind.CODE,
                heading_path=[],
                text=body,
                locators=[f"{rel}#line:{start}-{end}"],
                child_ids=[f"chk-{digest}-{start}"],
            )
        )
        chunks.append(
            RagChunk(
                chunk_id=f"chk-{digest}-{start}",
                asset_id=f"AS-UNKNOWN-{doc_type}-{digest}",
                parent_id=parent_id,
                source_path=rel,
                kind=ChunkKind.CODE,
                heading_path=[],
                text=body,
                locators=[f"{rel}#line:{start}-{end}"],
                symbols=[],
                token_estimate=max(1, (len(body) + 1) // 2),
            )
        )
    return chunks, parents, warning


# --------------------------------------------------------------------------- entry point


def load_corpus(
    root: str | Path,
    *,
    text_doc_type: str = "tech_doc",
    code_doc_type: str = "source_code",
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    include_code: bool = True,
) -> Corpus:
    """Walk ``root`` and return chunks, parents and stats in one pass."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"corpus root is not a directory: {base}")

    chunks: list[RagChunk] = []
    parents: list[RagParent] = []
    unparsed: list[str] = []

    for path in _iter_files(base, TEXT_PATTERNS, excludes):
        found_chunks, found_parents, warning = _text_units(path, base, text_doc_type)
        chunks.extend(found_chunks)
        parents.extend(found_parents)
        if warning:
            unparsed.append(warning)

    if include_code:
        for path in _iter_files(base, CODE_PATTERNS, excludes):
            found_chunks, found_parents, warning = _code_units(path, base, code_doc_type)
            chunks.extend(found_chunks)
            parents.extend(found_parents)
            if warning:
                unparsed.append(warning)

    chunks.sort(key=lambda chunk: (chunk.source_path, chunk.chunk_id))
    parents.sort(key=lambda parent: (parent.source_path, parent.parent_id))
    stats = CorpusStats(
        documents=int(len({chunk.source_path for chunk in chunks})),
        chunks=len(chunks),
        parents=len(parents),
        code_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.CODE),
        text_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.TEXT),
        avg_chars=round(sum(len(chunk.text) for chunk in chunks) / len(chunks), 1) if chunks else 0.0,
        unparsed=unparsed,
    )
    return Corpus(chunks=chunks, parents=parents, stats=stats)


# --------------------------------------------------------------------------- registered corpus

#: 台账里有、但没有可切分文本的资产（示意图等），检索层直接跳过。
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}


def load_registered_corpus(dataset_root: str | Path | None = None) -> Corpus:
    """Build a corpus from the business asset ledger instead of walking a tree.

    Why this exists
    ---------------
    ``load_corpus`` calls ``identify_asset`` on whatever it finds, which yields
    ``AS-UNK-*`` ids for exactly the files the ledger registers as ``AS-A-03-*``.
    A retrieval hit is only useful when it can be turned into an ``evidence_ref``
    the caller can hand to ``evidence_locate``, so the index has to reuse the
    ledger's own registration: same ``asset_id``, same ``locator``.
    """
    from ..urban_dataset import DEFAULT_DATASET_ROOT, URBAN_ASSETS

    base = Path(dataset_root).resolve() if dataset_root else DEFAULT_DATASET_ROOT
    chunks: list[RagChunk] = []
    parents: list[RagParent] = []
    unparsed: list[str] = []

    for spec in URBAN_ASSETS:
        path = base / spec.file_name
        if not path.is_file():
            unparsed.append(f"{spec.file_name}: 台账已登记但文件不存在")
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        asset = identify_asset(
            path,
            doc_type=spec.asset_type,
            equipment_id=spec.scope,
            version=spec.version,
            standard_refs=spec.standard_refs,
        )
        found_chunks, found_parents, warning = _text_units(path, base, spec.asset_type, asset=asset)
        chunks.extend(found_chunks)
        parents.extend(found_parents)
        if warning:
            unparsed.append(warning)

    chunks.sort(key=lambda chunk: (chunk.source_path, chunk.chunk_id))
    parents.sort(key=lambda parent: (parent.source_path, parent.parent_id))
    stats = CorpusStats(
        documents=int(len({chunk.source_path for chunk in chunks})),
        chunks=len(chunks),
        parents=len(parents),
        code_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.CODE),
        text_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.TEXT),
        avg_chars=round(sum(len(chunk.text) for chunk in chunks) / len(chunks), 1) if chunks else 0.0,
        unparsed=unparsed,
    )
    return Corpus(chunks=chunks, parents=parents, stats=stats)
