"""Uniform identification for multimodal data assets.

The contest scenario asks for heterogeneous files (text / table / image) to be
recognised, understood and given a citable identity. This module does exactly
that, with the standard library only:

- modality detection by suffix,
- structural summary per modality (table header + row count, text lead
  paragraph, PNG pixel size),
- stable `asset_id` + sha256 checksum so the same file is always citable,
- `standard_refs` and `locator` so a downstream answer can point at
  "ISO 10816-3:2009 §7.2" or "inspection_log.csv row:5".

It performs no network I/O and knows nothing about Nexent; the manifest it
produces is the hand-off contract for ingestion, retrieval and KG alignment.
"""

import csv
import hashlib
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from .contracts import StrictModel

MODALITY_BY_SUFFIX = {
    ".csv": "table",
    ".tsv": "table",
    ".md": "text",
    ".txt": "text",
    ".log": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".bmp": "image",
    ".webp": "image",
    ".pdf": "document",
    ".docx": "document",
    ".xlsx": "document",
    ".pptx": "document",
}

PARSED_SUFFIXES = {".csv", ".tsv", ".md", ".txt", ".log", ".png"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SUMMARY_CHARS = 160
ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")


class AssetModality(StrEnum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    DOCUMENT = "document"


class ParseStatus(StrEnum):
    PARSED = "parsed"
    METADATA_ONLY = "metadata_only"


class DataAsset(StrictModel):
    asset_id: str = Field(min_length=1)
    modality: AssetModality
    parse_status: ParseStatus
    file_name: str = Field(min_length=1)
    doc_type: str = Field(min_length=1)
    equipment_id: str | None = None
    version: str = Field(min_length=1)
    standard_refs: list[str] = Field(default_factory=list)
    locator: str = Field(min_length=1)
    checksum_sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=0)
    summary: str = Field(min_length=1)
    structure: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset_id", "file_name", "doc_type", "version", "locator", "summary")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("standard_refs")
    @classmethod
    def reject_blank_refs(cls, values: list[str]) -> list[str]:
        cleaned = []
        for item in values:
            text = str(item).strip()
            if not text:
                raise ValueError("standard_refs must not contain blank entries")
            cleaned.append(text)
        return cleaned


@dataclass(frozen=True)
class AssetSpec:
    """How a file should be registered; the file itself provides the rest."""

    path: Path
    doc_type: str
    equipment_id: str | None = None
    version: str = "v1"
    standard_refs: Sequence[str] = field(default_factory=tuple)


def detect_modality(path: Path) -> AssetModality:
    suffix = path.suffix.lower()
    raw = MODALITY_BY_SUFFIX.get(suffix, "document")
    return AssetModality(raw)


def _read_text(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _summarize_table(data: bytes, delimiter: str) -> tuple[str, dict[str, Any]]:
    reader = csv.reader(_read_text(data).splitlines(), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return "空表格：无有效行", {"rows": 0, "columns": 0, "headers": []}
    headers = [cell.strip() for cell in rows[0]]
    body = rows[1:]
    preview = "、".join(headers[:6]) or "无表头"
    summary = f"表格资产：{len(body)} 行 × {len(headers)} 列，列名 {preview}"
    if len(headers) > 6:
        summary += " 等"
    return summary, {
        "rows": len(body),
        "columns": len(headers),
        "headers": headers,
    }


def _summarize_text(data: bytes) -> tuple[str, dict[str, Any]]:
    text = _read_text(data)
    lines = text.splitlines()
    lead = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(">"):
            continue
        lead = stripped
        break
    if not lead:
        lead = "（无正文段落）"
    if len(lead) > SUMMARY_CHARS:
        lead = lead[:SUMMARY_CHARS] + "…"
    return f"文本资产：{len(lines)} 行，首段「{lead}」", {
        "lines": len(lines),
        "lead": lead,
    }


def _png_size(data: bytes) -> dict[str, int] | None:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return {"width": width, "height": height}


def _summarize_image(data: bytes) -> tuple[str, dict[str, Any]]:
    size = _png_size(data)
    if size:
        structure: dict[str, Any] = dict(size)
        summary = f"图片资产：{size['width']}×{size['height']} 像素（PNG）"
        return summary, structure
    return "图片资产：非 PNG 格式，仅登记元数据", {}


def _slug(value: str | None, fallback: str = "UNK") -> str:
    if not value:
        return fallback
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-_")
    return cleaned or fallback


def build_asset_id(*, equipment_id: str | None, doc_type: str, checksum: str) -> str:
    """Readable, stable identity: equipment + doc type + content fingerprint."""
    return f"AS-{_slug(equipment_id)}-{_slug(doc_type)}-{checksum[:8]}"


def identify_asset(
    path: str | Path,
    *,
    doc_type: str,
    equipment_id: str | None = None,
    version: str = "v1",
    standard_refs: Sequence[str] = (),
) -> DataAsset:
    """Read one file and return its citable asset record."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"asset file not found: {target}")

    data = target.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    modality = detect_modality(target)
    suffix = target.suffix.lower()

    structure: dict[str, Any] = {}
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        summary, structure = _summarize_table(data, delimiter)
    elif suffix in {".md", ".txt", ".log"}:
        summary, structure = _summarize_text(data)
    elif suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        summary, structure = _summarize_image(data)
    else:
        summary = f"文档资产：{suffix or '未知格式'}，仅登记元数据，正文由解析服务处理"

    parse_status = (
        ParseStatus.PARSED if suffix in PARSED_SUFFIXES else ParseStatus.METADATA_ONLY
    )
    equipment = (equipment_id or "").strip() or None
    locator_parts = [part for part in (equipment, _slug(doc_type), version) if part]
    return DataAsset(
        asset_id=build_asset_id(
            equipment_id=equipment, doc_type=doc_type, checksum=checksum
        ),
        modality=modality,
        parse_status=parse_status,
        file_name=target.name,
        doc_type=doc_type,
        equipment_id=equipment,
        version=version,
        standard_refs=list(standard_refs),
        locator="/".join(locator_parts),
        checksum_sha256=checksum,
        byte_size=len(data),
        summary=summary,
        structure=structure,
    )


def build_manifest(specs: Iterable[AssetSpec]) -> list[DataAsset]:
    """Register many files at once, ordered by asset id for stable diffs."""
    assets = [
        identify_asset(
            spec.path,
            doc_type=spec.doc_type,
            equipment_id=spec.equipment_id,
            version=spec.version,
            standard_refs=spec.standard_refs,
        )
        for spec in specs
    ]
    return sorted(assets, key=lambda asset: asset.asset_id)


def manifest_payload(assets: Sequence[DataAsset]) -> dict[str, Any]:
    """JSON-ready envelope, also usable as Nexent `meta_data`."""
    counts: dict[str, int] = {}
    for asset in assets:
        counts[asset.modality.value] = counts.get(asset.modality.value, 0) + 1
    return {
        "manifest_version": "1",
        "asset_count": len(assets),
        "modality_counts": counts,
        "assets": [asset.model_dump(mode="json") for asset in assets],
    }


def to_nexent_metadata(
    assets: Sequence[DataAsset], *, limit: int = 8
) -> dict[str, Any]:
    """Compact metadata for a chat request (`meta_data`, audit-only on Nexent)."""
    return {
        "data_assets": [
            {
                "asset_id": asset.asset_id,
                "doc_type": asset.doc_type,
                "equipment_id": asset.equipment_id,
                "modality": asset.modality.value,
                "standard_refs": list(asset.standard_refs),
            }
            for asset in assets[:limit]
        ]
    }


def asset_lookup(assets: Sequence[DataAsset]) -> dict[str, DataAsset]:
    return {asset.asset_id: asset for asset in assets}


def resolve_asset(assets: Sequence[DataAsset], asset_id: str) -> DataAsset:
    """Look up by asset id; raises KeyError with a helpful message."""
    index = asset_lookup(assets)
    if asset_id not in index:
        raise KeyError(f"unknown asset_id: {asset_id}")
    return index[asset_id]


def manifest_from_mapping(rows: Iterable[Mapping[str, Any]]) -> list[DataAsset]:
    """Rebuild a manifest from JSON rows (round-trip / caching)."""
    return sorted(
        (DataAsset.model_validate(dict(row)) for row in rows),
        key=lambda asset: asset.asset_id,
    )
