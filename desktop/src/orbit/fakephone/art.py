"""Pure-Python PNG artwork for the scripted activities (album covers, app icons, avatars)."""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from collections.abc import Callable
from functools import cache

Pixel = tuple[int, int, int, int]


def art_key(data: bytes) -> str:
    """Art key per spec section 5: first 16 hex chars of SHA-256(data)."""
    return hashlib.sha256(data).hexdigest()[:16]


def encode_png(width: int, height: int, pixel: Callable[[int, int], Pixel]) -> bytes:
    """Encode an 8-bit RGBA PNG; ``pixel(x, y)`` returns (r, g, b, a)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def parse_hex(color: str) -> tuple[int, int, int]:
    value = int(color.lstrip("#"), 16)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(ca + (cb - ca) * t) for ca, cb in zip(a, b, strict=True))  # type: ignore[return-value]


@cache
def cover(color_a: str, color_b: str, size: int = 128) -> bytes:
    """Album cover: diagonal gradient with a soft disc in the middle."""
    a, b = parse_hex(color_a), parse_hex(color_b)
    c = (size - 1) / 2

    def pixel(x: int, y: int) -> Pixel:
        r, g, bl = _mix(a, b, (x + y) / (2 * (size - 1)))
        d = math.hypot(x - c, y - c) / c
        if 0.30 < d < 0.62:
            r, g, bl = _mix((r, g, bl), (255, 255, 255), 0.25)
        return r, g, bl, 255

    return encode_png(size, size, pixel)


@cache
def icon(color: str, glyph: str, size: int = 64) -> bytes:
    """App icon: coloured disc on transparency with a simple white glyph."""
    rgb = parse_hex(color)
    c = (size - 1) / 2
    s = size / 64

    def inside_glyph(x: float, y: float) -> bool:
        if glyph == "note":
            return math.hypot(x - 26 * s, y - 42 * s) < 7 * s or (31 * s <= x <= 35 * s and 16 * s <= y <= 42 * s)
        if glyph == "arrow":
            return (29 * s <= x <= 35 * s and 16 * s <= y <= 36 * s) or (
                36 * s <= y <= 46 * s and abs(x - 32 * s) <= (46 * s - y)
            )
        if glyph == "clock":
            ring = 14 * s < math.hypot(x - c, y - c) < 18 * s
            hands = (30 * s <= x <= 34 * s and 20 * s <= y <= 33 * s) or (
                32 * s <= x <= 42 * s and 30 * s <= y <= 34 * s
            )
            return ring or hands
        if glyph == "bubble":
            return math.hypot((x - c) / 1.25, y - 30 * s) < 13 * s or (22 * s <= x <= 30 * s and 38 * s <= y <= 46 * s)
        return False

    def pixel(x: int, y: int) -> Pixel:
        if math.hypot(x - c, y - c) > size / 2 - 1:
            return 0, 0, 0, 0
        if inside_glyph(x, y):
            return 255, 255, 255, 255
        return (*rgb, 255)

    return encode_png(size, size, pixel)
