"""Generate synthetic sample assets (zero third-party dependencies).

The PNG is written by hand (zlib + struct) instead of using Pillow/matplotlib so
the demo data is reproducible on any Python 3.12 machine:

    python samples/generate_assets.py

Output: samples/assets/vibration_spectrum.png (900x500, 8-bit RGB).
"""

import math
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT = 900, 500

# 5x7 bitmap font covering A-Z, digits and a few punctuation marks.
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "=": ["00000", "00000", "11111", "00000", "11111", "00000", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}

WHITE = (255, 255, 255)
GRID = (228, 228, 228)
AXIS = (70, 70, 70)
CURVE = (198, 40, 40)
LABEL = (40, 40, 40)


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray([255] * (width * height * 3))

    def _set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.pixels[index : index + 3] = bytes(color)

    def vline(self, x: int, y0: int, y1: int, color, width: int = 1) -> None:
        for y in range(y0, y1 + 1):
            for dx in range(width):
                self._set(x + dx, y, color)

    def hline(self, y: int, x0: int, x1: int, color, width: int = 1) -> None:
        for x in range(x0, x1 + 1):
            for dy in range(width):
                self._set(x, y + dy, color)

    def text(self, x: int, y: int, value: str, color=LABEL, scale: int = 2) -> None:
        cursor = x
        for char in value.upper():
            glyph = FONT.get(char, FONT[" "])
            for row, pattern in enumerate(glyph):
                for col, bit in enumerate(pattern):
                    if bit != "1":
                        continue
                    for dy in range(scale):
                        for dx in range(scale):
                            self._set(cursor + col * scale + dx, y + row * scale + dy, color)
            cursor += 6 * scale

    def line(self, x0: int, y0: int, x1: int, y1: int, color, width: int = 2) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            x = round(x0 + (x1 - x0) * step / steps)
            y = round(y0 + (y1 - y0) * step / steps)
            self.hline(y, x, x + width - 1, color)

    def png(self) -> bytes:
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)  # filter type 0 (None)
            start = y * self.width * 3
            raw.extend(self.pixels[start : start + self.width * 3])
        compressed = zlib.compress(bytes(raw), level=9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b"")
        )


def amplitude(freq: float) -> float:
    """Synthetic gearbox spectrum: 1x/2x/3x mesh harmonics plus low noise."""
    value = 0.18
    for center, peak, spread in ((120, 1.4, 6), (240, 0.55, 5), (360, 0.32, 5), (480, 0.12, 8)):
        value += peak * math.exp(-(((freq - center) / spread) ** 2))
    value += 0.03 * abs(math.sin(freq * 0.7))
    return min(value, 8.0)


def draw_spectrum() -> Canvas:
    canvas = Canvas(WIDTH, HEIGHT)
    left, right = 80, WIDTH - 40
    top, bottom = 90, HEIGHT - 70

    for i in range(11):
        x = round(left + (right - left) * i / 10)
        canvas.vline(x, top, bottom, GRID)
        canvas.text(x - 6, bottom + 10, str(i * 100), color=LABEL, scale=1)
    for i in range(9):
        y = round(bottom - (bottom - top) * i / 8)
        canvas.hline(y, left, right, GRID)
        canvas.text(left - 26, y - 3, str(i), color=LABEL, scale=1)

    canvas.hline(bottom, left, right, AXIS, width=2)
    canvas.vline(left, top, bottom, AXIS, width=2)

    previous = None
    for freq in range(0, 1001, 2):
        x = round(left + (right - left) * freq / 1000)
        y = round(bottom - (bottom - top) * amplitude(freq) / 8)
        if previous is not None:
            canvas.line(previous[0], previous[1], x, y, CURVE, width=2)
        previous = (x, y)

    canvas.text(left, 18, "A-101 VIB SPECTRUM", color=LABEL, scale=3)
    canvas.text(left, 46, "PEAK 120HZ = 7.8 MM/S", color=CURVE, scale=1)
    canvas.text(left, 60, "ISO 10816-3 ZONE D", color=CURVE, scale=1)
    canvas.text(6, 20, "MM/S", color=LABEL, scale=1)
    canvas.text((left + right) // 2 - 10, HEIGHT - 20, "HZ", color=LABEL, scale=1)
    return canvas


def main() -> None:
    target = Path(__file__).resolve().parent / "assets" / "vibration_spectrum.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(draw_spectrum().png())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
