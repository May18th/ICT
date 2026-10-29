"""Asset adapters: heterogeneous file -> structured, citable intermediate form.

Scope for P2 is deliberately honest: `.csv/.tsv/.md/.txt/.log/.png` are parsed
fully or partially; `.pdf/.docx/.xlsx` return `metadata_only` with an explicit
warning instead of pretending. Adding a real PDF/XLSX parser is a dependency
decision (see the P2 task order Q1), not an implementation detail to sneak in.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field

from .chunking import Chunk, chunk_text
from .contracts import StrictModel
from .data_asset import AssetModality, DataAsset
from .region_annotation import (
    AnnotationFile,
    ImageRegion,
    load_annotations,
    png_size,
    regions_for,
)
from .table_normalizer import NormalizedTable, decode_bytes, normalize_table

FULL_SUFFIXES = {".csv", ".tsv", ".md", ".txt", ".log"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx"}


class ParseStatus(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    METADATA_ONLY = "metadata_only"


class ParsedDocument(StrictModel):
    asset_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    modality: AssetModality
    parse_status: ParseStatus
    chunks: list[Chunk] = Field(default_factory=list)
    tables: list[NormalizedTable] = Field(default_factory=list)
    regions: list[ImageRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _parse_delimited(asset: DataAsset, path: Path) -> ParsedDocument:
    text = decode_bytes(path.read_bytes())
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    table = normalize_table(asset, text, delimiter=delimiter)
    status = ParseStatus.FULL if table.rows else ParseStatus.PARTIAL
    warnings = list(table.warnings)
    if not table.rows:
        warnings.append("表格无数据行，仅登记表头")
    return ParsedDocument(
        asset_id=asset.asset_id,
        file_name=asset.file_name,
        modality=asset.modality,
        parse_status=status,
        tables=[table],
        warnings=warnings,
    )


def _parse_text(asset: DataAsset, path: Path) -> ParsedDocument:
    text = decode_bytes(path.read_bytes())
    chunks = chunk_text(asset, text, markdown=path.suffix.lower() == ".md")
    status = ParseStatus.FULL if chunks else ParseStatus.PARTIAL
    warnings = [] if chunks else ["文本无有效内容"]
    return ParsedDocument(
        asset_id=asset.asset_id,
        file_name=asset.file_name,
        modality=asset.modality,
        parse_status=status,
        chunks=chunks,
        warnings=warnings,
    )


def _parse_image(asset: DataAsset, path: Path, annotations: AnnotationFile | None) -> ParsedDocument:
    warnings: list[str] = []
    size = png_size(path) if path.suffix.lower() == ".png" else None
    if size is None:
        return ParsedDocument(
            asset_id=asset.asset_id,
            file_name=asset.file_name,
            modality=asset.modality,
            parse_status=ParseStatus.METADATA_ONLY,
            warnings=["仅支持 PNG 尺寸解析；该格式仅登记元数据"],
        )

    regions: list[ImageRegion] = []
    if annotations is None:
        warnings.append("未提供标注文件，图片仅登记尺寸，无区域证据")
    else:
        specs = annotations.annotations.get(asset.file_name)
        if not specs:
            warnings.append(f"标注文件中无 {asset.file_name} 的区域标注")
        else:
            regions = regions_for(asset, specs, size=size, annotator=annotations.annotator)

    return ParsedDocument(
        asset_id=asset.asset_id,
        file_name=asset.file_name,
        modality=asset.modality,
        parse_status=ParseStatus.PARTIAL if regions else ParseStatus.METADATA_ONLY,
        regions=regions,
        warnings=warnings,
    )


def parse_asset(
    asset: DataAsset,
    path: Path | str,
    *,
    annotations: AnnotationFile | None = None,
) -> ParsedDocument:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"asset file not found: {target}")
    suffix = target.suffix.lower()

    if suffix in {".csv", ".tsv"}:
        return _parse_delimited(asset, target)
    if suffix in {".md", ".txt", ".log"}:
        return _parse_text(asset, target)
    if suffix in IMAGE_SUFFIXES:
        return _parse_image(asset, target, annotations)
    if suffix in DOCUMENT_SUFFIXES:
        return ParsedDocument(
            asset_id=asset.asset_id,
            file_name=asset.file_name,
            modality=asset.modality,
            parse_status=ParseStatus.METADATA_ONLY,
            warnings=[f"{suffix} 解析未实现（需第三方库），本阶段仅登记元数据"],
        )
    return ParsedDocument(
        asset_id=asset.asset_id,
        file_name=asset.file_name,
        modality=asset.modality,
        parse_status=ParseStatus.METADATA_ONLY,
        warnings=[f"未知后缀 {suffix or '(none)'}，仅登记元数据"],
    )


def parse_assets(
    assets: list[DataAsset] | tuple[DataAsset, ...],
    directory: Path | str,
    *,
    annotations_path: Path | str | None = None,
) -> list[ParsedDocument]:
    base = Path(directory)
    annotations = load_annotations(annotations_path) if annotations_path else None
    documents: list[ParsedDocument] = []
    for asset in assets:
        documents.append(parse_asset(asset, base / asset.file_name, annotations=annotations))
    return documents


def parse_stats(documents: list[ParsedDocument]) -> dict[str, int]:
    stats = {"documents": len(documents), "chunks": 0, "tables": 0, "rows": 0, "regions": 0}
    for document in documents:
        stats["chunks"] += len(document.chunks)
        stats["tables"] += len(document.tables)
        stats["rows"] += sum(len(table.rows) for table in document.tables)
        stats["regions"] += len(document.regions)
    for status in ParseStatus:
        stats[status.value] = sum(1 for d in documents if d.parse_status is status)
    return stats
