"""Mock of the orbitd local API (spec/LOCAL_API.md) for testing and demoing frontends.

Stdlib only. Serves NDJSON over a Unix socket from an asyncio loop on a daemon thread;
the test-facing API (``MockOrbitd``) is thread-safe.

    uv run python tests/applet/mock_orbitd.py [--socket PATH] [--scenario demo|empty|idle|crowded]
        [--pairing-on-start] [--no-evolve] [--tick SECONDS]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import functools
import hashlib
import json
import logging
import math
import os
import secrets
import signal
import socket
import stat
import struct
import sys
import threading
import time
import zlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

log = logging.getLogger("mock_orbitd")

VERSION = "0.0.0-mock"
# sizeof(sockaddr_un.sun_path)
AF_UNIX_PATH_MAX = 104 if sys.platform == "darwin" else 108


def now_ms() -> int:
    return int(time.time() * 1000)


def device_id_for(seed: str) -> str:
    """Stable PROTOCOL.md 3.1 style id: hex of the first 16 bytes of SHA-256(static key)."""
    x = hashlib.sha256(f"orbit-mock:{seed}:x".encode()).digest()
    y = hashlib.sha256(f"orbit-mock:{seed}:y".encode()).digest()
    return hashlib.sha256(b"\x04" + x + y).digest()[:16].hex()


def art_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


PIXEL_8 = device_id_for("Pixel 8")
PIXEL_9_PRO = device_id_for("Pixel 9 Pro")  # real id; demo lists it provisionally by prefix
PROVISIONAL_REAL_IDS = {PIXEL_9_PRO[:16]: PIXEL_9_PRO}
GALAXY_TAB = device_id_for("Galaxy Tab S9")
WORK_PHONE = device_id_for("Work Phone")
PIXEL_7A = device_id_for("Pixel 7a")

SPOTIFY = "media:com.spotify.music"
DOWNLOAD = "notif:com.android.chrome:download"
TIMER = "notif:com.google.android.deskclock:timer"
MESSAGE = "notif:com.google.android.apps.messaging:alice"
BACKUP = "notif:com.google.android.apps.photos:backup"
NAVIGATION = "notif:com.google.android.apps.maps:navigation"

DEMO_SAS = "042917"


# --------------------------------------------------------------------------- PNG art


def encode_png(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride : (y + 1) * stride]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _hex(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b, strict=True))


def _cov(d: float) -> float:
    """Anti-aliased coverage of a signed distance (pixels, negative inside)."""
    return 0.0 if d >= 0.5 else 1.0 if d <= -0.5 else 0.5 - d


def _sd_circle(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r


def _sd_ring(x, y, cx, cy, r, w):
    return abs(math.hypot(x - cx, y - cy) - r) - w / 2


def _sd_segment(x, y, ax, ay, bx, by, r):
    px, py, dx, dy = x - ax, y - ay, bx - ax, by - ay
    h = max(0.0, min(1.0, (px * dx + py * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - dx * h, py - dy * h) - r


def _sd_arc(x, y, cx, cy, r, w, half_angle):
    """Round-capped arc centred on "up" (screen -y), spanning +-half_angle radians."""
    ang = math.atan2(x - cx, cy - y)
    if abs(ang) <= half_angle:
        return _sd_ring(x, y, cx, cy, r, w)
    end = math.copysign(half_angle, ang)
    ex, ey = cx + r * math.sin(end), cy - r * math.cos(end)
    return math.hypot(x - ex, y - ey) - w / 2


def _sd_round_box(x, y, cx, cy, hw, hh, rad):
    qx, qy = abs(x - cx) - hw + rad, abs(y - cy) - hh + rad
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - rad


def _sd_pin(x, y, cx, cy, r, tip_y):
    """Map pin: circle of radius r at (cx, cy) merged with a cone down to (cx, tip_y)."""
    d = _sd_circle(x, y, cx, cy, r)
    if cy <= y <= tip_y:
        half = r * (tip_y - y) / (tip_y - cy)
        slope = r / (tip_y - cy)
        d = min(d, (abs(x - cx) - half) / math.sqrt(1 + slope * slope))
    return d


Layer = tuple[Callable[[float, float], float], Callable[[float, float], tuple]]


def _render(
    size: int,
    background: Callable[[float, float], tuple],
    layers: list[Layer],
    clip: Callable[[float, float], float] | None = None,
) -> bytes:
    """Rasterise SDF layers over a background; returns PNG bytes."""
    buf = bytearray(size * size * 4)
    i = 0
    for py in range(size):
        y = py + 0.5
        for px in range(size):
            x = px + 0.5
            r, g, b = background(x, y)
            for sdf, color in layers:
                c = _cov(sdf(x, y))
                if c:
                    cr, cg, cb, ca = color(x, y)
                    a = c * ca
                    r, g, b = r + (cr - r) * a, g + (cg - g) * a, b + (cb - b) * a
            alpha = _cov(clip(x, y)) if clip else 1.0
            buf[i : i + 4] = bytes((int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5), int(alpha * 255 + 0.5)))
            i += 4
    return encode_png(size, size, buf)


def _solid(c: str, a: float = 1.0):
    rgb = _hex(c)
    return lambda x, y: (*rgb, a)


def _app_icon(bg: str, glyphs: list[tuple[Callable, str]], size: int = 96) -> bytes:
    s = size / 96
    top, bottom = _mix(_hex(bg), (1, 1, 1), 0.12), _mix(_hex(bg), (0, 0, 0), 0.08)
    layers = [(lambda x, y, f=f: f(x / s, y / s) * s, _solid(c)) for f, c in glyphs]
    return _render(
        size,
        lambda x, y: _mix(top, bottom, y / size),
        layers,
        clip=lambda x, y: _sd_circle(x, y, size / 2, size / 2, size / 2 - 0.5),
    )


@functools.cache
def app_icons() -> dict[str, bytes]:
    c = 48
    return {
        "spotify": _app_icon(
            "#1db954",
            [
                (lambda x, y: _sd_arc(x, y, c, 76, 40, 9, 0.72), "#191414"),
                (lambda x, y: _sd_arc(x, y, c, 80, 32, 7.5, 0.70), "#191414"),
                (lambda x, y: _sd_arc(x, y, c, 84, 25, 6, 0.66), "#191414"),
            ],
        ),
        "chrome": _app_icon(
            "#4285f4",
            [
                (lambda x, y: _sd_ring(x, y, c, c, 24, 8), "#ffffff"),
                (lambda x, y: _sd_circle(x, y, c, c, 11), "#ffffff"),
            ],
        ),
        "clock": _app_icon(
            "#fb8c00",
            [
                (lambda x, y: _sd_ring(x, y, c, c, 27, 6), "#ffffff"),
                (lambda x, y: _sd_segment(x, y, c, c, c, 29, 3), "#ffffff"),
                (lambda x, y: _sd_segment(x, y, c, c, 62, 56, 3), "#ffffff"),
                (lambda x, y: _sd_circle(x, y, c, c, 5), "#ffffff"),
            ],
        ),
        "messages": _app_icon(
            "#7e57c2",
            [
                (lambda x, y: _sd_round_box(x, y, c, 44, 25, 18, 11), "#ffffff"),
                (lambda x, y: _sd_segment(x, y, 38, 58, 31, 69, 4), "#ffffff"),
                (lambda x, y: _sd_circle(x, y, 37, 44, 3.6), "#7e57c2"),
                (lambda x, y: _sd_circle(x, y, 48, 44, 3.6), "#7e57c2"),
                (lambda x, y: _sd_circle(x, y, 59, 44, 3.6), "#7e57c2"),
            ],
        ),
        "maps": _app_icon(
            "#ffffff",
            [
                (lambda x, y: _sd_pin(x, y, c, 40, 19, 76), "#ea4335"),
                (lambda x, y: _sd_circle(x, y, c, 40, 7.5), "#ffffff"),
            ],
        ),
        "photos": _app_icon(
            "#ffffff",
            [
                (lambda x, y: _sd_circle(x, y, 39, 39, 13), "#ea4335"),
                (lambda x, y: _sd_circle(x, y, 57, 39, 13), "#fbbc04"),
                (lambda x, y: _sd_circle(x, y, 57, 57, 13), "#34a853"),
                (lambda x, y: _sd_circle(x, y, 39, 57, 13), "#4285f4"),
            ],
        ),
    }


def _album_art(corners: tuple[str, str, str, str], shapes: list, size: int = 192) -> bytes:
    """Four-corner gradient with a soft glow, then circles/rings given in unit coordinates."""
    tl, tr, bl, br = (_hex(c) for c in corners)
    layers: list[Layer] = []
    for kind, *args in shapes:
        if kind == "disc":
            cx, cy, r, c1, c2 = args
            a, b = _hex(c1), _hex(c2)
            cy0, cy1 = (cy - r) * size, (cy + r) * size
            layers.append(
                (
                    lambda x, y, cx=cx * size, cy=cy * size, r=r * size: _sd_circle(x, y, cx, cy, r),
                    lambda x, y, a=a, b=b, cy0=cy0, cy1=cy1: (
                        *_mix(a, b, min(1, max(0, (y - cy0) / (cy1 - cy0)))),
                        1.0,
                    ),
                )
            )
        elif kind == "ring":
            cx, cy, r, w, col, alpha = args
            layers.append(
                (
                    lambda x, y, cx=cx * size, cy=cy * size, r=r * size, w=w * size: _sd_ring(x, y, cx, cy, r, w),
                    _solid(col, alpha),
                )
            )

    def background(x, y):
        u, v = x / size, y / size
        base = _mix(_mix(tl, tr, u), _mix(bl, br, u), v)
        glow = max(0.0, 1 - math.hypot(u - 0.3, v - 0.25) / 0.75) ** 2 * 0.25
        return _mix(base, (1, 1, 1), glow)

    return _render(size, background, layers)


TRACKS = [
    {
        "title": "Golden Hour Drive",
        "artist": "Luna Vale",
        "album": "Coastal Static",
        "durationMs": 215000,
        "art": (
            ("#ff9a56", "#ff5e8a", "#7b3fa0", "#2b1b5c"),
            [
                ("disc", 0.5, 0.56, 0.24, "#ffe08a", "#ff7a59"),
                ("ring", 0.5, 0.56, 0.32, 0.012, "#ffffff", 0.55),
                ("ring", 0.5, 0.56, 0.40, 0.008, "#ffffff", 0.35),
                ("ring", 0.5, 0.56, 0.48, 0.005, "#ffffff", 0.2),
            ],
        ),
    },
    {
        "title": "Northern Signals",
        "artist": "Echo Avenue",
        "album": "Polar Bloom",
        "durationMs": 198000,
        "art": (
            ("#0f2b46", "#1c7c7d", "#3a1f63", "#38d9a9"),
            [
                ("ring", 0.32, 0.36, 0.20, 0.05, "#7ef0c8", 0.85),
                ("ring", 0.62, 0.62, 0.26, 0.035, "#9fb8ff", 0.7),
                ("disc", 0.62, 0.62, 0.11, "#e0fff4", "#6bd6ff"),
            ],
        ),
    },
    {
        "title": "Paper Satellites",
        "artist": "Mira Sol",
        "album": "Small Orbits",
        "durationMs": 243000,
        "art": (
            ("#ffd1e8", "#b8a4ff", "#7fd3ff", "#4d6bff"),
            [
                ("ring", 0.5, 0.5, 0.34, 0.01, "#ffffff", 0.8),
                ("ring", 0.5, 0.5, 0.21, 0.01, "#ffffff", 0.6),
                ("disc", 0.5, 0.5, 0.12, "#ffb36b", "#ff5f7e"),
                ("disc", 0.79, 0.30, 0.05, "#ffffff", "#c7d8ff"),
                ("disc", 0.33, 0.66, 0.035, "#fff3a6", "#ffc94d"),
            ],
        ),
    },
]


@functools.cache
def album_arts() -> tuple[bytes, ...]:
    return tuple(_album_art(*t["art"]) for t in TRACKS)


DOWNLOADS = [
    ("ubuntu-24.04-desktop-amd64.iso", 5.7e9),
    ("fedora-workstation-41-x86_64.iso", 2.3e9),
    ("debian-12.7.0-amd64-netinst.iso", 631e6),
]

NAV_STEPS = [
    ("Turn left onto Market St", 350),
    ("Continue onto 3rd St", 900),
    ("Turn right onto Mission St", 450),
    ("Your destination is on the right", 200),
]


def _fmt_bytes(n: float, unit_of: float) -> str:
    return f"{n / 1e9:.1f} GB" if unit_of >= 1e9 else f"{n / 1e6:.0f} MB"


def _fmt_distance(m: int) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m} m"


# --------------------------------------------------------------------------- daemon


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class _Conn:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.subscribed = False

    def send(self, obj: dict) -> None:
        if not self.writer.is_closing():
            self.writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")


def _act(label: str, icon: str, id: str, **extra) -> dict:
    return {"id": id, "label": label, "icon": icon, **extra}


class MockOrbitd:
    def __init__(
        self,
        socket_path: str,
        *,
        scenario: str = "demo",
        evolve: bool = True,
        tick_s: float = 1.0,
        pairing_on_start: bool = False,
    ):
        if scenario not in ("demo", "empty", "idle", "crowded"):
            raise ValueError(f"unknown scenario {scenario!r} (expected demo, empty, idle or crowded)")
        self.socket_path = socket_path
        self.evolve = evolve
        self.tick_s = tick_s
        # A str fails the next action with {"ok": false, "error": str}; a dict answers it with that error.
        self.fail_next_action: str | dict | None = None
        # Accept connections and requests but never answer (a wedged orbitd).
        self.unresponsive = False
        self.action_delay_s: float = 0.0
        self.method_delay_s: dict[str, float] = {}  # other methods answered this late
        # How long activities.action waits for a "reconnecting" device (orbitd: 5 s).
        self.reconnect_hold_s: float = 5.0
        # How long a dropped device stays "reconnecting" before going "offline" (orbitd: 15 s).
        self.reconnect_grace_s: float = 15.0
        self._reconnecting: dict[str, str | None] = {}  # device id -> transport to restore
        self._grace: dict[str, asyncio.TimerHandle] = {}
        # Called on the loop thread with each received {"method", "params"}.
        self.on_request: Callable[[dict], None] | None = None

        self._name = socket.gethostname().split(".")[0] or "localhost"
        self._id = device_id_for(f"desktop:{self._name}")
        self._devices: list[dict] = []
        self._pairings: list[dict] = []
        self._acts: list[dict] = []  # DeviceActivity incl. those of disconnected devices
        self._extra: dict[tuple[str, str], dict] = {}  # scenario behaviour per (device, activity)
        self._art: dict[str, tuple[str, bytes]] = {}
        self._art_lock = threading.Lock()
        self._rec_lock = threading.Lock()
        self._requests: list[dict] = []
        self._conns: set[_Conn] = set()
        self._tasks: set[asyncio.Task] = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

        if scenario != "empty":
            self._build_scenario(scenario, pairing_on_start)

    # ------------------------------------------------------------------ scenario

    def _build_scenario(self, scenario: str, pairing_on_start: bool) -> None:
        def dev(name, *, paired, connected, transport, address, discovered):
            return {
                "id": device_id_for(name),
                "name": name,
                "platform": "android",
                "paired": paired,
                "connected": connected,
                "transport": transport,
                "address": address,
                "discovered": discovered,
                "provisional": False,
            }

        self._devices.append(
            dev(
                "Pixel 8",
                paired=True,
                connected=True,
                transport="lan",
                address="192.168.1.20:47820",
                discovered=["lan", "ble"],
            )
        )
        if scenario == "idle":
            return
        if scenario == "crowded":
            self._build_crowded()
            return
        self._devices += [
            dev("Galaxy Tab S9", paired=True, connected=True, transport="ble", address=None, discovered=["ble"]),
            dev(
                "Work Phone", paired=True, connected=False, transport=None, address="192.168.1.44:47820", discovered=[]
            ),
            dev(
                "Pixel 7a",
                paired=False,
                connected=False,
                transport=None,
                address="192.168.1.31:47820",
                discovered=["lan"],
            ),
            # Seen only over BLE: listed under its advertised 8-byte id prefix until connected.
            {
                "id": PIXEL_9_PRO[:16],
                "name": "Pixel 9 Pro",
                "platform": "",
                "paired": False,
                "connected": False,
                "transport": None,
                "address": None,
                "discovered": ["ble"],
                "provisional": True,
            },
        ]
        if pairing_on_start:
            self._pairings.append(self._new_pairing(PIXEL_7A, "Pixel 7a", sas=DEMO_SAS))

        icons = {name: self.add_art(data) for name, data in app_icons().items()}
        self._album_keys = [self.add_art(data) for data in album_arts()]
        now = now_ms()

        media = {
            "device": PIXEL_8,
            "id": SPOTIFY,
            "kind": "media",
            "app": {"id": "com.spotify.music", "name": "Spotify"},
            "iconKey": icons["spotify"],
            "color": "#1db954",
            "media": {
                "state": "playing",
                "positionMs": 61000,
                "positionAtMs": now,
                "durationMs": 0,
                "rate": 1.0,
                "seekable": True,
            },
            "actions": [
                _act("Previous", "previous", "media.previous"),
                _act("Pause", "pause", "media.pause"),
                _act("Next", "next", "media.next"),
                _act("Like", "like", "notif.0"),
            ],
            "updatedMs": now,
        }
        self._extra[(PIXEL_8, SPOTIFY)] = {"role": "playlist", "index": 0}
        self._load_track(media, 0, now, position=61000)

        download = {
            "device": PIXEL_8,
            "id": DOWNLOAD,
            "kind": "progress",
            "app": {"id": "com.android.chrome", "name": "Chrome"},
            "iconKey": icons["chrome"],
            "color": "#4285f4",
            "progress": {"value": 21, "max": 100, "indeterminate": False},
            "actions": [_act("Pause", "pause", "notif.0"), _act("Cancel", "close", "notif.1")],
            "updatedMs": now,
        }
        self._extra[(PIXEL_8, DOWNLOAD)] = {"role": "download", "file": 0, "paused": False}
        self._describe_download(download)

        timer = {
            "device": PIXEL_8,
            "id": TIMER,
            "kind": "timer",
            "app": {"id": "com.google.android.deskclock", "name": "Clock"},
            "title": "Pasta",
            "subtitle": "Timer",
            "iconKey": icons["clock"],
            "color": "#fb8c00",
            "chrono": {"baseMs": now + 450_000, "countDown": True, "running": True},
            "actions": [
                _act("Pause", "pause", "notif.0"),
                _act("+1 min", "add", "notif.1"),
                _act("Stop", "stop", "notif.2"),
            ],
            "updatedMs": now,
        }
        self._extra[(PIXEL_8, TIMER)] = {"role": "timer"}

        message = {
            "device": PIXEL_8,
            "id": MESSAGE,
            "kind": "generic",
            "app": {"id": "com.google.android.apps.messaging", "name": "Messages"},
            "title": "Alice",
            "text": "Are we still on for 7? I can bring the board games",
            "iconKey": icons["messages"],
            "color": "#7e57c2",
            "actions": [
                _act("Reply", "reply", "notif.0", input={"kind": "text", "label": "Reply"}),
                _act("Mark as read", "check", "notif.1"),
            ],
            "updatedMs": now,
        }
        self._extra[(PIXEL_8, MESSAGE)] = {"role": "message"}

        backup = {
            "device": PIXEL_8,
            "id": BACKUP,
            "kind": "progress",
            "app": {"id": "com.google.android.apps.photos", "name": "Google Photos"},
            "title": "Backing up 23 photos",
            "subtitle": "Backup",
            "iconKey": icons["photos"],
            "color": "#fbbc04",
            "progress": {"value": 0, "max": 100, "indeterminate": True},
            "actions": [_act("Cancel", "close", "notif.0")],
            "updatedMs": now,
        }
        self._extra[(PIXEL_8, BACKUP)] = {"role": "dismissable"}

        navigation = {
            "device": GALAXY_TAB,
            "id": NAVIGATION,
            "kind": "navigation",
            "app": {"id": "com.google.android.apps.maps", "name": "Google Maps"},
            "iconKey": icons["maps"],
            "color": "#1a73e8",
            "chrono": {"baseMs": now + 12 * 60_000, "countDown": True, "running": True},
            "actions": [_act("Exit", "close", "notif.0")],
            "updatedMs": now,
        }
        self._extra[(GALAXY_TAB, NAVIGATION)] = {"role": "navigation", "step": 0, "remaining": NAV_STEPS[0][1]}
        self._describe_navigation(navigation, now)

        self._acts = [media, download, timer, message, backup, navigation]

    def _build_crowded(self) -> None:
        """Many and long actions, long texts: a Metrolist-like player as a real phone sends it."""
        icons = {name: self.add_art(data) for name, data in app_icons().items()}
        self._album_keys = [self.add_art(data) for data in album_arts()]
        now = now_ms()
        media = {
            "device": PIXEL_8,
            "id": "media:com.metrolist.music",
            "kind": "media",
            "app": {"id": "com.metrolist.music", "name": "Metrolist"},
            "title": "Sweater Weather (Extended Live Version Recorded at the Hollywood Bowl) [Remastered]",
            "subtitle": "The Neighbourhood, Featuring An Unreasonably Long List Of Guest Artists",
            "iconKey": icons["spotify"],
            "artKey": self._album_keys[1],
            "media": {
                "state": "playing",
                "positionMs": 26603,
                "positionAtMs": now,
                "durationMs": 252401,
                "rate": 1.0,
                "seekable": True,
            },
            # What the phone shows inline: prev/pause/next/like/repeat; the rest is overflow.
            "actions": [
                _act("Previous", "previous", "media.previous"),
                _act("Pause", "pause", "media.pause"),
                _act("Next", "next", "media.next"),
                _act("Stop", "stop", "media.stop", overflow=True),
                {"id": "media.custom.TOGGLE_LIKE", "label": "Remove like", "overflow": False},
                _act("Repeat mode off", "repeat", "media.custom.TOGGLE_REPEAT_MODE", overflow=False),
                _act("Shuffle on", "shuffle", "media.custom.TOGGLE_SHUFFLE", overflow=True),
                {"id": "media.custom.TOGGLE_START_RADIO", "label": "Start radio", "overflow": True},
                {"id": "media.custom.ADD_TO_TARGET_PLAYLIST", "label": "Quick-add destination", "overflow": True},
                {"id": "open", "label": "Open"},
            ],
            "updatedMs": now,
        }
        chat = {
            "device": PIXEL_8,
            "id": "notif:com.whatsapp:group",
            "kind": "generic",
            "app": {"id": "com.whatsapp", "name": "WhatsApp Messenger For Very Small Businesses"},
            "title": "Weekend hiking trip planning committee (official, please read the pinned message)",
            "subtitle": "Bartholomew-Alexander Montgomery-Smithington",
            "text": "Here is the route: https://maps.example.com/routes/very-long-route-identifier-"
            "0123456789abcdef0123456789abcdef0123456789abcdef and bring water.",
            "iconKey": icons["messages"],
            "color": "#25d366",
            "actions": [
                {"id": f"notif.{i}", "label": label}
                for i, label in enumerate(
                    [
                        "Mark as read",
                        "Mute conversation for 8 hours",
                        "Archive this chat",
                        "Call back now",
                        "Block and report this sender",
                        "Snooze for one hour",
                    ]
                )
            ]
            + [
                {
                    "id": "notif.6",
                    "label": "Reply",
                    "icon": "reply",
                    "input": {"kind": "text", "label": "Reply to the group"},
                }
            ],
            "updatedMs": now,
        }
        download = {
            "device": PIXEL_8,
            "id": "notif:com.android.chrome:bigdownload",
            "kind": "progress",
            "app": {"id": "com.android.chrome", "name": "Chrome"},
            "iconKey": icons["chrome"],
            "title": "Downloading a-file-with-an-absurdly-long-name-that-never-ends-v2.0.1-final-FINAL.zip",
            "subtitle": "1.2 GB of 5.7 GB",
            "progress": {"value": 21, "max": 100, "indeterminate": False},
            "actions": [
                {"id": "notif.0", "label": "Pause download", "icon": "pause"},
                {"id": "notif.1", "label": "Cancel download", "icon": "close"},
            ],
            "updatedMs": now,
        }
        self._acts = [media, chat, download]

    @staticmethod
    def _toggle_custom(act: dict, action_id: str) -> None:
        """Relabel Metrolist-style toggles the way the real app does."""
        cycles = {
            "TOGGLE_LIKE": ["Like", "Remove like"],
            "TOGGLE_SHUFFLE": ["Shuffle off", "Shuffle on"],
            "TOGGLE_REPEAT_MODE": ["Repeat mode off", "Repeat mode one", "Repeat mode all"],
        }
        cycle = cycles.get(action_id.removeprefix("media.custom."))
        for a in act.get("actions", []):
            if cycle and a.get("id") == action_id:
                i = cycle.index(a["label"]) if a.get("label") in cycle else -1
                a["label"] = cycle[(i + 1) % len(cycle)]

    def _new_pairing(self, device_id: str, name: str, sas: str | None = None) -> dict:
        return {
            "session": secrets.token_hex(8),
            "deviceId": device_id,
            "name": name,
            "sas": sas or f"{secrets.randbelow(10**6):06d}",
            "localAccepted": False,
            "remoteAccepted": True,
        }

    def _load_track(self, act: dict, index: int, now: int, position: int = 0) -> None:
        track = TRACKS[index]
        self._extra[(act["device"], act["id"])]["index"] = index
        act.update(title=track["title"], subtitle=track["artist"], text=track["album"], artKey=self._album_keys[index])
        act["media"].update(positionMs=position, positionAtMs=now, durationMs=track["durationMs"])

    def _describe_download(self, act: dict) -> None:
        extra = self._extra[(act["device"], act["id"])]
        name, total = DOWNLOADS[extra["file"]]
        done = total * act["progress"]["value"] / 100
        act["title"] = f"Downloading {name}"
        act["subtitle"] = f"{_fmt_bytes(done, total)} of {_fmt_bytes(total, total)}"
        if extra["paused"]:
            act["text"] = "Paused"
        else:
            act.pop("text", None)

    def _describe_navigation(self, act: dict, now: int) -> None:
        extra = self._extra[(act["device"], act["id"])]
        arrival = act["chrono"]["baseMs"]
        act["title"] = NAV_STEPS[extra["step"]][0]
        act["subtitle"] = _fmt_distance(extra["remaining"])
        eta = datetime.fromtimestamp(arrival / 1000).strftime("%H:%M")
        act["text"] = f"ETA {eta} · {max(1, math.ceil((arrival - now) / 60_000))} min"

    # ------------------------------------------------------------------ state helpers

    def _device(self, device_id: str) -> dict | None:
        return next((d for d in self._devices if d["id"] == device_id), None)

    def _listed_devices(self) -> list[dict]:
        out = copy.deepcopy(self._devices)
        for d in out:
            d["state"] = (
                "reconnecting" if d["id"] in self._reconnecting else "connected" if d["connected"] else "offline"
            )
        return out

    def _visible_activities(self) -> list[dict]:
        """Connected devices' activities, plus a reconnecting device's last ones marked stale."""
        hidden = {d["id"] for d in self._devices if not d["connected"] and d["id"] not in self._reconnecting}
        return [{**a, "stale": a["device"] in self._reconnecting} for a in self._acts if a["device"] not in hidden]

    def _find_act(self, device_id: str, activity_id: str) -> dict | None:
        return next((a for a in self._acts if a["device"] == device_id and a["id"] == activity_id), None)

    def _remove_act(self, act: dict) -> None:
        self._acts.remove(act)
        self._extra.pop((act["device"], act["id"]), None)

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {"devices": self._listed_devices(), "pairing": self._pairings, "activities": self._visible_activities()}
        )

    def _event(self, name: str, snap: dict) -> dict:
        key = {"devices": "devices", "pairing": "pairings", "activities": "activities"}[name]
        return {"event": f"{name}.changed", "data": {key: snap[name]}}

    def _mutate(self, fn: Callable[[], Any], force: tuple[str, ...] = ()) -> Any:
        """Run fn (on the loop thread) and broadcast an event for every section it changed."""
        before = self._snapshot()
        result = fn()
        after = self._snapshot()
        for name in ("devices", "pairing", "activities"):
            if name in force or before[name] != after[name]:
                event = self._event(name, after)
                for conn in list(self._conns):
                    if conn.subscribed:
                        conn.send(event)
        return result

    def _on_loop(self, fn: Callable[[], Any]) -> Any:
        loop = self._loop
        if loop is None or self._thread is None or threading.current_thread() is self._thread:
            return fn()

        async def run():
            return fn()

        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout=10)

    # ------------------------------------------------------------------ public API

    @property
    def requests(self) -> list[dict]:
        with self._rec_lock:
            return copy.deepcopy(self._requests)

    def actions(self) -> list[dict]:
        with self._rec_lock:
            return [copy.deepcopy(r["params"]) for r in self._requests if r["method"] == "activities.action"]

    def clear_recorded(self) -> None:
        with self._rec_lock:
            self._requests.clear()

    @property
    def connection_count(self) -> int:
        return self._on_loop(lambda: len(self._conns))

    @property
    def devices(self) -> list[dict]:
        return self._on_loop(self._listed_devices)

    def drop_device(self, device_id: str) -> None:
        """Simulate a dropped session: "reconnecting" with its activities kept but stale, then
        "offline" after reconnect_grace_s unless restore_device comes first."""

        def apply():
            dev = self._device(device_id)
            if dev is None or not dev["connected"]:
                raise ValueError(f"{device_id} is not connected")
            self._reconnecting[device_id] = dev["transport"]
            dev.update(connected=False, transport=None)
            self._grace[device_id] = self._loop.call_later(
                self.reconnect_grace_s, lambda: self._mutate(lambda: self._end_reconnect(device_id))
            )

        self._on_loop(lambda: self._mutate(apply))

    def _end_reconnect(self, device_id: str) -> str | None:
        handle = self._grace.pop(device_id, None)
        if handle is not None:
            handle.cancel()
        return self._reconnecting.pop(device_id, None)

    def restore_device(self, device_id: str) -> None:
        """The phone is back: fresh snapshot, nothing stale."""

        def apply():
            dev = self._device(device_id)
            if device_id not in self._reconnecting:
                raise ValueError(f"{device_id} is not reconnecting")
            dev.update(connected=True, transport=self._end_reconnect(device_id) or "lan")
            now = now_ms()
            for a in self._acts:
                if a["device"] == device_id:
                    a["updatedMs"] = now

        self._on_loop(lambda: self._mutate(apply))

    def expire_device(self, device_id: str) -> None:
        """End the grace period now: "offline", its activities gone from the list."""
        self._on_loop(lambda: self._mutate(lambda: self._end_reconnect(device_id)))

    @property
    def activities(self) -> list[dict]:
        """What activities.list returns (activities of disconnected devices are hidden)."""
        return self._on_loop(lambda: copy.deepcopy(self._visible_activities()))

    @property
    def pairings(self) -> list[dict]:
        return self._on_loop(lambda: copy.deepcopy(self._pairings))

    def set_devices(self, devices: list[dict]) -> None:
        devices = copy.deepcopy(devices)
        self._on_loop(lambda: self._mutate(lambda: setattr(self, "_devices", devices), force=("devices",)))

    def set_activities(self, activities: list[dict]) -> None:
        """Replace all activities; scenario behaviours are dropped (generic handling only)."""
        activities = copy.deepcopy(activities)

        def apply():
            self._acts = activities
            self._extra.clear()

        self._on_loop(lambda: self._mutate(apply, force=("activities",)))

    def set_pairings(self, pairings: list[dict]) -> None:
        pairings = copy.deepcopy(pairings)
        self._on_loop(lambda: self._mutate(lambda: setattr(self, "_pairings", pairings), force=("pairing",)))

    def update_activity(self, device: str, activity_id: str, **fields) -> None:
        fields = copy.deepcopy(fields)

        def apply():
            act = self._find_act(device, activity_id)
            if act is None:
                raise KeyError(f"no activity {activity_id!r} on device {device!r}")
            act.update(fields)
            act["updatedMs"] = now_ms()

        self._on_loop(lambda: self._mutate(apply, force=("activities",)))

    def add_art(self, data: bytes, mime: str = "image/png") -> str:
        key = art_key(data)
        with self._art_lock:
            self._art[key] = (mime, bytes(data))
        return key

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MockOrbitd is already running")
        encoded = os.fsencode(self.socket_path)
        if len(encoded) > AF_UNIX_PATH_MAX:
            raise ValueError(
                f"socket path is {len(encoded)} bytes, over the AF_UNIX limit of "
                f"{AF_UNIX_PATH_MAX} bytes on this platform: {self.socket_path}"
            )
        ready = threading.Event()
        errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._thread_main, args=(ready, errors), name="mock-orbitd", daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("mock orbitd did not start listening within 10 s")
        if errors:
            self._thread.join(5)
            self._thread = None
            self._loop = None
            raise errors[0]

    def stop(self) -> None:
        thread, loop = self._thread, self._loop
        if thread is None or loop is None:
            return
        loop.call_soon_threadsafe(self._stop_event.set)  # type: ignore[union-attr]
        thread.join(10)
        self._thread = None
        self._loop = None

    def _thread_main(self, ready: threading.Event, errors: list) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve(ready, errors))
        finally:
            loop.close()

    async def _serve(self, ready: threading.Event, errors: list) -> None:
        path = self.socket_path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)  # noqa: ASYNC240
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            server = await asyncio.start_unix_server(self._handle_conn, path=path, limit=1 << 22)
            os.chmod(path, 0o600)
            inode = os.stat(path).st_ino
        except BaseException as e:  # surfaced to start()'s caller
            errors.append(e)
            ready.set()
            return
        self._stop_event = asyncio.Event()
        ticker = asyncio.create_task(self._tick_loop()) if self.evolve else None
        ready.set()
        log.info("listening on %s", path)

        await self._stop_event.wait()

        if ticker:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        server.close()
        # abort(), not close(): close() keeps the fd open until buffered replies are flushed,
        # which never happens if the client is not reading. A dying daemon drops it at once.
        for conn in list(self._conns):
            conn.writer.transport.abort()
        if self._tasks:
            _, pending = await asyncio.wait(list(self._tasks), timeout=2)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
        try:
            await asyncio.wait_for(server.wait_closed(), 2)
        except TimeoutError:
            pass
        try:
            st = os.stat(path)
            if st.st_ino == inode and stat.S_ISSOCK(st.st_mode):
                os.unlink(path)
        except FileNotFoundError:
            pass
        log.info("stopped")

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        conn = _Conn(writer)
        self._conns.add(conn)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    log.warning("request line too long; closing connection")
                    break
                if not line:
                    break
                if line.strip():
                    await self._dispatch(conn, line)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._conns.discard(conn)
            self._tasks.discard(task)
            writer.close()

    async def _dispatch(self, conn: _Conn, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("ignoring unparseable line: %r", line[:200])
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            log.warning("ignoring line without a method: %r", line[:200])
            return
        req_id, method = msg.get("id"), msg["method"]
        params = msg.get("params")
        record = {"method": method, "params": copy.deepcopy(params) if params is not None else {}}
        with self._rec_lock:
            self._requests.append(record)
        if self.on_request:
            self.on_request(copy.deepcopy(record))
        if self.unresponsive:
            return

        if method == "activities.action":
            fail, self.fail_next_action = self.fail_next_action, None
            device = (params or {}).get("device") if isinstance(params, dict) else None
            if device in self._reconnecting:
                task = asyncio.create_task(self._answer_after_reconnect(conn, req_id, method, params, fail))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
            if self.action_delay_s > 0:
                task = asyncio.create_task(self._answer(conn, req_id, method, params, fail, self.action_delay_s))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
            await self._answer(conn, req_id, method, params, fail, 0)
        elif self.method_delay_s.get(method):
            task = asyncio.create_task(self._answer(conn, req_id, method, params, None, self.method_delay_s[method]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._answer(conn, req_id, method, params, None, 0)

    async def _answer_after_reconnect(self, conn, req_id, method, params, fail) -> None:
        """orbitd holds actions for a reconnecting device up to 5 s, then gives up."""
        deadline = time.monotonic() + self.reconnect_hold_s
        while params["device"] in self._reconnecting and time.monotonic() < deadline:  # noqa: ASYNC110 - polls test-mock state
            await asyncio.sleep(0.05)
        if params["device"] in self._reconnecting:
            conn.send({"id": req_id, "error": {"code": "not_connected", "message": "reconnecting"}})
            return
        await self._answer(conn, req_id, method, params, fail, 0)

    async def _answer(self, conn, req_id, method, params, fail, delay) -> None:
        if delay:
            await asyncio.sleep(delay)
        try:
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ApiError("bad_request", "params must be an object")
            if method == "subscribe":
                conn.send({"id": req_id, "result": True})
                conn.subscribed = True
                snap = self._snapshot()
                for name in ("devices", "pairing", "activities"):
                    conn.send(self._event(name, snap))
                return
            if method == "activities.action" and isinstance(fail, dict):
                conn.send({"id": req_id, "error": fail})
                return
            if method == "activities.action" and fail is not None:
                result: Any = {"ok": False, "error": fail}
            else:
                handler = self._handlers.get(method)
                if handler is None:
                    raise ApiError("bad_request", f"unknown method {method!r}")
                result = handler(self, params)
            conn.send({"id": req_id, "result": result})
        except ApiError as e:
            conn.send({"id": req_id, "error": {"code": e.code, "message": e.message}})
        except Exception as e:  # never kill the connection over a mock bug
            log.exception("handler for %s failed", method)
            conn.send({"id": req_id, "error": {"code": "failed", "message": str(e)}})

    # ------------------------------------------------------------------ methods

    @staticmethod
    def _str_param(params: dict, name: str) -> str:
        v = params.get(name)
        if not isinstance(v, str) or not v:
            raise ApiError("bad_request", f"missing or invalid {name!r}")
        return v

    def _m_status(self, params):
        return {"version": VERSION, "name": self._name, "deviceId": self._id}

    def _m_devices_list(self, params):
        return copy.deepcopy(self._devices)

    def _m_pairing_list(self, params):
        return copy.deepcopy(self._pairings)

    def _m_activities_list(self, params):
        return copy.deepcopy(self._visible_activities())

    def _m_art_get(self, params):
        self._str_param(params, "device")
        key = self._str_param(params, "key")
        with self._art_lock:
            entry = self._art.get(key)
        if entry is None:
            raise ApiError("not_found", f"unknown art key {key!r}")
        mime, data = entry
        return {"mime": mime, "data": base64.b64encode(data).decode()}

    def _m_devices_connect(self, params):
        if "id" in params:
            return self._mutate(lambda: self._connect_id(self._str_param(params, "id")))
        if "host" in params or "port" in params:
            host = self._str_param(params, "host")
            port = params.get("port")
            if not isinstance(port, int) or isinstance(port, bool):
                raise ApiError("bad_request", "missing or invalid 'port'")
            if host == "unreachable.invalid" or not 1 <= port <= 65535:
                raise ApiError("failed", "connection refused")
            address = f"{host}:{port}"
            existing = next((d for d in self._devices if d["address"] == address), None)
            if existing:
                return self._mutate(lambda: self._connect_id(existing["id"]))

            def create():
                dev = {
                    "id": device_id_for(address),
                    "name": f"Phone at {address}",
                    "platform": "android",
                    "paired": False,
                    "connected": True,
                    "transport": "lan",
                    "address": address,
                    "discovered": [],
                    "provisional": False,
                }
                self._devices.append(dev)
                self._pairings.append(self._new_pairing(dev["id"], dev["name"]))
                return {"id": dev["id"]}

            return self._mutate(create)
        raise ApiError("bad_request", "devices.connect needs 'id' or 'host' and 'port'")

    def _connect_id(self, device_id: str) -> dict:
        dev = self._device(device_id)
        if dev is None:
            raise ApiError("not_found", "unknown device")
        if dev.get("provisional"):
            # The handshake reveals the real id; the provisional entry is replaced in place, so
            # one devices.changed both drops the prefix and adds the real id.
            real = PROVISIONAL_REAL_IDS.get(device_id) or device_id_for(f"provisional:{device_id}")
            dev.update(id=real, platform="android", provisional=False)
            device_id = real
        if not dev["connected"]:
            dev["connected"] = True
            dev["transport"] = dev["discovered"][0] if dev["discovered"] else "lan"
        if not dev["paired"] and not any(p["deviceId"] == device_id for p in self._pairings):
            self._pairings.append(self._new_pairing(device_id, dev["name"]))
        return {"id": device_id}

    def _m_devices_disconnect(self, params):
        device_id = self._str_param(params, "id")

        def apply():
            dev = self._device(device_id)
            if dev is None:
                raise ApiError("not_found", "unknown device")
            dev["connected"] = False
            dev["transport"] = None
            self._end_reconnect(device_id)
            self._pairings = [p for p in self._pairings if p["deviceId"] != device_id]
            return True

        return self._mutate(apply)

    def _m_devices_forget(self, params):
        device_id = self._str_param(params, "id")

        def apply():
            dev = self._device(device_id)
            if dev is None:
                raise ApiError("not_found", "unknown device")
            self._devices.remove(dev)
            self._pairings = [p for p in self._pairings if p["deviceId"] != device_id]
            for act in [a for a in self._acts if a["device"] == device_id]:
                self._remove_act(act)
            return True

        return self._mutate(apply)

    def _m_pairing_respond(self, params):
        session = self._str_param(params, "session")
        accept = params.get("accept")
        if not isinstance(accept, bool):
            raise ApiError("bad_request", "missing or invalid 'accept'")

        def apply():
            pairing = next((p for p in self._pairings if p["session"] == session), None)
            if pairing is None:
                raise ApiError("not_found", "unknown pairing session")
            self._pairings.remove(pairing)
            dev = self._device(pairing["deviceId"])
            if dev is None:
                return True
            if accept:
                dev["paired"] = True
                if not dev["connected"]:
                    dev["connected"] = True
                    dev["transport"] = dev["discovered"][0] if dev["discovered"] else "lan"
            elif not dev["discovered"]:
                self._devices.remove(dev)  # ad-hoc host:port device nobody can see any more
            else:
                dev["connected"] = False
                dev["transport"] = None
            return True

        return self._mutate(apply, force=("pairing",))

    def _m_activities_action(self, params):
        device_id = self._str_param(params, "device")
        activity_id = self._str_param(params, "activity")
        action_id = self._str_param(params, "action")
        value = params.get("value")
        dev = self._device(device_id)
        act = self._find_act(device_id, activity_id)
        if dev is None and act is None:
            raise ApiError("not_found", "unknown device")
        if dev is not None and not dev["connected"]:
            raise ApiError("not_connected", "device is not connected")
        if dev is not None and not dev["paired"]:
            raise ApiError("not_paired", "device is not paired")
        if act is None:
            raise ApiError("not_found", "unknown activity")

        media = act.get("media")
        if action_id == "media.seek" and media and media.get("seekable"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ApiError("bad_request", "media.seek needs an integer position (ms)")
        else:
            spec = next((a for a in act.get("actions", []) if a.get("id") == action_id), None)
            if spec is None:
                return {"ok": False, "error": "unsupported action"}
            if (spec.get("input") or {}).get("kind") == "text" and not (isinstance(value, str) and value.strip()):
                return {"ok": False, "error": "missing text"}
        self._mutate(lambda: self._perform(act, action_id, value))
        return {"ok": True}

    _handlers = {
        "status": _m_status,
        "devices.list": _m_devices_list,
        "devices.connect": _m_devices_connect,
        "devices.disconnect": _m_devices_disconnect,
        "devices.forget": _m_devices_forget,
        "pairing.list": _m_pairing_list,
        "pairing.respond": _m_pairing_respond,
        "activities.list": _m_activities_list,
        "activities.action": _m_activities_action,
        "art.get": _m_art_get,
    }

    # ------------------------------------------------------------------ action effects

    @staticmethod
    def _media_position(media: dict, now: int) -> int:
        pos = media.get("positionMs", 0)
        if media.get("state") == "playing":
            pos += (now - media.get("positionAtMs", now)) * media.get("rate", 1.0)
        duration = media.get("durationMs")
        return int(max(0, min(pos, duration) if duration else pos))

    @staticmethod
    def _swap_action(act: dict, old_id: str, new: dict) -> None:
        act["actions"] = [new if a.get("id") == old_id else a for a in act.get("actions", [])]

    def _perform(self, act: dict, action_id: str, value: Any) -> None:
        now = now_ms()
        extra = self._extra.get((act["device"], act["id"]), {})
        role = extra.get("role")
        media = act.get("media")
        act["updatedMs"] = now

        if media is not None and action_id.startswith("media."):
            if action_id in ("media.play", "media.pause", "media.stop"):
                media["positionMs"] = self._media_position(media, now)
                media["positionAtMs"] = now
                if action_id == "media.play":
                    media["state"] = "playing"
                    self._swap_action(act, "media.play", _act("Pause", "pause", "media.pause"))
                else:
                    media["state"] = "paused" if action_id == "media.pause" else "stopped"
                    self._swap_action(act, "media.pause", _act("Play", "play", "media.play"))
            elif action_id == "media.seek":
                duration = media.get("durationMs")
                media["positionMs"] = max(0, min(value, duration) if duration else value)
                media["positionAtMs"] = now
            elif action_id.startswith("media.custom."):
                self._toggle_custom(act, action_id)
            elif action_id in ("media.next", "media.previous"):
                if role == "playlist":
                    step = 1 if action_id == "media.next" else -1
                    self._load_track(act, (extra["index"] + step) % len(TRACKS), now)
                else:
                    media["positionMs"] = 0
                    media["positionAtMs"] = now
            return

        if action_id == "dismiss":
            self._remove_act(act)
            return
        spec = next((a for a in act.get("actions", []) if a.get("id") == action_id), {})
        if (spec.get("input") or {}).get("kind") == "text":
            act["text"] = f"You: {value}"
            return

        if role == "playlist" and action_id == "notif.0":
            liked = spec.get("label") == "Like"
            self._swap_action(act, "notif.0", _act("Liked" if liked else "Like", "like", "notif.0"))
        elif role == "download":
            if action_id == "notif.0":
                extra["paused"] = not extra["paused"]
                self._swap_action(
                    act,
                    "notif.0",
                    _act("Resume", "play", "notif.0") if extra["paused"] else _act("Pause", "pause", "notif.0"),
                )
                self._describe_download(act)
            elif action_id == "notif.1":
                self._remove_act(act)
        elif role == "timer":
            chrono = act["chrono"]
            if action_id == "notif.0":
                if chrono["running"]:
                    chrono.update(running=False, valueMs=max(0, chrono["baseMs"] - now))
                    self._swap_action(act, "notif.0", _act("Resume", "play", "notif.0"))
                else:
                    chrono.update(running=True, baseMs=now + chrono.pop("valueMs", 0))
                    self._swap_action(act, "notif.0", _act("Pause", "pause", "notif.0"))
            elif action_id == "notif.1":
                if chrono["running"]:
                    chrono["baseMs"] += 60_000
                else:
                    chrono["valueMs"] = chrono.get("valueMs", 0) + 60_000
            elif action_id == "notif.2":
                self._remove_act(act)
        elif role in ("message", "dismissable", "navigation"):
            # Mark as read / Cancel / Exit
            self._remove_act(act)

    # ------------------------------------------------------------------ evolution

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self._mutate(self._tick)
            except Exception:
                log.exception("tick failed")

    def _tick(self) -> None:
        now = now_ms()
        for act in list(self._acts):
            if act["device"] in self._reconnecting:
                continue  # orbitd hears nothing from a dropped phone
            extra = self._extra.get((act["device"], act["id"]))
            if not extra:
                continue
            role = extra["role"]
            if role == "playlist":
                media = act["media"]
                if media["state"] == "playing" and self._media_position(media, now) >= media["durationMs"]:
                    self._load_track(act, (extra["index"] + 1) % len(TRACKS), now)
                    act["updatedMs"] = now
            elif role == "download" and not extra["paused"]:
                progress = act["progress"]
                progress["value"] += 3
                if progress["value"] >= 100:
                    progress["value"] = 0
                    extra["file"] = (extra["file"] + 1) % len(DOWNLOADS)
                self._describe_download(act)
                act["updatedMs"] = now
            elif role == "timer":
                chrono = act["chrono"]
                if chrono["running"] and chrono["baseMs"] <= now:
                    chrono.update(running=False, valueMs=0)
                    act["subtitle"] = "Time's up"
                    act["actions"] = [a for a in act["actions"] if a["id"] == "notif.2"]
                    act["updatedMs"] = now
            elif role == "navigation":
                extra["remaining"] -= 10
                if extra["remaining"] <= 0:
                    extra["step"] = (extra["step"] + 1) % len(NAV_STEPS)
                    extra["remaining"] = NAV_STEPS[extra["step"]][1]
                if act["chrono"]["baseMs"] <= now:
                    act["chrono"]["baseMs"] = now + 12 * 60_000
                self._describe_navigation(act, now)
                act["updatedMs"] = now


# --------------------------------------------------------------------------- CLI


def default_socket_path() -> str:
    if os.environ.get("ORBIT_SOCKET"):
        return os.environ["ORBIT_SOCKET"]
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "orbit", "orbitd.sock")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "orbit", "orbitd.sock")
    return os.path.join(home, ".local", "state", "orbit", "orbitd.sock")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mock orbitd local-API daemon")
    parser.add_argument("--socket", default=default_socket_path())
    parser.add_argument("--scenario", choices=("demo", "empty", "idle", "crowded"), default="demo")
    parser.add_argument("--pairing-on-start", action="store_true")
    parser.add_argument("--no-evolve", dest="evolve", action="store_false")
    parser.add_argument("--tick", type=float, default=1.0, metavar="SECONDS")
    parser.add_argument(
        "--flap",
        type=float,
        default=0,
        metavar="SECONDS",
        help='every SECONDS drop Pixel 8 to "reconnecting" for half that long',
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    mock = MockOrbitd(
        args.socket,
        scenario=args.scenario,
        evolve=args.evolve,
        tick_s=args.tick,
        pairing_on_start=args.pairing_on_start,
    )
    mock.on_request = lambda r: print(json.dumps(r), flush=True)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    mock.start()
    try:
        if args.flap > 0:
            while not done.wait(args.flap / 2):
                if PIXEL_8 in (d["id"] for d in mock.devices if d["state"] == "connected"):
                    mock.drop_device(PIXEL_8)
                elif PIXEL_8 in (d["id"] for d in mock.devices if d["state"] == "reconnecting"):
                    mock.restore_device(PIXEL_8)
        while not done.wait(0.5):
            pass
    finally:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
