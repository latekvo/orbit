"""Conversion of phone-clock timestamps into the local clock (spec/LOCAL_API.md, DeviceActivity)."""

from __future__ import annotations

import copy
import time
from typing import Any


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def to_local(activity: dict[str, Any], device: str, sender_now: int, offset: int) -> dict[str, Any]:
    """Return ``activity`` + ``device`` with timestamps shifted by ``offset`` (= local_now - sender_now).

    ``media.positionAtMs`` is the local time at which ``media.positionMs`` was valid, i.e. the
    message's ``now`` (spec 5: media positions are valid at the message's ``now``).
    """
    out = {**copy.deepcopy(activity), "device": device}
    if _num(out.get("updatedMs")):
        out["updatedMs"] = int(out["updatedMs"] + offset)
    chrono = out.get("chrono")
    if isinstance(chrono, dict) and _num(chrono.get("baseMs")):
        chrono["baseMs"] = int(chrono["baseMs"] + offset)
    media = out.get("media")
    if isinstance(media, dict):
        media["positionAtMs"] = int(sender_now + offset)
    return out
