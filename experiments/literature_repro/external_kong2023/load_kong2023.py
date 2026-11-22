#!/usr/bin/env python
"""Loader for the Kong et al. (2023) validation bundle.

只做一件事：把 Zenodo 里的两张 xlsx 读成内存里的规则结构，并把口径写死在代码里，
避免下游每处都重新猜单位。不 import decision_poc，不写 samples/。

数据实际内容（2026-11-22 实测）：

- ``water_level_10.xlsx``：10 个工作表，每表一条易涝路。列 ``SW`` 水位（m）、``SJ`` 时间（字符串）。
  只有 2019-06-11 一场，每张表 10—56 行，采样间隔 5 min，部分路段更稀。
- ``rainfall_10.xlsx``：10 个工作表，与上表一一对应。列 ``FZJY`` 5 min 累积雨量（mm）、``Time`` 时间。
  场次总量 37.0—52.9 mm，单步最大 20.1 mm。

论文里 2015-05-09 / 05-23 是率定事件、2019-06-11 是检验事件；Zenodo 只给了 2019-06-11 的
降雨与水位，2015 两场只公开了出租车通过率计数（``Training data/``），没有水位。
所以本模块能提供的是**单场真观测**，不是多场。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")

#: 雨量累积步长（分钟）。论文与数据都是 5 min。
RAIN_INTERVAL_MIN = 5.0

#: 一份数据包应恰好覆盖 10 条易涝路。
EXPECTED_ROADS = 10


@dataclass(frozen=True)
class Series:
    """一条易涝路上的一组 (时刻, 数值) 序列。"""

    road_id: str
    times: np.ndarray  # datetime64[ns]
    values: np.ndarray  # 浮点

    def __len__(self) -> int:
        return int(self.values.size)

    @property
    def span_minutes(self) -> float:
        if self.values.size < 2:
            return 0.0
        return float((self.times[-1] - self.times[0]) / np.timedelta64(1, "m"))


@dataclass(frozen=True)
class RoadBundle:
    road_id: str
    rainfall: Series  # mm / 5 min
    water_level: Series  # m
    source_type: str = "observed"

    @property
    def total_rain_mm(self) -> float:
        return float(self.rainfall.values.sum())

    @property
    def peak_rain_mm_per_5min(self) -> float:
        return float(self.rainfall.values.max()) if len(self.rainfall) else 0.0

    @property
    def peak_level_m(self) -> float:
        return float(self.water_level.values.max()) if len(self.water_level) else 0.0

    @property
    def peak_rain_time(self) -> np.datetime64:
        return self.rainfall.times[int(np.argmax(self.rainfall.values))]

    @property
    def peak_level_time(self) -> np.datetime64:
        return self.water_level.times[int(np.argmax(self.water_level.values))]

    @property
    def lag_minutes(self) -> float:
        """水位峰值相对雨量峰值的滞后，正数表示水位更晚。"""

        return float((self.peak_level_time - self.peak_rain_time) / np.timedelta64(1, "m"))


def _parse_time(value: object) -> np.datetime64:
    if isinstance(value, dt.datetime):
        return np.datetime64(value.replace(tzinfo=None), "s")
    text = str(value).strip()
    for fmt in TIME_FORMATS:
        try:
            return np.datetime64(dt.datetime.strptime(text, fmt), "s")
        except ValueError:
            continue
    raise ValueError(f"无法解析时间：{value!r}")


def _read_sheet(workbook, sheet: str, value_header: str) -> Series:
    """读一张表；第 1 行是表头，第 2 行起是 (数值, 时间)。"""

    worksheet = workbook[sheet]
    times: list[np.datetime64] = []
    values: list[float] = []
    for row in worksheet.iter_rows(min_row=2, values_only=True):
        if row[0] is None or row[1] is None:
            continue
        if isinstance(row[0], str):
            try:
                value = float(row[0])
            except ValueError:
                continue
        else:
            value = float(row[0])
        times.append(_parse_time(row[1]))
        values.append(value)

    if not times:
        raise ValueError(f"工作表 {sheet} 没有数据行（表头 {value_header}）")

    order = np.argsort(np.array(times))
    return Series(
        road_id=str(sheet),
        times=np.array(times)[order],
        values=np.array(values, dtype=float)[order],
    )


def load_bundle(data_root: Path) -> list[RoadBundle]:
    """读取 ``<data_root>/Assets/Validation data`` 下的两张表。"""

    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 依赖门
        raise RuntimeError(
            "缺少 openpyxl，无法读 xlsx。装到实验环境："
            "uv pip install --python .venv-exp/Scripts/python.exe openpyxl"
        ) from exc

    validation = Path(data_root) / "Assets" / "Validation data"
    level_file = validation / "water_level_10.xlsx"
    rain_file = validation / "rainfall_10.xlsx"
    for path in (level_file, rain_file):
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到 {path}。先跑 fetch_kong2023.py 下载并解压数据包。"
            )

    level_book = openpyxl.load_workbook(level_file, data_only=True, read_only=True)
    rain_book = openpyxl.load_workbook(rain_file, data_only=True, read_only=True)
    try:
        road_ids = [name for name in level_book.sheetnames if name in rain_book.sheetnames]
        if len(road_ids) != EXPECTED_ROADS:
            raise ValueError(
                f"期望 {EXPECTED_ROADS} 条路段同时具备降雨与水位，实际 {len(road_ids)}：{road_ids}"
            )
        bundles = [
            RoadBundle(
                road_id=road_id,
                rainfall=_read_sheet(rain_book, road_id, "FZJY"),
                water_level=_read_sheet(level_book, road_id, "SW"),
            )
            for road_id in road_ids
        ]
    finally:
        level_book.close()
        rain_book.close()
    return bundles


def to_common_grid(
    bundle: RoadBundle, lead_minutes: float = 30.0, tail_minutes: float = 180.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把雨量与水位对齐到 5 min 公共网格。

    水位采样在无雨时段会稀到 1 h（论文 Table 4），直接按时间戳求值会错位，
    所以统一在 5 min 网格上做**前向填充**：每个网格点取不晚于该时刻的最近观测。
    这不引入未来信息，也不插值造点。

    返回 ``(相对分钟, 雨量mm, 水位m)``，窗口为降雨起点前 ``lead_minutes``
    到降雨结束后 ``tail_minutes``。
    """

    step = np.timedelta64(int(RAIN_INTERVAL_MIN), "m")
    start = bundle.rainfall.times[0] - np.timedelta64(int(lead_minutes), "m")
    end = bundle.rainfall.times[-1] + np.timedelta64(int(tail_minutes), "m")
    grid = np.arange(start, end + step, step)

    rain = _forward_fill(bundle.rainfall, grid)
    level = _forward_fill(bundle.water_level, grid)
    minutes = (grid - bundle.rainfall.times[0]) / np.timedelta64(1, "m")
    return minutes.astype(float), rain, level


def _forward_fill(series: Series, grid: np.ndarray) -> np.ndarray:
    """对每个网格点取不晚于它的最近观测；早于首个观测的点填该序列首值。"""

    index = np.searchsorted(series.times, grid, side="right") - 1
    index = np.clip(index, 0, series.values.size - 1)
    return series.values[index]


def summarise(bundle: RoadBundle) -> dict[str, object]:
    """一条路的关键量，全部按原始观测直接算，不含任何模型。"""

    level = bundle.water_level
    wet = level.values >= 0.15
    return {
        "road_id": bundle.road_id,
        "rain_samples": len(bundle.rainfall),
        "level_samples": len(level),
        "rain_span_min": round(bundle.rainfall.span_minutes, 1),
        "level_span_min": round(level.span_minutes, 1),
        "total_rain_mm": round(bundle.total_rain_mm, 2),
        "peak_rain_mm_per_5min": round(bundle.peak_rain_mm_per_5min, 2),
        "peak_rain_time": str(bundle.peak_rain_time),
        "peak_level_m": round(bundle.peak_level_m, 3),
        "peak_level_time": str(bundle.peak_level_time),
        "lag_minutes": round(bundle.lag_minutes, 1),
        "minutes_above_015m": int(wet.sum()) * 5,
        "level_range_m": [round(float(level.values.min()), 3), round(float(level.values.max()), 3)],
    }
