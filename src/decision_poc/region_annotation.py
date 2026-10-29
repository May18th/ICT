"""Image region annotation: make a picture citable by bounding box.

The brief asks for map/photo assets to become evidence. A vision model would
introduce unexplainable error into a "verifiable" demo, so P2 ships **manual
annotations** (`samples/assets/urban/annotations.json`) and a hard validation
that every box lies inside the real pixel size. Swapping in a model later only
requires producing the same file.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel
from .data_asset import DataAsset

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RegionSpec(StrictModel):
    label: str = Field(min_length=1)
    bbox: tuple[int, int, int, int]
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    note: str = ""

    @field_validator("bbox")
    @classmethod
    def bbox_must_be_positive(cls, value: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = value
        if min(x, y) < 0:
            raise ValueError("bbox origin must be non-negative")
        if width <= 0 or height <= 0:
            raise ValueError("bbox width/height must be positive")
        return value


class AnnotationFile(StrictModel):
    version: str = Field(min_length=1)
    annotator: str = Field(default="manual:v1", min_length=1)
    annotations: dict[str, list[RegionSpec]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_empty_label_sets(self) -> "AnnotationFile":
        for file_name, specs in self.annotations.items():
            if not file_name.strip():
                raise ValueError("annotation key must be a file name")
            if not specs:
                raise ValueError(f"annotation for {file_name} is empty")
        return self


class ImageRegion(StrictModel):
    region_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0.0, le=1.0)
    locator: str = Field(min_length=1)
    annotator: str = Field(min_length=1)
    note: str = ""


def png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:24]
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def load_annotations(path: Path | str) -> AnnotationFile:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return AnnotationFile.model_validate(payload)


def _check_inside(bbox: tuple[int, int, int, int], size: tuple[int, int]) -> None:
    x, y, width, height = bbox
    canvas_width, canvas_height = size
    if x + width > canvas_width or y + height > canvas_height:
        raise ValueError(
            f"bbox {bbox} exceeds image bounds {size}"
        )


def regions_for(
    asset: DataAsset,
    specs: list[RegionSpec],
    *,
    size: tuple[int, int] | None,
    annotator: str,
) -> list[ImageRegion]:
    """Validate boxes against the real image size and build citable regions."""
    if size is None:
        raise ValueError(f"cannot resolve image size for {asset.file_name}")
    regions: list[ImageRegion] = []
    for position, spec in enumerate(specs, start=1):
        _check_inside(spec.bbox, size)
        x, y, width, height = spec.bbox
        regions.append(
            ImageRegion(
                region_id=f"rgn-{asset.asset_id}-{position:02d}",
                asset_id=asset.asset_id,
                label=spec.label,
                bbox=spec.bbox,
                confidence=spec.confidence,
                locator=f"{asset.locator}#region:{x},{y},{width},{height}",
                annotator=annotator,
                note=spec.note,
            )
        )
    return regions


def annotation_summary(regions: list[ImageRegion]) -> dict[str, Any]:
    labels = sorted({region.label for region in regions})
    return {
        "regions": len(regions),
        "labels": labels,
        "annotators": sorted({region.annotator for region in regions}),
    }
