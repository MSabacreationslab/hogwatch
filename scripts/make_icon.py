"""Draw HogWatch's icon (the dashboard's blue chart mark) and save it as assets/hogwatch.ico.

Standard library only, so the build needs no image package. Run it again only if
the design changes; the .ico is committed.
"""

import struct
import zlib
from pathlib import Path

SIZE = 256
BLUE = (42, 120, 214)
WHITE = (255, 255, 255)
RADIUS = 56          # rounded corners of the square
STROKE = 12          # half-width of the chart line
# The dashboard favicon's path, scaled from its 32-unit grid to 256 pixels.
POINTS = [(48, 176), (96, 112), (136, 152), (208, 64)]


def _coverage(distance: float, edge: float) -> float:
    """1 inside, 0 outside, a smooth ramp across one pixel at the edge (anti-aliasing)."""
    return max(0.0, min(1.0, edge - distance + 0.5))


def _segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def render() -> bytes:
    """RGBA pixels, row by row."""
    rows = bytearray()
    half = SIZE / 2
    for y in range(SIZE):
        rows.append(0)  # PNG filter type for this row: none
        for x in range(SIZE):
            cx, cy = x + 0.5, y + 0.5
            # Distance outside a rounded square.
            qx, qy = abs(cx - half) - (half - RADIUS), abs(cy - half) - (half - RADIUS)
            outside = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0)
            alpha = _coverage(outside, RADIUS)
            line = min(_segment_distance(cx, cy, *a, *b) for a, b in zip(POINTS, POINTS[1:]))
            w = _coverage(line, STROKE)
            r, g, b = (round(BLUE[i] + (WHITE[i] - BLUE[i]) * w) for i in range(3))
            rows += bytes((r, g, b, round(255 * alpha)))
    return bytes(rows)


def png(pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels, 9)) + chunk(b"IEND", b"")


def ico(png_bytes: bytes) -> bytes:
    """A one-image .ico holding the 256x256 PNG (Windows scales it for smaller sizes)."""
    directory = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png_bytes), 22)
    return directory + png_bytes


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets" / "hogwatch.ico"
    out.parent.mkdir(exist_ok=True)
    image = png(render())
    out.write_bytes(ico(image))
    out.with_suffix(".png").write_bytes(image)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
