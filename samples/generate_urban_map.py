"""Draw the A-03 schematic pipeline map (synthetic, standard library only).

Reuses the 5x7 bitmap Canvas from `generate_assets.py` so the demo dataset stays
byte-reproducible without Pillow/matplotlib:

    python samples/generate_urban_map.py

Output: samples/assets/urban/pipeline_map.png (800x520 RGB).
The map is schematic, not georeferenced: it exists to give the agent a citable
"map/管线图" modality asset with a stable region, not to be a real basemap.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_assets import Canvas  # noqa: E402

WIDTH, HEIGHT = 800, 520

ROAD = (232, 232, 228)
ROAD_EDGE = (170, 170, 165)
DRAINAGE = (40, 110, 190)
GAS = (200, 60, 40)
POWER = (215, 150, 30)
TELECOM = (60, 150, 100)
WATER = (120, 90, 170)
TEXT = (40, 40, 40)
MUTED = (110, 110, 105)
BOX = (250, 250, 248)
WATER_BODY = (205, 225, 240)

CENTER_Y = 250
CENTER_X = 360
ROAD_W = 22


def draw_road(canvas: Canvas, horizontal: bool, pos: int, start: int, end: int, width: int = ROAD_W) -> None:
    if horizontal:
        canvas.hline(pos - width // 2, start, end, ROAD_EDGE, width=width + 4)
        canvas.hline(pos - width // 2 + 2, start, end, ROAD, width=width)
    else:
        canvas.vline(pos - width // 2, start, end, ROAD_EDGE, width=width + 4)
        canvas.vline(pos - width // 2 + 2, start, end, ROAD, width=width)


def draw_pipe(canvas: Canvas, horizontal: bool, pos: int, start: int, end: int, color, width: int = 4) -> None:
    if horizontal:
        canvas.hline(pos, start, end, color, width=width)
    else:
        canvas.vline(pos, start, end, color, width=width)


def draw_map() -> Canvas:
    canvas = Canvas(WIDTH, HEIGHT)
    canvas.hline(0, 0, WIDTH - 1, (255, 255, 255), width=HEIGHT)

    canvas.text(24, 18, "A-03 PIPELINE SCHEMATIC", color=TEXT, scale=2)
    canvas.text(24, 40, "SYNTHETIC DEMO DATA / NOT GEOREFERENCED", color=MUTED, scale=1)

    left, right = 40, 560
    top, bottom = 110, 470
    road_top, road_main, road_south = 160, 260, 400
    road_west, road_mid = 190, 360

    for y in (road_top, road_south):
        draw_road(canvas, True, y, left, right, width=16)
    for x in (road_west, road_mid):
        draw_road(canvas, False, x, top, bottom, width=16)
    draw_road(canvas, True, road_main, left, right)

    canvas.hline(road_main - 15, 250, 470, WATER_BODY, width=30)

    draw_pipe(canvas, True, road_main + 9, left, right, DRAINAGE)
    draw_pipe(canvas, True, road_main - 9, road_west, right, GAS)
    draw_pipe(canvas, False, road_mid - 9, top, bottom, POWER)
    draw_pipe(canvas, False, road_west + 8, top, bottom, TELECOM)
    draw_pipe(canvas, False, road_west + 14, road_top, road_main, WATER)

    canvas.text(44, road_top - 26, "R-104 HUANCHENG N RD", color=TEXT, scale=1)
    canvas.text(44, road_south - 26, "R-102 JIANSHE AVE", color=TEXT, scale=1)
    canvas.text(44, road_main - 34, "R-101 XINGHUA RD", color=TEXT, scale=1)
    canvas.text(196, 92, "R-105 XINGHUA BRANCH", color=TEXT, scale=1)
    canvas.text(366, 92, "R-103 MINSHENG ST", color=TEXT, scale=1)

    canvas.text(256, 222, "FLOOD AREA R-101 K0+200-600", color=(70, 120, 160), scale=1)

    canvas.vline(300, 276, 300, TEXT, width=2)
    canvas.hline(276, 300, 324, TEXT, width=2)
    canvas.hline(300, 300, 324, TEXT, width=2)
    canvas.text(332, 282, "DF-PS-01 PUMP 1200 M3/H", color=TEXT, scale=1)

    legend_x, legend_y = 590, 80
    canvas.hline(legend_y - 8, legend_x, 780, ROAD_EDGE, width=1)
    canvas.vline(legend_x, legend_y - 8, 240, ROAD_EDGE, width=1)
    canvas.vline(780, legend_y - 8, 240, ROAD_EDGE, width=1)
    canvas.hline(240, legend_x, 780, ROAD_EDGE, width=1)
    canvas.text(legend_x + 10, legend_y, "LEGEND", color=TEXT, scale=1)

    entries = [
        (DRAINAGE, "DRAINAGE DN600"),
        (GAS, "GAS DN300 D=2.5M"),
        (POWER, "POWER 10KV"),
        (TELECOM, "TELECOM"),
        (WATER, "WATER DN300"),
        (WATER_BODY, "FLOOD AREA"),
    ]
    for index, (color, label) in enumerate(entries):
        y = legend_y + 26 + index * 26
        canvas.hline(y, legend_x + 10, legend_x + 34, color, width=6)
        canvas.text(legend_x + 42, y - 3, label, color=TEXT, scale=1)

    canvas.text(24, HEIGHT - 26, "SOURCE: SYNTHETIC / VERSION V1 / 2026-10-29", color=MUTED, scale=1)
    return canvas


def main() -> None:
    target = Path(__file__).resolve().parent / "assets" / "urban" / "pipeline_map.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(draw_map().png())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
