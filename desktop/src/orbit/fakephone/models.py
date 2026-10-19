"""Scripted activities simulated by the fake phone (spec/PROTOCOL.md section 5.1)."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import art

if TYPE_CHECKING:
    from .phone import FakePhone


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class ActionError(Exception):
    """An action the activity rejects; the message becomes ``action_result.error``."""


def action(id: str, label: str, icon: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"id": id, "label": label}
    if icon:
        out["icon"] = icon
    out.update(extra)
    return out


class Model:
    """One scripted activity. ``publish()`` upserts it to every paired session; ``hide()`` removes it."""

    id: str
    app: dict[str, str]

    def __init__(self, phone: FakePhone) -> None:
        self.phone = phone
        self.updated_ms = now_ms()
        self.retired = False  # set by FakePhone.remove(): never published again

    def start(self) -> None:
        self.publish()

    def stop(self) -> None:
        """Cancel pending timers (phone shutdown)."""

    def tick(self) -> None:
        """Called every ``phone.tick`` seconds."""

    def build(self, now: int) -> dict[str, Any]:
        raise NotImplementedError

    def act(self, action_id: str, value: Any) -> str:
        """Perform an action; return a one-line description of the resulting state or raise ActionError."""
        raise NotImplementedError

    def publish(self) -> None:
        if self.retired:
            return
        self.updated_ms = now_ms()
        self.phone._publish(self)

    def hide(self) -> None:
        self.phone._unpublish(self.id)

    def base(self, kind: str, title: str, actions: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
        activity: dict[str, Any] = {"id": self.id, "kind": kind, "app": dict(self.app), "title": title}
        activity.update({k: v for k, v in fields.items() if v is not None})
        activity["actions"] = actions
        activity["updatedMs"] = self.updated_ms
        return activity


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    duration_ms: int
    color: str
    color_b: str

    @property
    def art(self) -> bytes:
        return art.cover(self.color, self.color_b)


PLAYLIST = (
    Track("Neon Harbor", "The Midnight Static", "Afterglow Avenue", 215_000, "#1db954", "#0b3d2e"),
    Track("Paper Satellites", "Juniper Vale", "Low Orbit", 187_000, "#ff6b35", "#6a1b4d"),
    Track("Glass Weather", "Odd Atlas", "Small Hours", 243_000, "#3a86ff", "#14213d"),
    Track("Slow Comet", "Mira & the Tides", "Perihelion", 201_000, "#ffbe0b", "#8338ec"),
)


class Media(Model):
    id = "media:com.example.music"
    app = {"id": "com.example.music", "name": "Fake Music"}
    restart_threshold_ms = 3000  # media.previous restarts the track when past this position

    def __init__(self, phone: FakePhone, tracks: tuple[Track, ...] = PLAYLIST) -> None:
        super().__init__(phone)
        self.tracks = tracks
        self.track_index = 0
        self.state = "playing"
        self._pos_ms = 0
        self._anchor_ms = now_ms()  # epoch ms at which _pos_ms was valid
        self._end_handle: asyncio.TimerHandle | None = None
        self.icon_key = phone.add_art(art.icon("#1db954", "note"))
        self.art_keys = [phone.add_art(t.art) for t in tracks]

    @property
    def track(self) -> Track:
        return self.tracks[self.track_index]

    def position_ms(self, at: int | None = None) -> int:
        if self.state != "playing":
            return self._pos_ms
        at = now_ms() if at is None else at
        return min(self.track.duration_ms, self._pos_ms + max(0, at - self._anchor_ms))

    def _set(self, *, state: str | None = None, index: int | None = None, position: int | None = None) -> None:
        pos = self.position_ms() if position is None else position
        if index is not None:
            self.track_index = index
        if state is not None:
            self.state = state
        self._pos_ms = pos
        self._anchor_ms = now_ms()
        self._schedule_end()
        self.publish()

    def _schedule_end(self) -> None:
        if self._end_handle:
            self._end_handle.cancel()
            self._end_handle = None
        if self.state == "playing":
            remaining = self.track.duration_ms - self.position_ms()
            self._end_handle = asyncio.get_running_loop().call_later(max(0, remaining) / 1000, self._track_ended)

    def _track_ended(self) -> None:
        self._end_handle = None
        if self.state == "playing" and self.position_ms() >= self.track.duration_ms:
            self._set(index=(self.track_index + 1) % len(self.tracks), position=0)
            self.phone.log.info("media track ended -> %s", self.describe_track())
        else:
            self._schedule_end()

    def start(self) -> None:
        self._anchor_ms = now_ms()
        self._schedule_end()
        super().start()

    def stop(self) -> None:
        if self._end_handle:
            self._end_handle.cancel()
            self._end_handle = None

    def describe_track(self) -> str:
        return f"'{self.track.title}' by {self.track.artist}"

    def build(self, now: int) -> dict[str, Any]:
        t = self.track
        toggle = (
            action("media.pause", "Pause", "pause") if self.state == "playing" else action("media.play", "Play", "play")
        )
        return self.base(
            "media",
            t.title,
            [action("media.previous", "Previous", "previous"), toggle, action("media.next", "Next", "next")],
            subtitle=t.artist,
            text=t.album,
            iconKey=self.icon_key,
            artKey=self.art_keys[self.track_index],
            color=t.color,
            media={
                "state": self.state,
                "positionMs": self.position_ms(now),
                "durationMs": t.duration_ms,
                "rate": 1.0 if self.state == "playing" else 0.0,
                "seekable": True,
            },
        )

    def act(self, action_id: str, value: Any) -> str:
        n = len(self.tracks)
        if action_id == "media.play":
            if self.state != "playing":
                self._set(state="playing")
            return f"media playing {self.describe_track()} at {fmt_ms(self.position_ms())}"
        if action_id == "media.pause":
            if self.state == "playing":
                self._set(state="paused")
            return f"media {self.state} at {fmt_ms(self.position_ms())}"
        if action_id == "media.stop":
            self._set(state="stopped", position=0)
            return "media stopped"
        if action_id == "media.next":
            self._set(index=(self.track_index + 1) % n, position=0)
            return f"media next -> {self.describe_track()} ({self.state})"
        if action_id == "media.previous":
            if self.position_ms() > self.restart_threshold_ms:
                self._set(position=0)
                return f"media previous -> restart {self.describe_track()} ({self.state})"
            self._set(index=(self.track_index - 1) % n, position=0)
            return f"media previous -> {self.describe_track()} ({self.state})"
        if action_id == "media.seek":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ActionError("value must be an integer position in ms")
            position = max(0, min(value, self.track.duration_ms))
            self._set(state="paused" if self.state == "stopped" else None, position=position)
            return f"media seek to {fmt_ms(position)} ({self.state})"
        raise ActionError(f"unknown action {action_id}")


class Download(Model):
    id = "progress:download"
    app = {"id": "com.example.browser", "name": "Fake Browser"}
    file_name = "ubuntu-24.04.iso"
    size_gb = 5.7
    step = 1.5  # mean percent per tick (jittered, always >= 1), ~67 ticks per download
    linger_ticks = 5  # "Download complete" stays this long before it is removed
    restart_ticks = 15  # idle ticks before the next download starts

    def __init__(self, phone: FakePhone) -> None:
        super().__init__(phone)
        self.icon_key = phone.add_art(art.icon("#3a86ff", "arrow"))
        self.state = "idle"  # downloading | complete | idle
        self.progress = 0.0
        self.cancelled = False
        self.completed = 0
        self._ticks_in_state = 0
        self._rng = random.Random(0)

    @property
    def value(self) -> int:
        return min(100, int(self.progress))

    def start(self) -> None:
        self._begin()

    def _begin(self) -> None:
        self.state = "downloading"
        self.progress = 0.0
        self.cancelled = False
        self._ticks_in_state = 0
        self.publish()

    def tick(self) -> None:
        self._ticks_in_state += 1
        if self.state == "downloading":
            before = self.value
            self.progress = min(100.0, self.progress + self.step * self._rng.uniform(0.7, 1.3))
            if self.progress >= 100:
                self.state = "complete"
                self.completed += 1
                self._ticks_in_state = 0
                self.publish()
                self.phone.log.info("download complete: %s", self.file_name)
            elif self.value != before:
                self.publish()
        elif self.state == "complete" and self._ticks_in_state >= self.linger_ticks:
            self._go_idle()
        elif self.state == "idle" and self._ticks_in_state >= self.restart_ticks:
            self._begin()
            self.phone.log.info("download started: %s", self.file_name)

    def _go_idle(self) -> None:
        self.state = "idle"
        self._ticks_in_state = 0
        self.hide()

    def build(self, now: int) -> dict[str, Any]:
        if self.state == "complete":
            return self.base(
                "progress",
                "Download complete",
                [action("dismiss", "Dismiss", "close")],
                text=self.file_name,
                iconKey=self.icon_key,
                color="#3a86ff",
                progress={"value": 100, "max": 100, "indeterminate": False},
            )
        done = self.size_gb * self.value / 100
        return self.base(
            "progress",
            f"Downloading {self.file_name}",
            [action("notif.0", "Cancel", "close")],
            text=f"{done:.1f} GB of {self.size_gb} GB",
            iconKey=self.icon_key,
            color="#3a86ff",
            progress={"value": self.value, "max": 100, "indeterminate": False},
        )

    def act(self, action_id: str, value: Any) -> str:
        if action_id == "notif.0" and self.state == "downloading":
            self.cancelled = True
            self._go_idle()
            return f"download cancelled at {self.value}%"
        if action_id == "dismiss" and self.state == "complete":
            self._go_idle()
            return "download dismissed"
        raise ActionError(f"unknown action {action_id}")


class Timer(Model):
    id = "timer:countdown"
    app = {"id": "com.example.clock", "name": "Fake Clock"}

    def __init__(self, phone: FakePhone, duration_ms: int = 5 * 60_000) -> None:
        super().__init__(phone)
        self.icon_key = phone.add_art(art.icon("#ff6b35", "clock"))
        self.duration_ms = duration_ms
        self.state = "running"  # running | paused | done | dismissed
        self._end_ms = now_ms() + duration_ms
        self._paused_remaining = 0
        self._handle: asyncio.TimerHandle | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def remaining_ms(self) -> int:
        if self.state == "running":
            return max(0, self._end_ms - now_ms())
        if self.state == "paused":
            return self._paused_remaining
        return 0

    def start(self) -> None:
        self._end_ms = now_ms() + self.duration_ms
        self._schedule()
        super().start()

    def stop(self) -> None:
        if self._handle:
            self._handle.cancel()
            self._handle = None

    def _schedule(self) -> None:
        self.stop()
        if self.state == "running":
            self._handle = asyncio.get_running_loop().call_later(self.remaining_ms() / 1000, self._fire)

    def _fire(self) -> None:
        self._handle = None
        if self.state != "running":
            return
        if self.remaining_ms() > 0:
            self._schedule()
            return
        self.state = "done"
        self.publish()
        self.phone.log.info("timer done")

    def build(self, now: int) -> dict[str, Any]:
        plus = action("notif.1", "+1 min", "add")
        dismiss = action("dismiss", "Dismiss", "close")
        if self.state == "done":
            chrono = {"baseMs": now, "countDown": True, "running": False, "valueMs": 0}
            return self.base(
                "timer",
                "Timer done",
                [plus, dismiss],
                text="Time's up",
                iconKey=self.icon_key,
                color="#ff6b35",
                chrono=chrono,
            )
        if self.state == "running":
            toggle = action("notif.0", "Pause", "pause")
            chrono = {"baseMs": self._end_ms, "countDown": True, "running": True}
        else:
            toggle = action("notif.0", "Resume", "play")
            chrono = {
                "baseMs": now + self._paused_remaining,
                "countDown": True,
                "running": False,
                "valueMs": self._paused_remaining,
            }
        return self.base(
            "timer",
            "Timer",
            [toggle, plus, dismiss],
            subtitle="Paused" if self.state == "paused" else None,
            text=f"{fmt_ms(self.duration_ms)} timer",
            iconKey=self.icon_key,
            color="#ff6b35",
            chrono=chrono,
        )

    def act(self, action_id: str, value: Any) -> str:
        if action_id == "notif.0" and self.state in ("running", "paused"):
            if self.state == "running":
                self._paused_remaining = self.remaining_ms()
                self.state = "paused"
                verb = "paused"
            else:
                self._end_ms = now_ms() + self._paused_remaining
                self.state = "running"
                verb = "resumed"
            self._schedule()
            self.publish()
            return f"timer {verb} with {fmt_ms(self.remaining_ms())} left"
        if action_id == "notif.1":
            if self.state == "paused":
                self._paused_remaining += 60_000
            elif self.state == "running":
                self._end_ms += 60_000
            else:
                self.state = "running"
                self._end_ms = now_ms() + 60_000
            self._schedule()
            self.publish()
            return f"timer +1 min -> {fmt_ms(self.remaining_ms())} left ({self.state})"
        if action_id == "dismiss":
            self.state = "dismissed"
            self.stop()
            self.hide()
            return "timer dismissed"
        raise ActionError(f"unknown action {action_id}")


class Chat(Model):
    id = "msg:chat"
    app = {"id": "com.example.chat", "name": "Fake Chat"}
    sender = "Alice"
    message = "Are we still on for tonight?"

    def __init__(self, phone: FakePhone) -> None:
        super().__init__(phone)
        self.icon_key = phone.add_art(art.icon("#8338ec", "bubble"))
        self.avatar_key = phone.add_art(art.cover("#f72585", "#7209b7", 96))
        self.replies: list[str] = []
        self.read = False

    def build(self, now: int) -> dict[str, Any]:
        text = f"You: {self.replies[-1]}" if self.replies else self.message
        return self.base(
            "generic",
            self.sender,
            [
                action("notif.0", "Reply", "reply", input={"kind": "text", "label": "Reply"}),
                action("notif.1", "Mark as read", "check"),
            ],
            text=text,
            iconKey=self.icon_key,
            artKey=self.avatar_key,
            color="#8338ec",
        )

    def act(self, action_id: str, value: Any) -> str:
        if action_id == "notif.0":
            if not isinstance(value, str) or not value.strip():
                raise ActionError("text required")
            self.replies.append(value)
            self.publish()
            return f"chat reply to {self.sender}: {value!r}"
        if action_id == "notif.1":
            self.read = True
            self.hide()
            return "chat marked as read"
        raise ActionError(f"unknown action {action_id}")
