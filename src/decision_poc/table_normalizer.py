"""Table normalisation: raw CSV/TSV -> typed, row-addressable structure.

Design rules:

- The row locator is the **source line number**, not the row index, so an answer
  can say "第 7 行" and a human can open the file and see the same thing.
- Unmapped columns are never dropped silently: they stay in the row under their
  raw header *and* are reported in `unmapped_columns` so P3 can extend aliases.
- A value that fails type conversion keeps its raw text and produces a warning;
  one bad cell must not drop the row.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .city_profile import (
    DATETIME_COLUMNS,
    FLOAT_COLUMNS,
    INT_COLUMNS,
    UNITS,
    alias_index,
)
from .contracts import StrictModel
from .data_asset import ENCODINGS, DataAsset

DType = Literal["str", "int", "float", "datetime", "enum"]


class ColumnSpec(StrictModel):
    raw_name: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    dtype: DType
    unit: str | None = None


class NormalizedTable(StrictModel):
    table_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    columns: list[ColumnSpec] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_locators: list[str] = Field(default_factory=list)
    unmapped_columns: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def column_names(self) -> list[str]:
        return [column.canonical for column in self.columns]


def decode_bytes(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _dtype_for(canonical: str) -> DType:
    if canonical in DATETIME_COLUMNS:
        return "datetime"
    if canonical in FLOAT_COLUMNS:
        return "float"
    if canonical in INT_COLUMNS:
        return "int"
    return "str"


def _convert(raw: str, dtype: DType) -> tuple[Any, str | None]:
    text = raw.strip()
    if text == "":
        return None, None
    if dtype == "int":
        try:
            return int(float(text)), None
        except ValueError:
            return text, f"无法解析为 int，保留原文 {text!r}"
    if dtype == "float":
        try:
            return float(text), None
        except ValueError:
            return text, f"无法解析为 float，保留原文 {text!r}"
    if dtype == "datetime":
        try:
            return datetime.fromisoformat(text), None
        except ValueError:
            return text, f"无法解析为 datetime，保留原文 {text!r}"
    return text, None


def normalize_table(
    asset: DataAsset,
    text: str,
    *,
    delimiter: str = ",",
    aliases: dict[str, str] | None = None,
    first_line: int = 1,
) -> NormalizedTable:
    """Parse a delimited table into typed rows with source-line locators."""
    index = aliases if aliases is not None else alias_index()
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    raw_rows = [row for row in reader]
    if not raw_rows:
        return NormalizedTable(
            table_id=f"tbl-{asset.asset_id}",
            asset_id=asset.asset_id,
            warnings=["空表：无表头"],
        )

    header = [cell.strip().lstrip("\ufeff") for cell in raw_rows[0]]
    columns: list[ColumnSpec] = []
    unmapped: list[str] = []
    mapping: list[str] = []
    for raw_name in header:
        canonical = index.get(raw_name)
        if canonical is None:
            unmapped.append(raw_name)
            mapping.append(raw_name)
            columns.append(ColumnSpec(raw_name=raw_name or "?", canonical=raw_name or "?", dtype="str"))
            continue
        mapping.append(canonical)
        columns.append(
            ColumnSpec(
                raw_name=raw_name,
                canonical=canonical,
                dtype=_dtype_for(canonical),
                unit=UNITS.get(canonical),
            )
        )

    rows: list[dict[str, Any]] = []
    locators: list[str] = []
    warnings: list[str] = [] if not unmapped else [f"未识别列：{', '.join(unmapped)}"]
    for offset, raw_row in enumerate(raw_rows[1:], start=1):
        if not any(cell.strip() for cell in raw_row):
            continue
        line_no = first_line + offset
        row: dict[str, Any] = {}
        for position, canonical in enumerate(mapping):
            dtype = columns[position].dtype
            cell = raw_row[position] if position < len(raw_row) else ""
            value, problem = _convert(cell, dtype)
            row[canonical] = value
            if problem:
                warnings.append(f"第 {line_no} 行 {canonical}：{problem}")
        rows.append(row)
        locators.append(f"#row:{line_no}")

    return NormalizedTable(
        table_id=f"tbl-{asset.asset_id}",
        asset_id=asset.asset_id,
        columns=columns,
        rows=rows,
        row_locators=locators,
        unmapped_columns=unmapped,
        warnings=warnings,
    )


def normalize_table_file(asset: DataAsset, path: Path) -> NormalizedTable:
    text = decode_bytes(path.read_bytes())
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    table = normalize_table(asset, text, delimiter=delimiter)
    return table


def table_fingerprint(table: NormalizedTable) -> str:
    """Stable fingerprint of the normalised content (layout-independent)."""
    material = "|".join(
        f"{column.canonical}:{column.dtype}" for column in table.columns
    ) + "||" + "|".join(
        ",".join(f"{key}={value!r}" for key, value in sorted(row.items())) for row in table.rows
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
