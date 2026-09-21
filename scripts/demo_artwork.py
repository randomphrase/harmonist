"""Draw original geometric demo sleeves, using only the Python standard library.

Run from the repository root: python scripts/demo_artwork.py
These are deliberately simple printed shapes and bitmap lettering, not borrowed
film artwork. The small Mouse Rat sleeve and archive candidate share one design.
"""

import struct
import zlib
from pathlib import Path

FONT = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01111 10000 10000 10000 10000 10000 01111",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "J": "00111 00010 00010 00010 10010 10010 01100",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010",
    "4": "00110 01010 10010 11111 00010 00010 00010",
}
SLEEVES = [
    ("wyld", "WS", "ede4ca", "20395a", "ce5438"),
    ("sex-bob-omb", "SB", "e9d846", "202021", "d64338"),
    ("sonic", "SDM", "e2ded3", "30352d", "cb5b33"),
    ("thamesmen", "TM", "dc6842", "f2deb0", "283c41"),
    ("dingoes", "D", "e4c6a5", "4b3542", "ac5940"),
    ("barry", "BJ", "222f46", "dfbc75", "8e5543"),
    ("juror", "RJ", "e4ddcd", "404542", "9b372c"),
    ("mayhem", "EM", "edc945", "5f3153", "d35a36"),
    ("soggy", "SB", "cabc95", "38443b", "8e563c"),
    ("stillwater", "S", "d8ab76", "462f2b", "a44b31"),
    ("mouse", "MR", "dce0c6", "244b49", "d27846"),
    ("blues", "BB", "d9dfdf", "233b57", "507d95"),
    ("cb4", "CB4", "e0be51", "272927", "a84532"),
    ("autobahn", "A", "d9dbd4", "28403e", "c9522e"),
]


def draw(index, letters, background, ink, accent, size):
    colours = [bytes.fromhex(c) for c in (background, ink, accent)]
    pixels = bytearray(colours[0] * size * size)

    def rect(x, y, w, h, colour):
        left, top, right, bottom = [round(v * size / 640) for v in (x, y, x + w, y + h)]
        for row in range(max(0, top), min(size, bottom)):
            a, b = max(0, left), min(size, right)
            pixels[(row * size + a) * 3 : (row * size + b) * 3] = colours[colour] * max(0, b - a)

    def disc(x, y, radius, colour):
        cx, cy, r = [v * size / 640 for v in (x, y, radius)]
        for row in range(max(0, int(cy - r)), min(size, int(cy + r) + 1)):
            reach = max(0, r * r - (row - cy) ** 2) ** 0.5
            a, b = max(0, int(cx - reach)), min(size, int(cx + reach))
            pixels[(row * size + a) * 3 : (row * size + b) * 3] = colours[colour] * max(0, b - a)

    rect(36, 36, 568, 3, 1)
    rect(36, 572, 568, 32, 1)
    if index % 4 == 0:
        for r, c in ((190, 1), (145, 0), (108, 2), (58, 0)):
            disc(370, 333, r, c)
    elif index % 4 == 1:
        for i in range(7):
            rect(190 + i * 53, 205 + (i % 3) * 32, 30, 315 - (i % 3) * 32, 1 if i % 2 else 2)
    elif index % 4 == 2:
        for i in range(5):
            rect(180 + i * 65, 195 + i * 53, 118, 110, 1 if i % 2 else 2)
    else:
        for i in range(3):
            disc(280 + i * 110, 315 + i * 45, 100, 1)
            disc(280 + i * 110, 315 + i * 45, 55, 2)
    for j, letter in enumerate(letters):
        for row, bits in enumerate(FONT[letter].split()):
            for col, bit in enumerate(bits):
                if bit == "1":
                    rect(38 + j * 66 + col * 10, 65 + row * 10, 9, 9, 1)
    for i in range(index + 1):
        rect(42 + i * 12, 583, 4, 9, 0)

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\0" + pixels[y * size * 3 : (y + 1) * size * 3] for y in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "src/harmonist/_demo_assets"
    for i, (name, letters, bg, ink, accent) in enumerate(SLEEVES):
        (root / f"{name}.png").write_bytes(
            draw(i, letters, bg, ink, accent, 320 if name == "mouse" else 640)
        )
        if name == "mouse":
            (root / "mouse-archive.png").write_bytes(draw(i, letters, bg, ink, accent, 960))
        if name == "barry":
            (root / "barry-archive.png").write_bytes(draw(i + 1, letters, bg, ink, accent, 320))
        if name == "juror":
            (root / "juror-track.png").write_bytes(draw(i + 1, letters, bg, ink, accent, 640))
