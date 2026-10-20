"""orbit-mpris: exposes each phone media activity mirrored by orbitd as an MPRIS player.

Talks to orbitd only through the local API (spec/LOCAL_API.md). Every media activity gets
its own session-bus connection owning ``org.mpris.MediaPlayer2.orbit_<dev8>_<sha8>``, so
desktop media widgets and ``playerctl`` list and control each phone player separately.
Linux only; ``dbus-fast`` is imported lazily so the pure mapping logic loads anywhere.
"""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import logging
import math
import os
import re
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from .client import ApiError, LocalClient
from .paths import socket_path, write_private

log = logging.getLogger("orbit.mpris")

MPRIS_PATH = "/org/mpris/MediaPlayer2"
BUS_NAME_PREFIX = "org.mpris.MediaPlayer2."
SEEK_JUMP_US = 1_500_000
ART_CACHE_FILES = 64
ART_RETRY_S = 60.0
RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 2.0

_STATUS = {"playing": "Playing", "paused": "Paused", "buffering": "Paused"}
_ART_EXT = {"image/png": ".png", "image/jpeg": ".jpg"}
_ART_KEY = re.compile(r"[A-Za-z0-9]{1,64}")

Perform = Callable[[str, str, str, int | None], Awaitable[None]]
Plan = tuple[str, int | None] | None


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _sha(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
        return None
    return x


def bus_name(device: str, activity: str) -> str:
    """Deterministic MPRIS bus name for one activity of one device."""
    dev8 = re.sub(r"[^A-Za-z0-9]", "", device)[:8] or "x"
    return f"{BUS_NAME_PREFIX}orbit_{dev8}_{_sha(device, activity)[:8]}"


def _has_ids(obj: Any, *keys: str) -> bool:
    return isinstance(obj, dict) and all(isinstance(obj.get(k), str) for k in keys)


def is_media(act: dict) -> bool:
    return act.get("kind") == "media" or isinstance(act.get("media"), dict)


class MediaView:
    """The MPRIS reading of one DeviceActivity."""

    def __init__(self, act: dict, device_name: str | None = None, art_path: Path | None = None) -> None:
        self.act = act
        self.media: dict = act["media"] if isinstance(act.get("media"), dict) else {}
        self.device: str = act["device"]
        self.id: str = act["id"]
        self.device_name = device_name
        self.art_path = art_path
        self.actions = {a.get("id") for a in act.get("actions") or [] if isinstance(a, dict)}

    @property
    def state(self) -> str:
        return self.media.get("state") or "stopped"

    @property
    def playing(self) -> bool:
        return self.state == "playing"

    @property
    def seekable(self) -> bool:
        return self.media.get("seekable") is True

    @property
    def rate(self) -> float:
        rate = _num(self.media.get("rate"))
        return float(rate) if rate is not None and rate > 0 else 1.0

    @property
    def playback_status(self) -> str:
        return _STATUS.get(self.state, "Stopped")

    @property
    def identity(self) -> str:
        app = self.act.get("app") or {}
        name = app.get("name") or app.get("id") or "Orbit"
        return f"{name} ({self.device_name})" if self.device_name else name

    @property
    def track_id(self) -> str:
        # Not under /org/mpris: the spec reserves that namespace.
        return f"/org/orbit/track/t{_sha(self.device, self.id, str(self.act.get('title') or ''))[:16]}"

    @property
    def length_us(self) -> int | None:
        duration = _num(self.media.get("durationMs"))
        return int(duration * 1000) if duration is not None and duration > 0 else None

    def position_us(self, now: int) -> int:
        pos = _num(self.media.get("positionMs")) or 0
        at = _num(self.media.get("positionAtMs"))
        if self.playing and at is not None:
            pos += max(0, now - at) * self.rate
        pos_us = max(0, int(pos * 1000))
        length = self.length_us
        return min(pos_us, length) if length is not None else pos_us

    def metadata(self) -> dict[str, tuple[str, Any]]:
        """``a{sv}`` metadata as ``{key: (signature, value)}``."""
        md: dict[str, tuple[str, Any]] = {"mpris:trackid": ("o", self.track_id)}
        if (length := self.length_us) is not None:
            md["mpris:length"] = ("x", length)
        if isinstance(title := self.act.get("title"), str):
            md["xesam:title"] = ("s", title)
        if isinstance(artist := self.act.get("subtitle"), str) and artist:
            md["xesam:artist"] = ("as", [artist])
        if isinstance(album := self.act.get("text"), str) and album:
            md["xesam:album"] = ("s", album)
        if self.art_path is not None:
            md["mpris:artUrl"] = ("s", self.art_path.as_uri())
        return md

    def root_props(self) -> dict[str, Any]:
        return {
            "CanQuit": False,
            "CanRaise": True,
            "CanSetFullscreen": False,
            "HasTrackList": False,
            "Identity": self.identity,
            "SupportedUriSchemes": [],
            "SupportedMimeTypes": [],
        }

    def player_props(self) -> dict[str, Any]:
        """Every Player property except Position, which never signals PropertiesChanged."""
        # Phones usually list only the transport action valid right now (pause while playing,
        # play while paused); the other half of the toggle is still a capability.
        play, pause = "media.play" in self.actions, "media.pause" in self.actions
        rate = self.rate
        return {
            "PlaybackStatus": self.playback_status,
            "Metadata": self.metadata(),
            "Rate": rate,
            "MinimumRate": min(1.0, rate),
            "MaximumRate": max(1.0, rate),
            "Volume": 1.0,
            "CanControl": True,
            "CanPlay": play or (self.playing and pause),
            "CanPause": pause or (not self.playing and play),
            "CanGoNext": "media.next" in self.actions,
            "CanGoPrevious": "media.previous" in self.actions,
            "CanSeek": self.seekable,
        }


# Method -> orbit action, following MPRIS no-op rules. Each returns (action, value) or None.


def plan_play_pause(v: MediaView) -> Plan:
    return ("media.pause" if v.playing else "media.play", None)


def plan_play(v: MediaView) -> Plan:
    return None if v.playing else ("media.play", None)


def plan_pause(v: MediaView) -> Plan:
    return None if v.state in ("paused", "stopped") else ("media.pause", None)


def plan_stop(v: MediaView) -> Plan:
    if "media.stop" in v.actions:
        return ("media.stop", None)
    return plan_pause(v)


def plan_next(v: MediaView) -> Plan:
    return ("media.next", None) if "media.next" in v.actions else None


def plan_previous(v: MediaView) -> Plan:
    return ("media.previous", None) if "media.previous" in v.actions else None


def plan_seek(v: MediaView, offset_us: int, now: int) -> Plan:
    if not v.seekable:
        return None
    target = max(0, v.position_us(now) + offset_us)
    if (length := v.length_us) is not None:
        target = min(target, length)
    return ("media.seek", target // 1000)


def plan_set_position(v: MediaView, track_id: str, position_us: int) -> Plan:
    length = v.length_us
    if not v.seekable or track_id != v.track_id or position_us < 0:
        return None
    if length is not None and position_us > length:
        return None
    return ("media.seek", position_us // 1000)


def seek_jump(old: MediaView, new: MediaView, now: int) -> int | None:
    """New position (us) if it departs from the old extrapolation enough to warrant ``Seeked``."""
    if old.track_id != new.track_id:
        return None
    pos = new.position_us(now)
    return pos if abs(pos - old.position_us(now)) > SEEK_JUMP_US else None


def changed(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in new.items() if old.get(k) != v}


def art_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "orbit" / "mpris-art"


class ArtCache:
    """Art images on disk, named by their content key, keeping at most ``max_files``."""

    def __init__(self, directory: Path, max_files: int = ART_CACHE_FILES) -> None:
        self.dir = directory
        self.max_files = max_files

    @staticmethod
    def valid_key(key: Any) -> bool:
        return isinstance(key, str) and _ART_KEY.fullmatch(key) is not None

    def lookup(self, key: str) -> Path | None:
        for ext in _ART_EXT.values():
            path = self.dir / f"{key}{ext}"
            with contextlib.suppress(OSError):
                os.utime(path)
                return path
        return None

    def store(self, key: str, mime: str, data_b64: str) -> Path:
        if not self.valid_key(key) or mime not in _ART_EXT:
            raise ValueError(f"unusable art {key!r} ({mime!r})")
        path = self.dir / f"{key}{_ART_EXT[mime]}"
        write_private(path, base64.b64decode(data_b64, validate=True))
        self._evict()
        return path

    def _evict(self) -> None:
        files = []
        for p in self.dir.iterdir():
            with contextlib.suppress(OSError):
                if p.is_file() and not p.name.startswith("."):
                    files.append((p.stat().st_mtime, p))
        files.sort()
        for _, p in files[: max(0, len(files) - self.max_files)]:
            with contextlib.suppress(OSError):
                p.unlink()


class Player(Protocol):
    @property
    def alive(self) -> bool: ...
    def update(self, view: MediaView) -> None: ...
    async def close(self) -> None: ...


PlayerFactory = Callable[[MediaView, Perform], Awaitable[Player]]


class ActionFailed(Exception):
    pass


def _present(device: dict | None) -> bool:
    """Players stay while the device is connected or briefly reconnecting (its media keys then wait for it).

    A device orbitd has not described yet counts as connected.
    """
    return device is None or device.get("connected", True) or device.get("state") == "reconnecting"


class Bridge:
    """Mirrors orbitd's media activities into players made by ``player_factory``."""

    def __init__(self, player_factory: PlayerFactory, art: ArtCache) -> None:
        self._factory = player_factory
        self._art = art
        self._client: LocalClient | None = None
        self._devices: dict[str, dict] = {}
        self._activities: dict[tuple[str, str], dict] = {}
        self.players: dict[tuple[str, str], Player] = {}
        self._art_tasks: dict[str, asyncio.Task] = {}
        self._art_failed: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def run(self, path: Path | str) -> None:
        delay = RECONNECT_MIN_S
        waiting = False
        while True:
            try:
                client = await LocalClient.connect(path)
            except OSError as e:
                (log.debug if waiting else log.info)("waiting for orbitd at %s (%s)", path, e)
                waiting = True
            else:
                waiting = False
                self._client = client
                try:
                    await client.call("subscribe")
                    log.info("connected to orbitd at %s", path)
                    async for event, data in client.events():
                        delay = RECONNECT_MIN_S
                        await self.handle(event, data)
                    log.info("orbitd closed the connection")
                except (ConnectionError, ApiError) as e:
                    log.warning("orbitd connection failed: %s", e)
                finally:
                    self._client = None
                    await client.close()
                    await self.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def handle(self, event: str, data: Any) -> None:
        if event == "devices.changed":
            self._devices = {d["id"]: d for d in data.get("devices", []) if _has_ids(d, "id")}
        elif event == "activities.changed":
            acts = data.get("activities", [])
            self._activities = {(a["device"], a["id"]): a for a in acts if _has_ids(a, "device", "id") and is_media(a)}
        else:
            return
        await self.sync()

    async def sync(self) -> None:
        async with self._lock:
            wanted = {k: a for k, a in self._activities.items() if _present(self._devices.get(k[0]))}
            for k in [k for k in self.players if k not in wanted]:
                log.info("removing player %s", bus_name(*k))
                await self.players.pop(k).close()
            for k, act in wanted.items():
                view = self._view(act)
                if (player := self.players.get(k)) is not None:
                    if player.alive:
                        player.update(view)
                        continue
                    log.warning("lost the session bus connection of %s", bus_name(*k))
                    await self.players.pop(k).close()
                try:
                    self.players[k] = await self._factory(view, self.perform)
                except Exception as e:
                    log.warning("cannot publish %s: %s", bus_name(*k), e)
                    continue
                log.info("published %s for %r", bus_name(*k), view.identity)

    async def clear(self) -> None:
        for task in self._art_tasks.values():
            task.cancel()
        self._art_tasks.clear()
        self._art_failed.clear()
        self._devices.clear()
        self._activities.clear()
        await self.sync()

    async def perform(self, device: str, activity: str, action: str, value: int | None = None) -> None:
        if self._client is None:
            raise ConnectionError("not connected to orbitd")
        params: dict[str, Any] = {"device": device, "activity": activity, "action": action}
        if value is not None:
            params["value"] = value
        log.debug("action %s", params)
        res = await self._client.call("activities.action", params)
        if not (isinstance(res, dict) and res.get("ok")):
            raise ActionFailed((res or {}).get("error") or f"{action} failed")

    def _view(self, act: dict) -> MediaView:
        device = self._devices.get(act["device"], {})
        key = act.get("artKey") or act.get("iconKey")
        path = None
        if ArtCache.valid_key(key):
            path = self._art.lookup(key)
            if path is None:
                self._fetch_art(act["device"], key)
        return MediaView(act, device.get("name"), path)

    def _fetch_art(self, device: str, key: str) -> None:
        if key in self._art_tasks or time.monotonic() - self._art_failed.get(key, -math.inf) < ART_RETRY_S:
            return
        self._art_tasks[key] = asyncio.create_task(self._fetch_art_task(device, key))

    async def _fetch_art_task(self, device: str, key: str) -> None:
        try:
            client = self._client
            if client is None:
                return
            res = await client.call("art.get", {"device": device, "key": key})
            self._art.store(key, res["mime"], res["data"])
        except (ApiError, ConnectionError, OSError, ValueError, KeyError, TypeError) as e:
            log.warning("art %s unavailable: %s", key, e)
            self._art_failed[key] = time.monotonic()
            return
        finally:
            self._art_tasks.pop(key, None)
        await self.sync()


def dbus_player_factory() -> PlayerFactory:
    """The D-Bus backend; imports dbus-fast, so Linux only."""
    from typing import Annotated

    from dbus_fast import BusType, DBusError, ErrorType, NameFlag, RequestNameReply, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.annotations import (
        DBusBool,
        DBusDict,
        DBusDouble,
        DBusInt64,
        DBusObjectPath,
        DBusSignature,
        DBusStr,
    )
    from dbus_fast.constants import PropertyAccess
    from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal

    DBusStrList = Annotated[list[str], DBusSignature("as")]
    READ = PropertyAccess.READ
    FAILED = "org.orbit.Error.ActionFailed"

    def variants(md: dict[str, tuple[str, Any]]) -> dict[str, Variant]:
        return {k: Variant(sig, v) for k, (sig, v) in md.items()}

    def wire(props: dict[str, Any]) -> dict[str, Any]:
        return {k: variants(v) if k == "Metadata" else v for k, v in props.items()}

    class _Iface(ServiceInterface):
        def __init__(self, name: str, player: "DBusPlayer") -> None:
            super().__init__(name)
            self._p = player

        async def _do(self, plan: Plan) -> None:
            if plan is None:
                return
            v = self._p.view
            try:
                await self._p.perform(v.device, v.id, plan[0], plan[1])
            except Exception as e:
                raise DBusError(FAILED, str(e) or type(e).__name__) from e

    class Root(_Iface):
        @dbus_method()
        async def Raise(self):
            await self._do(("open", None))

        @dbus_method()
        def Quit(self):
            pass

        @dbus_property(access=READ)
        def CanQuit(self) -> DBusBool:
            return False

        @dbus_property(access=READ)
        def CanRaise(self) -> DBusBool:
            return True

        @dbus_property(access=READ)
        def CanSetFullscreen(self) -> DBusBool:
            return False

        @dbus_property(access=READ)
        def HasTrackList(self) -> DBusBool:
            return False

        @dbus_property(access=READ)
        def Identity(self) -> DBusStr:
            return self._p.view.identity

        @dbus_property(access=READ)
        def SupportedUriSchemes(self) -> DBusStrList:
            return []

        @dbus_property(access=READ)
        def SupportedMimeTypes(self) -> DBusStrList:
            return []

    class PlayerIface(_Iface):
        @dbus_method()
        async def Next(self):
            await self._do(plan_next(self._p.view))

        @dbus_method()
        async def Previous(self):
            await self._do(plan_previous(self._p.view))

        @dbus_method()
        async def Pause(self):
            await self._do(plan_pause(self._p.view))

        @dbus_method()
        async def PlayPause(self):
            await self._do(plan_play_pause(self._p.view))

        @dbus_method()
        async def Stop(self):
            await self._do(plan_stop(self._p.view))

        @dbus_method()
        async def Play(self):
            await self._do(plan_play(self._p.view))

        @dbus_method()
        async def Seek(self, Offset: DBusInt64):
            await self._do(plan_seek(self._p.view, Offset, now_ms()))

        @dbus_method()
        async def SetPosition(self, TrackId: DBusObjectPath, Position: DBusInt64):
            await self._do(plan_set_position(self._p.view, TrackId, Position))

        @dbus_method()
        def OpenUri(self, Uri: DBusStr):
            raise DBusError(ErrorType.NOT_SUPPORTED, "opening URIs is not supported")

        @dbus_signal()
        def Seeked(self, position: int) -> DBusInt64:
            return position

        @dbus_property(access=READ)
        def PlaybackStatus(self) -> DBusStr:
            return self._p.view.playback_status

        @dbus_property()
        def Rate(self) -> DBusDouble:
            return self._p.view.rate

        @Rate.setter
        def Rate(self, value: DBusDouble):
            raise DBusError(ErrorType.NOT_SUPPORTED, "the phone controls the playback rate")

        @dbus_property(access=READ)
        def Metadata(self) -> DBusDict:
            return variants(self._p.view.metadata())

        @dbus_property()
        def Volume(self) -> DBusDouble:
            return 1.0

        @Volume.setter
        def Volume(self, value: DBusDouble):
            raise DBusError(ErrorType.NOT_SUPPORTED, "the phone controls the volume")

        @dbus_property(access=READ)
        def Position(self) -> DBusInt64:
            return self._p.view.position_us(now_ms())

        @dbus_property(access=READ)
        def MinimumRate(self) -> DBusDouble:
            return min(1.0, self._p.view.rate)

        @dbus_property(access=READ)
        def MaximumRate(self) -> DBusDouble:
            return max(1.0, self._p.view.rate)

        @dbus_property(access=READ)
        def CanGoNext(self) -> DBusBool:
            return self._p.view.player_props()["CanGoNext"]

        @dbus_property(access=READ)
        def CanGoPrevious(self) -> DBusBool:
            return self._p.view.player_props()["CanGoPrevious"]

        @dbus_property(access=READ)
        def CanPlay(self) -> DBusBool:
            return self._p.view.player_props()["CanPlay"]

        @dbus_property(access=READ)
        def CanPause(self) -> DBusBool:
            return self._p.view.player_props()["CanPause"]

        @dbus_property(access=READ)
        def CanSeek(self) -> DBusBool:
            return self._p.view.seekable

        @dbus_property(access=READ)
        def CanControl(self) -> DBusBool:
            return True

    # As in the MPRIS spec XML: clients must poll Position (and follow Seeked) instead of caching it.
    PlayerIface.Position.introspection.annotations["org.freedesktop.DBus.Property.EmitsChangedSignal"] = "false"

    class DBusPlayer:
        def __init__(self, view: MediaView, perform: Perform, bus: MessageBus) -> None:
            self.view = view
            self.perform = perform
            self.bus = bus
            self.name = bus_name(view.device, view.id)
            self.root = Root("org.mpris.MediaPlayer2", self)
            self.player = PlayerIface("org.mpris.MediaPlayer2.Player", self)

        @property
        def alive(self) -> bool:
            return self.bus.connected

        def update(self, view: MediaView) -> None:
            old, self.view = self.view, view
            if root := changed(old.root_props(), view.root_props()):
                self.root.emit_properties_changed(root)
            if player := changed(old.player_props(), view.player_props()):
                self.player.emit_properties_changed(wire(player))
            if (pos := seek_jump(old, view, now_ms())) is not None:
                self.player.Seeked(pos)

        async def close(self) -> None:
            self.bus.disconnect()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.bus.wait_for_disconnect(), 2)

    async def create(view: MediaView, perform: Perform) -> Player:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            p = DBusPlayer(view, perform, bus)
            bus.export(MPRIS_PATH, p.root)
            bus.export(MPRIS_PATH, p.player)
            reply = await bus.request_name(p.name, NameFlag.DO_NOT_QUEUE)
            if reply is not RequestNameReply.PRIMARY_OWNER:
                raise RuntimeError(f"{p.name} is already owned on the session bus")
        except BaseException:
            bus.disconnect()
            raise
        return p

    return create


async def _amain(args: argparse.Namespace) -> int:
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    try:
        probe = await MessageBus(bus_type=BusType.SESSION).connect()
    except Exception as e:
        print(f"orbit-mpris: no D-Bus session bus ({e})", file=sys.stderr)
        return 1
    probe.disconnect()

    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    bridge = Bridge(dbus_player_factory(), ArtCache(art_cache_dir()))
    try:
        await bridge.run(args.socket)
    except asyncio.CancelledError:
        await bridge.clear()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orbit-mpris", description="Expose orbitd's phone media as MPRIS players.")
    parser.add_argument("--socket", type=Path, help="orbitd socket (default: $ORBIT_SOCKET, else the platform path)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    args.socket = args.socket or socket_path()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="orbit-mpris: %(levelname)s %(message)s"
    )
    if not sys.platform.startswith("linux"):
        print(f"orbit-mpris: MPRIS is a Linux D-Bus interface; not supported on {sys.platform}", file=sys.stderr)
        return 2
    try:
        import dbus_fast  # noqa: F401
    except ImportError:
        print("orbit-mpris: the dbus-fast package is not installed", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(_amain(args))
    return 0
