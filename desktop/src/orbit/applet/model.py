"""Qt-free helpers: socket location, time extrapolation and formatting."""

from __future__ import annotations

import copy
import math
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


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
    if not abs(ms) < CLOCK_MAX_MS:
        return "--:--"
    sign = "-" if ms < 0 else ""
    total = int(abs(ms) // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"


CLOCK_MAX_MS = 10_000 * 3600 * 1000  # past this a clock value is garbage, not time


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


def device_state(device: Mapping[str, Any]) -> str:
    """ "connected", "reconnecting" or "offline"; daemons without `state` only know connected."""
    state = device.get("state")
    if state in ("connected", "reconnecting", "offline"):
        return state
    return "connected" if device.get("connected") else "offline"


def device_status_text(device: Mapping[str, Any]) -> str:
    transport = {"lan": "Wi-Fi", "ble": "Bluetooth"}
    state = device_state(device)
    if state == "connected":
        via = transport.get(device.get("transport") or "", device.get("transport") or "")
        return f"Connected via {via}" if via else "Connected"
    if state == "reconnecting":
        return "Reconnecting..."
    seen = [transport.get(t, t) for t in device.get("discovered") or []]
    if seen:
        return "Nearby (" + ", ".join(seen) + ")"
    return "Offline"


def devices_summary(devices: list[Mapping[str, Any]]) -> str:
    """One line for the panel header and tray tooltip, e.g. "Pixel 8 connected · Tab reconnecting..."."""
    paired = [d for d in devices if d.get("paired")]
    parts = []
    for state, suffix in (("connected", "connected"), ("reconnecting", "reconnecting...")):
        names = [d.get("name") or "Phone" for d in paired if device_state(d) == state]
        if names:
            parts.append(f"{', '.join(names)} {suffix}")
    if parts:
        return " · ".join(parts)
    if len(paired) == 1:
        return f"{paired[0].get('name') or 'Phone'} offline"
    return "No phone connected"


# Error codes the client reports itself, next to orbitd's own (LOCAL_API.md).
ORBITD_DOWN = "orbitd_down"
ORBITD_UNRESPONSIVE = "orbitd_unresponsive"


def action_failure(error: Mapping[str, Any] | None, result: Any, phone: str, label: str) -> tuple[str, bool]:
    """What to tell the user about a failed activities.action, and whether it is a gentle notice
    (the phone may still get it) rather than an error."""
    if not error:
        reason = result.get("error") if isinstance(result, Mapping) else None
        return f"{label}: {reason or 'failed'}", False
    code, message = error.get("code"), str(error.get("message") or "")
    if "may have been applied" in message:
        return "Connection dropped - the action may have gone through", True
    if code == "not_connected":
        if message == "reconnecting":
            return f"Reconnecting to {phone}...", True
        return f"{phone} is offline", False
    if code == "timeout":
        return f"{phone} didn't respond", False
    if code == "not_paired":
        return f"{phone} isn't paired", False
    if code in (ORBITD_DOWN, ORBITD_UNRESPONSIVE):
        return message, False
    return f"{label}: {message or code or 'failed'}", False


INT32_MAX = 2**31 - 1  # Qt's int range: slider and progress bar values


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else ""


def _fields(raw: Any, nums: tuple[str, ...] = (), bools: tuple[str, ...] = (), texts: tuple[str, ...] = ()) -> dict:
    """The listed keys of `raw` that have the right type; the rest are dropped so .get() defaults apply."""
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict[str, Any] = {}
    for k in nums:
        if (v := _num(raw.get(k))) is not None:
            out[k] = v
    for k in bools:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    for k in texts:
        if isinstance(raw.get(k), str):
            out[k] = raw[k]
    return out


def sanitize_activity(raw: Mapping[str, Any]) -> dict:
    """A copy of a DeviceActivity with every field the UI reads in the type it expects. A phone app
    controls most of these values, and one odd field must not take the panel down."""
    a = copy.deepcopy(dict(raw))
    for k in ("device", "id", "kind", "title", "subtitle", "text", "iconKey", "artKey", "color"):
        a[k] = _text(raw.get(k))
    a["app"] = {k: _text(v) for k, v in _fields(raw.get("app"), texts=("id", "name")).items()}
    a["stale"] = raw.get("stale") is True
    specs = {
        "media": (("positionMs", "positionAtMs", "durationMs", "rate"), ("seekable",), ("state",)),
        "chrono": (("baseMs", "valueMs"), ("running", "countDown"), ()),
        "progress": (("value", "max"), ("indeterminate",), ()),
    }
    for k, (nums, bools, texts) in specs.items():
        if isinstance(raw.get(k), Mapping):
            a[k] = _fields(raw[k], nums, bools, texts)
        else:
            a.pop(k, None)
    if a.get("chrono", {}).get("running", True) and "baseMs" not in a.get("chrono", {"baseMs": 0}):
        del a["chrono"]  # a running clock without a base has nothing to count from
    actions = []
    for x in raw.get("actions") if isinstance(raw.get("actions"), list) else []:
        if not isinstance(x, Mapping) or not isinstance(x.get("id"), str) or not x["id"]:
            continue
        action = {"id": x["id"], **_fields(x, bools=("active", "overflow"), texts=("label", "icon"))}
        if isinstance(x.get("input"), Mapping):
            action["input"] = _fields(x["input"], texts=("kind", "label"))
        actions.append(action)
    a["actions"] = actions
    return a


def activity_key(activity: Mapping[str, Any]) -> tuple[str, str]:
    return (str(activity.get("device", "")), str(activity.get("id", "")))
