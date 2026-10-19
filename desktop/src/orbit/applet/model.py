"""Qt-free helpers: socket location, time extrapolation and formatting."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping


def now_ms() -> float:
    return time.time() * 1000


def default_socket_path(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Socket location per LOCAL_API.md."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("ORBIT_SOCKET"):
        return Path(env["ORBIT_SOCKET"])
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "orbit" / "orbitd.sock"
    if env.get("XDG_RUNTIME_DIR"):
        return Path(env["XDG_RUNTIME_DIR"]) / "orbit" / "orbitd.sock"
    return home / ".local" / "state" / "orbit" / "orbitd.sock"


def media_position_ms(media: Mapping[str, Any], now: float) -> float:
    """Current playback position, extrapolated while playing and clamped to the duration."""
    pos = float(media.get("positionMs") or 0)
    if media.get("state") == "playing":
        at = media.get("positionAtMs")
        if at is not None:
            pos += (now - float(at)) * float(media.get("rate", 1.0) or 0)
    duration = media.get("durationMs")
    if duration:
        pos = min(pos, float(duration))
    return max(pos, 0.0)


def chrono_value_ms(chrono: Mapping[str, Any], now: float) -> float:
    """Signed clock value: elapsed for count-up, remaining for count-down."""
    if not chrono.get("running", True):
        return float(chrono.get("valueMs") or 0)
    base = float(chrono.get("baseMs") or 0)
    return base - now if chrono.get("countDown") else now - base


def format_clock(ms: float) -> str:
    """M:SS or H:MM:SS, with a leading '-' for negative values (an overrun count-down)."""
    sign = "-" if ms < 0 else ""
    total = int(abs(ms) // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"


def format_sas(sas: str) -> str:
    return f"{sas[:3]} {sas[3:]}" if len(sas) == 6 else sas


def parse_host_port(text: str) -> tuple[str, int]:
    """Parse 'host:port' or '[v6]:port'. Raises ValueError with a user-facing message."""
    text = text.strip()
    if text.startswith("["):
        host, sep, port = text[1:].partition("]:")
    else:
        host, sep, port = text.rpartition(":")
    if not sep or not host or not port:
        raise ValueError("Enter an address like 192.168.1.20:47820")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"Invalid port: {port}")
    return host, int(port)


def device_status_text(device: Mapping[str, Any]) -> str:
    transport = {"lan": "Wi-Fi", "ble": "Bluetooth"}
    if device.get("connected"):
        via = transport.get(device.get("transport") or "", device.get("transport") or "")
        text = f"Connected via {via}" if via else "Connected"
        if device.get("transport") == "lan" and device.get("address"):
            text += f" - {device['address']}"
        return text
    seen = [transport.get(t, t) for t in device.get("discovered") or []]
    if seen:
        return "Nearby (" + ", ".join(seen) + ")"
    return "Offline"


def activity_key(activity: Mapping[str, Any]) -> tuple[str, str]:
    return (str(activity.get("device", "")), str(activity.get("id", "")))
