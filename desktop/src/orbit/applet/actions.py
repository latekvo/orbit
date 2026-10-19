"""Qt-free presentation rules for actions: which glyph, which slot, which toggle state.

Apps relabel toggle actions on every state change ("Like" <-> "Remove like", "Shuffle on" <->
"Shuffle off") while keeping the id, so the id decides the glyph and the slot, and the label
only refines the toggle state shown on that glyph.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping

# Protocol well-known icons (PROTOCOL.md 5.2).
WELL_KNOWN = {"play", "pause", "next", "previous", "stop", "close", "reply", "add", "remove",
              "check", "mute", "unmute", "like", "dislike", "shuffle", "repeat", "call_end"}

TRANSPORT_IDS = ("media.previous", "media.play", "media.pause", "media.next")
PLAY_PAUSE_SLOT = "media.playpause"

_ID_ICONS = {
    "media.play": "play", "media.pause": "pause", "media.next": "next",
    "media.previous": "previous", "media.stop": "stop", "open": "open", "dismiss": "close",
}

# (pattern on the upper-cased id, glyph); first match wins, so DISLIKE precedes LIKE.
_ID_KEYWORDS = [
    (r"DISLIKE|THUMBS?_?DOWN", "dislike"),
    (r"LIKE|FAVOU?RITE|HEART|THUMBS?_?UP", "like"),
    (r"SHUFFLE", "shuffle"),
    (r"REPEAT|LOOP", "repeat"),
    (r"RADIO", "radio"),
    (r"PLAYLIST", "playlist_add"),
    (r"ADD_TO|ENQUEUE|QUEUE", "add"),
    (r"UNMUTE", "unmute"),
    (r"MUTE", "mute"),
]

# (pattern on the lower-cased label, glyph), used only when neither id nor icon decide.
_LABEL_KEYWORDS = [
    (r"\bdislike\b|thumbs? down", "dislike"),
    (r"\b(un)?like[ds]?\b|favou?rite|thumbs? up", "like"),
    (r"\bshuffle\b", "shuffle"),
    (r"\brepeat\b|\bloop\b", "repeat"),
    (r"\bradio\b", "radio"),
    (r"playlist", "playlist_add"),
    (r"\breply\b|\brespond\b", "reply"),
    (r"\bunmute\b", "unmute"),
    (r"\bmute\b", "mute"),
    (r"\bmark as read\b|\bdone\b", "check"),
    (r"\bhang ?up\b|\bend call\b|\bdecline\b", "call_end"),
    (r"\bcancel\b|\bdismiss\b|\bclose\b|\bexit\b", "close"),
    (r"\bstop\b", "stop"),
    (r"\bpause\b", "pause"),
    (r"\bresume\b|\bplay\b", "play"),
    (r"\bskip\b|\bnext\b", "next"),
    (r"\bprevious\b", "previous"),
]

# Glyphs whose label conventionally carries an on/off state.
TOGGLE_GLYPHS = {"like", "dislike", "shuffle", "repeat"}

ToggleState = Literal["active", "inactive", "one"] | None


def icon_for(action: Mapping[str, Any]) -> str | None:
    """Glyph for an action: by id, then the `icon` hint, then label keywords; None = text only."""
    aid = str(action.get("id") or "")
    if aid in _ID_ICONS:
        return _ID_ICONS[aid]
    if aid.startswith("media.custom.") or not aid.startswith("notif."):
        for pattern, name in _ID_KEYWORDS:
            if re.search(pattern, aid.upper()):
                return name
    icon = action.get("icon")
    if icon in WELL_KNOWN:
        return icon
    label = str(action.get("label") or "").lower()
    for pattern, name in _LABEL_KEYWORDS:
        if re.search(pattern, label):
            return name
    return None


def toggle_state(action: Mapping[str, Any], glyph: str | None = None) -> ToggleState:
    """Current on/off state of a toggle, inferred from its label; None when ambiguous.

    Labels name the current state ("Shuffle on", "Repeat mode one") or, for likes, the way
    out of it ("Remove like" = liked)."""
    glyph = glyph or icon_for(action)
    if glyph not in TOGGLE_GLYPHS:
        return None
    label = str(action.get("label") or "").lower().strip()
    if glyph in ("like", "dislike"):
        if re.search(r"^(remove|undo|unlike|un-like|undislike)\b|\b(liked|disliked|favou?rited)$", label):
            return "active"
        if re.fullmatch(r"(add )?(dis)?like|(add to )?favou?rites?|thumbs? (up|down)", label):
            return "inactive"
        return None
    words = set(re.findall(r"[a-z0-9]+", label))
    if words & {"off", "disabled"}:
        return "inactive"
    if glyph == "repeat" and words & {"one", "1", "single", "track", "song"}:
        return "one"
    if words & {"on", "all", "enabled"}:
        return "active"
    return None


def slot_key(action: Mapping[str, Any], media: bool) -> str:
    """Identity of the button an action occupies; play and pause share one slot."""
    aid = str(action.get("id") or "")
    if media and aid in ("media.play", "media.pause"):
        return PLAY_PAUSE_SLOT
    return aid
