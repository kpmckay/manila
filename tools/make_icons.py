"""Generate Manila's icons -- the taskbar one and the web one, from one shape.

Run when the shape changes; the files it writes are what ship:

    python3 tools/make_icons.py

    windows/manila.ico              the Start Menu and shortcut icon
    manila/static/favicon.ico       the browser tab
    manila/static/icon-192.png      the app window's taskbar icon, and
    manila/static/icon-512.png      the same at installable-app size

Kept as source rather than binaries someone has to take on trust: it is a
hundred lines of stdlib, and the shape can be argued with. A folder in manila
card stock, which is the whole metaphor of the app.
"""

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WINDOWS_SIZES = (16, 32, 48, 64, 128, 256)
FAVICON_SIZES = (16, 32, 48)
SS = 4                                   # supersampling, for smooth edges

BODY = (0x8A, 0x6A, 0x35)                # manila
TAB = (0xA5, 0x84, 0x4B)                 # the same card, catching the light
EDGE = (0x6B, 0x50, 0x28)


def _rounded(u, v, left, top, right, bottom, radius):
    """Whether a point is inside a rounded rectangle, in unit coordinates."""
    if not (left <= u <= right and top <= v <= bottom):
        return False
    # Only the four corner squares need the circle test; everything else is in.
    cx = left + radius if u < left + radius else right - radius if u > right - radius else u
    cy = top + radius if v < top + radius else bottom - radius if v > bottom - radius else v
    return (u - cx) ** 2 + (v - cy) ** 2 <= radius ** 2


def _shape(x, y, n):
    """Coverage of the folder at a point, in a unit square scaled to n."""
    u, v = x / n, y / n
    # Back leaf: the tab, sitting above and behind the front leaf. Its bottom
    # corners are square so it reads as one sheet with the body, not a box
    # resting on top of it.
    tab = _rounded(u, v, 0.06, 0.18, 0.48, 0.36, 0.05) or (
        0.06 <= u <= 0.48 and 0.30 <= v <= 0.36)
    back = _rounded(u, v, 0.06, 0.26, 0.94, 0.86, 0.06)
    return tab, back


def _pixel(x, y, n):
    tab, back = _shape(x, y, n)
    if back:
        # A hairline darker along the very bottom reads as thickness.
        return EDGE if y / n > 0.82 else BODY
    if tab:
        return TAB
    return None


def _render(size):
    """One RGBA image, supersampled then boxed down for clean edges."""
    n = size * SS
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    hit = _pixel(x * SS + dx, y * SS + dy, n)
                    if hit:
                        r += hit[0]
                        g += hit[1]
                        b += hit[2]
                        a += 255
            count = SS * SS
            if a:
                # Average only over the covered samples, so edge pixels keep
                # their colour and vary in alpha rather than fading to black.
                covered = a // 255
                row += bytes((r // covered, g // covered, b // covered, a // count))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def _png(size, rows):
    raw = b"".join(b"\0" + row for row in rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _ico(sizes):
    images = [_png(size, _render(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in zip(sizes, images):
        # 256 is written as 0: the field is one byte and the format says so.
        entries += struct.pack("<BBBBHHII",
                               size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    return header + entries + blobs


def build():
    written = []

    def write(relative, data):
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append((relative, len(data)))

    write("windows/manila.ico", _ico(WINDOWS_SIZES))
    write("manila/static/favicon.ico", _ico(FAVICON_SIZES))
    for size in (192, 512):
        write(f"manila/static/icon-{size}.png", _png(size, _render(size)))
    return written


if __name__ == "__main__":
    for name, size in build():
        print(f"wrote {name} ({size} bytes)")
