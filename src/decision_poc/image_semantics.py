"""图像语义抽取：把 PNG 资产变成可核对的结构化事实（标准库实现，无视觉模型）。

两种形态由像素自行判定（不靠文件名）：

1. **频谱图**：检测坐标轴 → 逐列取响应曲线最高点 → 峰值频率与相对幅值；
   频率轴范围**来自本仓库生成器契约**（0–1000 Hz），不是从像素里 OCR 出来的刻度。
2. **示意图**：按颜色聚出色块 → 每个色块的 bbox、走向（横/竖/面）、像素数；
   另识别浅蓝填充的「积水范围」。

诚实边界（必须跟着代码走）：

- 只解码 **8-bit RGB/RGBA、非隔行** PNG（本仓库生成器同规格）；其他格式抛 `UnsupportedImage`。
- **不做 OCR**：不读图上的文字，色块用中性命名（颜色 + 走向）；图例含义需调用方给映射。
- 相对幅值是"归一化到绘图区高度"的像素量，不是 mm/s；mm/s 需要读刻度（未做 OCR）。
- 这不是视觉模型，结论可复算：同图同参数 → 同一结果；换一张图结论可能不同，但不会"编"。

用法：

    python -m decision_poc.image_semantics --image samples/assets/vibration_spectrum.png
    python -m decision_poc.image_semantics --image samples/assets/urban/pipeline_map.png \
        --json artifacts/image-semantics.json --regions artifacts/image-regions.json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SUPPORTED_COLOR_TYPES = {2: 3, 6: 4}  # RGB / RGBA
DEFAULT_FREQ_RANGE = (0.0, 1000.0)


class UnsupportedImage(ValueError):
    """不是本模块能解码的 PNG（格式或位深超出支持范围）。"""


# --- PNG 解码（标准库） ----------------------------------------------------


@dataclass(frozen=True)
class PngImage:
    width: int
    height: int
    channels: int
    rows: tuple[bytes, ...]

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        offset = x * self.channels
        row = self.rows[y]
        return row[offset], row[offset + 1], row[offset + 2]


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, height: int, stride: int) -> list[bytes]:
    rows: list[bytes] = []
    offset = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for index in range(stride):
                left = line[index - 3] if index >= 3 else 0
                line[index] = (line[index] + left) & 0xFF
        elif filter_type == 2:  # Up
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:  # Average
            for index in range(stride):
                left = line[index - 3] if index >= 3 else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for index in range(stride):
                left = line[index - 3] if index >= 3 else 0
                up_left = previous[index - 3] if index >= 3 else 0
                line[index] = (line[index] + _paeth(left, previous[index], up_left)) & 0xFF
        else:
            raise UnsupportedImage(f"未知 PNG 行过滤类型 {filter_type}")
        rows.append(bytes(line))
        previous = line
    return rows


def read_png(path: Path | str) -> PngImage:
    data = Path(path).read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise UnsupportedImage(f"{path} 不是 PNG")
    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int] | None = None
    compressed = bytearray()
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if bit_depth != 8:
                raise UnsupportedImage(f"只支持 8-bit PNG，收到 bit_depth={bit_depth}")
            if color_type not in SUPPORTED_COLOR_TYPES:
                raise UnsupportedImage(f"只支持 RGB/RGBA（color_type 2/6），收到 {color_type}")
            if compression != 0 or filter_method != 0:
                raise UnsupportedImage("不支持的 PNG 压缩/过滤方式")
            if interlace != 0:
                raise UnsupportedImage("不支持隔行 PNG")
            header = (width, height, color_type)
        elif tag == b"IDAT":
            compressed.extend(body)
        elif tag == b"IEND":
            break
    if header is None:
        raise UnsupportedImage("PNG 缺少 IHDR")
    width, height, color_type = header
    channels = SUPPORTED_COLOR_TYPES[color_type]
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:  # pragma: no cover - 损坏文件
        raise UnsupportedImage(f"IDAT 解压失败：{exc}") from exc
    rows = _unfilter(raw, height, width * channels)
    return PngImage(width=width, height=height, channels=channels, rows=tuple(rows))


# --- 通用像素统计 ---------------------------------------------------------


def _luminance(rgb: tuple[int, int, int]) -> int:
    return (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000


def _is_dark(rgb: tuple[int, int, int], threshold: int = 120) -> bool:
    return _luminance(rgb) < threshold


def _is_curve_red(rgb: tuple[int, int, int]) -> bool:
    """生成器曲线色 (198,40,40)；允许轻微抗锯齿偏差。"""
    r, g, b = rgb
    return r >= 150 and g <= 90 and b <= 90


def _bbox(points: Iterable[tuple[int, int]]) -> tuple[int, int, int, int] | None:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    if not xs:
        return None
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


# --- 形态判定 -------------------------------------------------------------


def _longest_run(flags: Sequence[bool]) -> tuple[int, int]:
    """返回 (起点, 长度)；全是 False 时长度为 0。"""
    best_start, best_len = 0, 0
    start = -1
    for index, flag in enumerate(flags):
        if flag:
            if start < 0:
                start = index
            if index - start + 1 > best_len:
                best_start, best_len = start, index - start + 1
        else:
            start = -1
    return best_start, best_len


def _find_axes(image: PngImage) -> dict[str, int] | None:
    """找绘图区：左侧竖轴、底部横轴。

    用"最长连续深色像素段"判定轴，而不是"整列/整行深色计数"——否则坐标图左上角
    的标题文字会被误判成轴的顶端（实测踩过这个坑）。
    """
    best_column: tuple[int, int, int] | None = None
    for x in range(max(1, image.width // 3)):
        flags = [_is_dark(image.pixel(x, y)) for y in range(image.height)]
        start, length = _longest_run(flags)
        if length >= image.height * 0.5 and (best_column is None or length > best_column[1]):
            best_column = (x, length, start)
    if best_column is None:
        return None
    axis_x, column_len, column_start = best_column

    best_row: tuple[int, int, int] | None = None
    for y in range(image.height // 2, image.height):
        flags = [_is_dark(image.pixel(x, y)) for x in range(image.width)]
        start, length = _longest_run(flags)
        if length >= image.width * 0.5 and (best_row is None or length > best_row[1]):
            best_row = (y, length, start)
    if best_row is None:
        return None
    axis_y, _row_len, row_start = best_row
    x_left, x_right = row_start, row_start + _row_len - 1
    if axis_x >= x_right or column_start >= axis_y:
        return None
    if x_right - axis_x < 50 or axis_y - column_start < 50:
        return None
    return {"x_left": axis_x, "x_right": x_right, "y_bottom": axis_y, "y_top": column_start}


def _curve_columns(image: PngImage, axes: Mapping[str, int]) -> list[tuple[int, int]]:
    """逐列取响应曲线最高点：返回 [(x, y)]。"""
    points: list[tuple[int, int]] = []
    for x in range(axes["x_left"] + 1, axes["x_right"]):
        for y in range(axes["y_top"], axes["y_bottom"]):
            if _is_curve_red(image.pixel(x, y)):
                points.append((x, y))
                break
    return points


def detect_kind(image: PngImage) -> str:
    axes = _find_axes(image)
    if axes is None:
        return "schematic"
    points = _curve_columns(image, axes)
    span = max(1, axes["x_right"] - axes["x_left"])
    coverage = len(points) / span
    if coverage < 0.5:
        return "schematic"
    ys = [y for _, y in points]
    vertical = (max(ys) - min(ys)) / max(1, axes["y_bottom"] - axes["y_top"])
    return "spectrum" if vertical >= 0.15 else "schematic"


# --- 频谱：峰值抽取 -------------------------------------------------------


def analyze_spectrum(
    image: PngImage,
    *,
    freq_range: tuple[float, float] = DEFAULT_FREQ_RANGE,
    peak_threshold: float = 0.03,
    max_peaks: int = 4,
) -> dict[str, Any]:
    axes = _find_axes(image)
    if axes is None:
        raise UnsupportedImage("没有检测到坐标轴，不能按频谱解析")
    x_left, x_right = axes["x_left"], axes["x_right"]
    y_top, y_bottom = axes["y_top"], axes["y_bottom"]
    span = max(1, x_right - x_left)

    profile: list[float] = []
    for x in range(x_left + 1, x_right):
        best = None
        for y in range(y_top, y_bottom):
            if _is_curve_red(image.pixel(x, y)):
                best = y
                break
        amplitude = (y_bottom - best) / max(1, y_bottom - y_top) if best is not None else 0.0
        profile.append(amplitude)

    smoothed: list[float] = []
    for index in range(len(profile)):
        window = profile[max(0, index - 2) : index + 3]
        smoothed.append(sum(window) / len(window))

    candidates: list[tuple[int, float]] = []
    for index in range(1, len(smoothed) - 1):
        if smoothed[index] >= peak_threshold and smoothed[index] >= smoothed[index - 1] and smoothed[index] > smoothed[index + 1]:
            candidates.append((index, smoothed[index]))
    merged: list[tuple[int, float]] = []
    for index, value in candidates:
        if merged and index - merged[-1][0] <= 6:
            if value > merged[-1][1]:
                merged[-1] = (index, value)
        else:
            merged.append((index, value))
    top = sorted(merged, key=lambda item: item[1], reverse=True)[:max_peaks]
    top.sort(key=lambda item: item[0])

    freq_min, freq_max = freq_range
    peaks: list[dict[str, Any]] = []
    for order, (index, value) in enumerate(top, start=1):
        column = x_left + 1 + index
        freq = round(freq_min + (column - x_left) / span * (freq_max - freq_min), 2)
        curve_y = y_bottom
        for y in range(y_top, y_bottom):
            if _is_curve_red(image.pixel(column, y)):
                curve_y = y
                break
        peaks.append(
            {
                "index": order,
                "freq_hz": freq,
                "rel_amplitude": round(value, 6),
                "column": column,
                "bbox": [max(x_left, column - 8), curve_y, 16, max(1, y_bottom - curve_y)],
            }
        )
    return {
        "kind": "spectrum",
        "pixel_size": [image.width, image.height],
        "plot": {"x": [x_left, x_right], "y": [y_top, y_bottom]},
        "frequency_axis": {
            "min_hz": freq_min,
            "max_hz": freq_max,
            "source": "generator_contract",
        },
        "peaks": peaks,
        "peak_count": len(peaks),
        "note": "相对幅值归一化到绘图区高度，不是 mm/s；刻度读数需 OCR（本模块不做）",
    }


# --- 示意图：色块与积水范围 ----------------------------------------------


def analyze_schematic(
    image: PngImage,
    *,
    min_pixels: int = 150,
    saturation: int = 40,
    flood_tolerance: int = 14,
) -> dict[str, Any]:
    counts: Counter[tuple[int, int, int]] = Counter()
    for y in range(image.height):
        for x in range(image.width):
            counts[image.pixel(x, y)] += 1

    flood_color = (205, 225, 240)
    candidates: list[tuple[int, int, int]] = []
    for rgb, count in counts.most_common():
        if count < min_pixels:
            continue
        is_flood = all(abs(rgb[i] - flood_color[i]) <= flood_tolerance for i in range(3))
        high, low = max(rgb), min(rgb)
        if is_flood or high - low >= saturation:
            candidates.append(rgb)
        if len(candidates) >= 40:
            break

    wanted = set(candidates)
    boxes: dict[tuple[int, int, int], list[int]] = {}
    for rgb in candidates:
        boxes[rgb] = [image.width, image.height, -1, -1, 0]
    for y in range(image.height):
        for x in range(image.width):
            rgb = image.pixel(x, y)
            box = boxes.get(rgb)
            if box is None:
                continue
            box[0] = min(box[0], x)
            box[1] = min(box[1], y)
            box[2] = max(box[2], x)
            box[3] = max(box[3], y)
            box[4] += 1

    segments: list[dict[str, Any]] = []
    flood: dict[str, Any] | None = None
    # 走向用"该颜色在单行/单列里的最大像素数"判定：图例色块很小，不会主导走向；
    # 直接看 bbox 长宽比会把"竖线 + 右下角图例色块"判成面积块（实测踩过）。
    row_max: dict[tuple[int, int, int], int] = {rgb: 0 for rgb in candidates}
    col_max: dict[tuple[int, int, int], int] = {rgb: 0 for rgb in candidates}
    for y in range(image.height):
        local: Counter[tuple[int, int, int]] = Counter()
        for x in range(image.width):
            rgb = image.pixel(x, y)
            if rgb in row_max:
                local[rgb] += 1
        for rgb, count in local.items():
            if count > row_max[rgb]:
                row_max[rgb] = count
    for x in range(image.width):
        local = Counter()
        for y in range(image.height):
            rgb = image.pixel(x, y)
            if rgb in col_max:
                local[rgb] += 1
        for rgb, count in local.items():
            if count > col_max[rgb]:
                col_max[rgb] = count

    for rgb in candidates:
        box = boxes[rgb]
        if box[2] < 0:
            continue
        bbox = (box[0], box[1], box[2] - box[0] + 1, box[3] - box[1] + 1)
        pixels = box[4]
        is_flood = all(abs(rgb[i] - flood_color[i]) <= flood_tolerance for i in range(3))
        if is_flood:
            if flood is None or pixels > flood["pixels"]:
                flood = {"color": list(rgb), "pixels": pixels, "bbox": list(bbox)}
            continue
        horizontal_run, vertical_run = row_max.get(rgb, 0), col_max.get(rgb, 0)
        if horizontal_run >= 60 and horizontal_run >= 3 * max(1, vertical_run):
            orientation = "horizontal"
        elif vertical_run >= 60 and vertical_run >= 3 * max(1, horizontal_run):
            orientation = "vertical"
        else:
            orientation = "area"
        segments.append(
            {
                "color": list(rgb),
                "pixels": pixels,
                "bbox": list(bbox),
                "orientation": orientation,
                "longest_run": {"horizontal": horizontal_run, "vertical": vertical_run},
            }
        )
    segments.sort(key=lambda item: item["pixels"], reverse=True)
    return {
        "kind": "schematic",
        "pixel_size": [image.width, image.height],
        "segments": segments[:12],
        "segment_count": len(segments),
        "flood_area": flood,
        "note": "色块按颜色聚类（非 OCR）；图例文字含义需调用方映射",
    }


# --- 统一入口与区域候选 ---------------------------------------------------


def analyze(path: Path | str, *, kind: str = "auto", freq_range: tuple[float, float] = DEFAULT_FREQ_RANGE) -> dict[str, Any]:
    image = read_png(path)
    resolved = detect_kind(image) if kind == "auto" else kind
    if resolved == "spectrum":
        facts = analyze_spectrum(image, freq_range=freq_range)
    elif resolved == "schematic":
        facts = analyze_schematic(image)
    else:
        raise UnsupportedImage(f"未知解析类型 {resolved}")
    facts["source"] = str(path)
    return facts


def to_region_specs(facts: Mapping[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
    """把图像语义转成 `region_annotation.RegionSpec` 兼容的区域候选（可直接引用）。"""
    specs: list[dict[str, Any]] = []
    if facts.get("kind") == "spectrum":
        for peak in facts.get("peaks", [])[:limit]:
            specs.append(
                {
                    "label": f"频谱峰值 {peak['freq_hz']:.0f}Hz（相对幅值 {peak['rel_amplitude']:.3f}）",
                    "bbox": peak["bbox"],
                    "confidence": 0.9,
                    "note": "pixel-derived：响应曲线局部极大；频率轴范围来自生成器契约",
                }
            )
    else:
        for segment in facts.get("segments", [])[:limit]:
            rgb = segment["color"]
            specs.append(
                {
                    "label": f"示意线 {segment['orientation']} RGB{tuple(rgb)}（{segment['pixels']} px）",
                    "bbox": segment["bbox"],
                    "confidence": 0.8,
                    "note": "pixel-derived：按颜色聚类的色块 bbox；语义名称需图例映射",
                }
            )
        flood = facts.get("flood_area")
        if flood is not None:
            specs.append(
                {
                    "label": f"积水范围（浅蓝填充，{flood['pixels']} px）",
                    "bbox": flood["bbox"],
                    "confidence": 0.85,
                    "note": "pixel-derived：浅蓝填充区域 bbox",
                }
            )
    return specs


# --- CLI ------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图像语义抽取（标准库，无视觉模型）")
    parser.add_argument("--image", required=True, help="PNG 路径")
    parser.add_argument("--kind", default="auto", choices=["auto", "spectrum", "schematic"])
    parser.add_argument("--json", dest="json_path", default=None, help="把语义结果写到 JSON")
    parser.add_argument("--regions", dest="regions_path", default=None, help="把区域候选写到 JSON")
    parser.add_argument("--freq-min", type=float, default=DEFAULT_FREQ_RANGE[0])
    parser.add_argument("--freq-max", type=float, default=DEFAULT_FREQ_RANGE[1])
    args = parser.parse_args(argv)

    facts = analyze(args.image, kind=args.kind, freq_range=(args.freq_min, args.freq_max))
    specs = to_region_specs(facts)
    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.regions_path:
        target = Path(args.regions_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"regions": specs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "image": args.image,
        "kind": facts["kind"],
        "pixel_size": facts["pixel_size"],
        "peaks": [peak["freq_hz"] for peak in facts.get("peaks", [])],
        "segments": len(facts.get("segments", [])),
        "flood_area": bool(facts.get("flood_area")),
        "regions": len(specs),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
