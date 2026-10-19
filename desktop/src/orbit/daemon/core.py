"""orbitd state: devices, sessions, activities, art, pairing and (re)connection policy."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import sys
import time
from asyncio import FIRST_COMPLETED
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..client import ApiError
from ..paths import ensure_private_dir
from ..proto import FrameStream, device_id, initiate
from ..store import PeerStore, PinnedPeer, load_or_create_identity
from .session import ActionNotSent, Session
from .timeconv import to_local

log = logging.getLogger(__name__)

ART_MAX_ENTRIES = 64
ART_MAX_BYTES = 8 * 1024 * 1024
BLE_STALE_AFTER = 30.0
RECONNECT_MAX_DELAY = 60.0
STABLE_SESSION = 30.0
CONNECTOR_RESTART_DELAY = 5.0


@dataclass(frozen=True)
class Route:
    kind: str  # "lan" | "ble"
    address: str
    open: Callable[[], Awaitable[FrameStream]]
    expected: str | None = None
    manual: bool = False
    # For a provisional device: the responder's real id must start with this advertised prefix.
    prefix: str | None = None


@dataclass
class LanRecord:
    name: str
    platform: str
    addresses: list[tuple[str, int]]


@dataclass
class Device:
    id: str
    name: str = ""
    platform: str = ""
    session: Session | None = None
    lan: dict[str, LanRecord] = field(default_factory=dict)
    # BLE address -> (bleak device, last seen). Phones rotate their address, so several may be current.
    ble: dict[str, tuple[Any, float]] = field(default_factory=dict)
    # Set when an address not in ``ble`` is advertised; an attempt to an older address is abandoned.
    ble_moved: asyncio.Event = field(default_factory=asyncio.Event)
    activities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The activities are the last ones seen before the session dropped (until the next snapshot).
    stale: bool = False
    art: OrderedDict[str, tuple[str, bytes]] = field(default_factory=OrderedDict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    connector: asyncio.Task | None = None
    # Cleared by devices.disconnect so the device is not reconnected until the user asks again.
    auto: bool = True
    # Seen only via BLE and never connected: ``id`` is the advertised 16-hex prefix (spec/LOCAL_API.md).
    provisional: bool = False
    # Running while a paired session that dropped is expected back: state "reconnecting".
    grace: asyncio.TimerHandle | None = None
    lost_at: float | None = None
    # Replaced (after being set) whenever ``session`` or ``grace`` changes, to wake waiting actions.
    transition: asyncio.Event = field(default_factory=asyncio.Event)


def _combined(errors: list[ApiError]) -> ApiError:
    if len(errors) == 1:
        return errors[0]
    codes = {e.code for e in errors}
    detail = "; ".join(e.message for e in errors)
    return ApiError(codes.pop() if len(codes) == 1 else "failed", f"all {len(errors)} addresses failed: {detail}")


def format_hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_hostport(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected host:port, got {text!r}")
    return host.strip("[]"), int(port)


def local_platform() -> str:
    return "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform


async def open_tcp(host: str, port: int) -> FrameStream:
    reader, writer = await asyncio.open_connection(host, port)
    return FrameStream(reader, writer)


class Daemon:
    def __init__(
        self,
        config_dir: Path,
        name: str,
        socket_path: Path | None = None,
        *,
        mdns: bool = True,
        ble: bool = True,
        keepalive: float = 20.0,
        dead_after: float = 60.0,
        pairing_timeout: float = 120.0,
        action_timeout: float = 10.0,
        connect_timeout: float = 10.0,
        tcp_timeout: float = 3.0,
        ble_link_timeout: float = 4.0,
        stale_grace: float = 15.0,
        action_wait: float = 5.0,
    ) -> None:
        from .api import ApiServer

        ensure_private_dir(config_dir)
        self.name = name
        self.platform = local_platform()
        self.identity = load_or_create_identity(config_dir)
        self.device_id = device_id(self.identity.public)
        self.store = PeerStore(config_dir)
        self.keepalive = keepalive
        self.dead_after = dead_after
        self.pairing_timeout = pairing_timeout
        self.action_timeout = action_timeout
        self.connect_timeout = connect_timeout
        self.tcp_timeout = tcp_timeout
        self.ble_link_timeout = ble_link_timeout
        self.stale_grace = stale_grace
        self.action_wait = action_wait
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, Session] = {}
        self.api = ApiServer(self, socket_path)
        self._use_mdns = mdns
        self._use_ble = ble
        self._lan = None
        self._ble = None
        self._stopping = False
        self._dirty: set[str] = set()
        self._flush_scheduled = False
        self._bg: set[asyncio.Task] = set()
        for pin in self.store.peers.values():
            self.devices[pin.id] = Device(pin.id, pin.name, pin.platform)

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        await self.api.start()
        log.info("orbitd %s: %s (%s), local API at %s", __version__, self.name, self.device_id, self.api.path)
        if self._use_mdns:
            from .discovery import LanDiscovery

            self._lan = LanDiscovery(self.on_lan)
            await self._lan.start()
        if self._use_ble:
            from .discovery import BleDiscovery

            self._ble = BleDiscovery(self.on_ble)
            if not await self._ble.start():
                self._ble = None
            else:
                self._spawn(self._ble_expiry())
        for dev in self.devices.values():
            self._ensure_connector(dev)

    async def stop(self) -> None:
        self._stopping = True
        for dev in self.devices.values():
            if dev.connector:
                dev.connector.cancel()
        for task in list(self._bg):
            task.cancel()
        await asyncio.gather(*(s.close("orbitd shutting down", bye=True) for s in list(self.sessions.values())))
        if self._lan:
            await self._lan.stop()
        if self._ble:
            await self._ble.stop()
        await self.api.stop()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    # ---- events ----------------------------------------------------------------------------

    def changed(self, *kinds: str) -> None:
        """Mark event kinds dirty; subscribers get one coalesced event per kind on the next loop pass."""
        self._dirty.update(kinds)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            asyncio.get_running_loop().call_soon(self._flush)

    def _flush(self) -> None:
        self._flush_scheduled = False
        dirty, self._dirty = self._dirty, set()
        for kind in ("devices", "pairing", "activities"):
            if kind in dirty:
                self.api.broadcast(*self.event(kind))

    def event(self, kind: str) -> tuple[str, dict[str, Any]]:
        if kind == "devices":
            return "devices.changed", {"devices": self.devices_json()}
        if kind == "pairing":
            return "pairing.changed", {"pairings": self.pairings_json()}
        return "activities.changed", {"activities": self.activities_json()}

    # ---- views -----------------------------------------------------------------------------

    def _ble_fresh(self, dev: Device) -> list[Any]:
        """Bleak devices advertised within BLE_STALE_AFTER, most recently seen first."""
        now = time.monotonic()
        recent = sorted(dev.ble.values(), key=lambda e: e[1], reverse=True)
        return [d for d, seen in recent if now - seen < BLE_STALE_AFTER]

    def _ble_visible(self, dev: Device) -> bool:
        return bool(self._ble_fresh(dev))

    def device_json(self, dev: Device) -> dict[str, Any]:
        pin = self.store.get(dev.id)
        s = dev.session
        address = None
        if s:
            address = s.address
        elif pin and pin.address:
            address = pin.address
        elif dev.lan:
            host, port = next(iter(dev.lan.values())).addresses[0]
            address = format_hostport(host, port)
        return {
            "id": dev.id,
            "name": dev.name,
            "platform": dev.platform,
            "paired": pin is not None,
            "connected": s is not None,
            "state": "connected" if s else "reconnecting" if dev.grace else "offline",
            "transport": s.transport if s else None,
            "address": address,
            "discovered": (["lan"] if dev.lan else []) + (["ble"] if self._ble_visible(dev) else []),
            "provisional": dev.provisional,
        }

    def devices_json(self) -> list[dict[str, Any]]:
        return [self.device_json(d) for d in self.devices.values()]

    def pairings_json(self) -> list[dict[str, Any]]:
        return [
            {
                "session": s.id,
                "deviceId": s.peer.id,
                "name": s.peer.name,
                "sas": s.sas,
                "localAccepted": s.pairing.local is True,
                "remoteAccepted": s.pairing.remote is True,
            }
            for s in self.sessions.values()
            if s.pairing is not None
        ]

    def activities_json(self) -> list[dict[str, Any]]:
        return [{**a, "stale": d.stale} for d in self.devices.values() for a in d.activities.values()]

    # ---- device bookkeeping ----------------------------------------------------------------

    def _device(self, dev_id: str) -> Device:
        dev = self.devices.get(dev_id)
        if dev is None:
            dev = self.devices[dev_id] = Device(dev_id)
        return dev

    def _prune(self, dev: Device) -> None:
        if self.store.get(dev.id) or dev.session or dev.lan or self._ble_visible(dev):
            return
        if self.devices.pop(dev.id, None) is not None:
            self.changed("devices")

    def routes(self, dev: Device) -> list[Route]:
        return self._lan_routes(dev) + self._ble_routes(dev)

    def _lan_routes(self, dev: Device) -> list[Route]:
        routes: list[Route] = []
        seen: set[str] = set()

        def add_tcp(host: str, port: int, manual: bool) -> None:
            addr = format_hostport(host, port)
            if addr not in seen:
                seen.add(addr)
                routes.append(Route("lan", addr, lambda: open_tcp(host, port), dev.id, manual))

        for rec in dev.lan.values():
            for host, port in rec.addresses:
                add_tcp(host, port, False)
        pin = self.store.get(dev.id)
        if pin and pin.address:
            with contextlib.suppress(ValueError):
                add_tcp(*parse_hostport(pin.address), True)
        return routes

    def _ble_routes(self, dev: Device) -> list[Route]:
        from .ble import open_ble

        expected, prefix = (None, dev.id) if dev.provisional else (dev.id, None)
        return [
            Route("ble", ble.address, lambda b=ble: open_ble(b, self.ble_link_timeout), expected, prefix=prefix)
            for ble in self._ble_fresh(dev)
        ]

    # ---- discovery callbacks ---------------------------------------------------------------

    def on_lan(self, instance: str, info: dict[str, Any] | None) -> None:
        for dev in list(self.devices.values()):
            if instance in dev.lan and (info is None or info["id"] != dev.id):
                del dev.lan[instance]
                self.changed("devices")
                self._prune(dev)
        if info is None:
            return
        dev = self._device(info["id"])
        self._absorb_provisional(dev)
        dev.lan[instance] = LanRecord(info["name"], info["platform"], info["addresses"])
        if not dev.name:
            dev.name = info["name"]
        if not dev.platform:
            dev.platform = info["platform"]
        log.info("discovered %s (%s) via mDNS at %s", info["name"], dev.id, info["addresses"])
        self.changed("devices")
        dev.wake.set()
        if dev.session and dev.session.transport == "ble" and dev.auto:
            self._spawn(self._upgrade_to_lan(dev))

    def on_ble(self, prefix: str, ble_device: Any, local_name: str | None = None) -> None:
        matches = [d for d in self.devices.values() if not d.provisional and d.id.startswith(prefix)]
        if not matches:
            dev = self.devices.get(prefix)
            if dev is None:
                dev = self.devices[prefix] = Device(prefix, provisional=True)
            dev.name = local_name or dev.name or f"Orbit device {prefix[:4]}"
            matches = [dev]
        for dev in matches:
            fresh = not self._ble_visible(dev)
            moved = ble_device.address not in dev.ble
            dev.ble[ble_device.address] = (ble_device, time.monotonic())
            if fresh:
                log.info("discovered %s (%s) via BLE at %s", dev.name, dev.id, ble_device.address)
                self.changed("devices")
            elif moved:
                log.info("%s (%s) now advertises via BLE at %s", dev.name, dev.id, ble_device.address)
            if fresh or moved:
                dev.ble_moved.set()
                dev.wake.set()

    def _absorb_provisional(self, dev: Device) -> None:
        """``dev``'s real id is now known: fold the provisional BLE-only entry for its prefix into it."""
        prov = self.devices.get(dev.id[:16])
        if prov is None or not prov.provisional:
            return
        del self.devices[prov.id]
        for address, entry in prov.ble.items():
            if address not in dev.ble or dev.ble[address][1] < entry[1]:
                dev.ble[address] = entry
        self.changed("devices")

    async def _ble_expiry(self) -> None:
        while True:
            await asyncio.sleep(BLE_STALE_AFTER / 3)
            now = time.monotonic()
            for dev in list(self.devices.values()):
                kept = {a: e for a, e in dev.ble.items() if now - e[1] < BLE_STALE_AFTER}
                if len(kept) != len(dev.ble):
                    dev.ble = kept
                    if not kept:
                        self.changed("devices")
                        self._prune(dev)

    async def _upgrade_to_lan(self, dev: Device) -> None:
        with contextlib.suppress(ApiError):
            await self.connect_routes(self._lan_routes(dev), explicit=False)

    # ---- connecting ------------------------------------------------------------------------

    def _ensure_connector(self, dev: Device) -> None:
        if not self._stopping and (dev.connector is None or dev.connector.done()):
            dev.connector = asyncio.create_task(self._connector(dev))
            dev.connector.add_done_callback(lambda t, d=dev: self._connector_done(d, t))

    def _connector_done(self, dev: Device, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        log.error("connector for %s crashed; restarting", dev.id, exc_info=task.exception())
        if self.store.get(dev.id) and self.devices.get(dev.id) is dev:
            loop = asyncio.get_running_loop()
            loop.call_later(CONNECTOR_RESTART_DELAY, self._ensure_connector, dev)

    async def _wait_wake(self, dev: Device, timeout: float | None) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(dev.wake.wait(), timeout)
        dev.wake.clear()

    async def _connector(self, dev: Device) -> None:
        """Keep a paired device connected: connect when reachable, reconnect with exponential backoff.

        A session that stayed up for STABLE_SESSION is reconnected at once and resets the backoff;
        a phone that accepts and immediately drops us is not hammered.
        """
        delay = 1.0
        while not self._stopping and self.store.get(dev.id) and self.devices.get(dev.id) is dev:
            session = dev.session
            if session is None:
                if not dev.auto or not self.routes(dev):
                    await self._wait_wake(dev, BLE_STALE_AFTER if dev.ble else None)
                    continue
                try:
                    session = await self.connect_device(dev, explicit=False)
                except ApiError as e:
                    log.info("connecting to %s (%s) failed: %s; retrying in %g s", dev.name, dev.id, e, delay)
                    await self._wait_wake(dev, delay)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                    continue
            started = time.monotonic()
            await session.closed.wait()
            dev.wake.clear()  # set by session_closed itself; only later discovery changes may cut the wait short
            if time.monotonic() - started >= STABLE_SESSION:
                delay = 1.0
                continue
            await self._wait_wake(dev, delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def connect_device(self, dev: Device, *, explicit: bool) -> Session:
        """Try the LAN addresses, then each current BLE address, most recently advertised first.

        An attempt to a BLE address is abandoned as soon as the phone advertises a new one, which is
        tried next: a rotated-away address must not hold up a reachable one.
        """
        lan = self._lan_routes(dev)
        if not lan and not self._ble_visible(dev):
            raise ApiError("not_connected", "no known address for this device")
        errors: list[ApiError] = []
        for route in lan:
            try:
                return await self._connect_route(route, explicit=explicit)
            except ApiError as e:
                errors.append(e)
        tried: set[str] = set()
        while route := next((r for r in self._ble_routes(dev) if r.address not in tried), None):
            tried.add(route.address)
            dev.ble_moved.clear()
            try:
                return await self._connect_route(route, explicit=explicit, moved=dev.ble_moved)
            except ApiError as e:
                errors.append(e)
        raise _combined(errors)

    async def connect_routes(self, routes: list[Route], *, explicit: bool) -> Session:
        if not routes:
            raise ApiError("not_connected", "no known address for this device")
        errors: list[ApiError] = []
        for route in routes:
            try:
                return await self._connect_route(route, explicit=explicit)
            except ApiError as e:
                errors.append(e)
        raise _combined(errors)

    async def _open(self, route: Route, moved: asyncio.Event | None) -> FrameStream:
        opening = asyncio.ensure_future(route.open())
        waiter = asyncio.ensure_future(moved.wait()) if moved else None
        try:
            timeout = self.tcp_timeout if route.kind == "lan" else self.connect_timeout
            await asyncio.wait([t for t in (opening, waiter) if t], timeout=timeout, return_when=FIRST_COMPLETED)
        finally:
            if waiter:
                waiter.cancel()
            if not opening.done():
                opening.cancel()
                await asyncio.wait([opening])
        if opening.cancelled():
            if waiter and waiter.done() and not waiter.cancelled():
                raise ApiError("failed", f"gave up on {route.address}: the phone advertised a newer address")
            raise TimeoutError
        return opening.result()

    async def _connect_route(self, route: Route, *, explicit: bool, moved: asyncio.Event | None = None) -> Session:
        try:
            stream = await self._open(route, moved)
        except ApiError:
            raise
        except TimeoutError:
            raise ApiError("timeout", f"connecting to {route.address} timed out") from None
        except Exception as e:  # OSError for TCP; bleak raises its own exception types
            raise ApiError("failed", f"cannot connect to {route.address}: {e}") from None
        session = None
        try:
            channel = await asyncio.wait_for(
                initiate(stream, self.identity, self.name, self.platform), self.connect_timeout
            )
            peer = channel.peer
            if route.expected and peer.id != route.expected:
                raise ApiError("failed", f"{route.address} is device {peer.id}, expected {route.expected}")
            if route.prefix and not peer.id.startswith(route.prefix):
                raise ApiError("failed", f"{route.address} is device {peer.id}, not the advertised {route.prefix}")
            session = Session(self, channel, route.kind, route.address, route.manual)
            session.local_paired = self.store.is_pinned(peer.id, peer.static)
            await session.exchange_ready(self.connect_timeout)
            return await self._adopt(session, explicit)
        except ApiError:
            raise
        except TimeoutError:
            raise ApiError("timeout", f"handshake with {route.address} timed out") from None
        except Exception as e:  # ProtocolError, IncompleteReadError, OSError, transport-specific errors
            raise ApiError("failed", f"handshake with {route.address} failed: {e}") from None
        finally:
            if session is None or not session.started:
                stream.close()

    async def _adopt(self, session: Session, explicit: bool) -> Session:
        peer = session.peer
        dev = self._device(peer.id)
        self._absorb_provisional(dev)
        existing = dev.session
        if existing is not None and not existing.closed.is_set():
            if not (session.transport == "lan" and existing.transport == "ble"):
                await session.close("already connected", bye=True)
                return existing
            dev.session = None
            self._spawn(existing.close("switching to LAN", bye=True))
        dev.name, dev.platform = peer.name, peer.platform
        dev.session = session
        self.sessions[session.id] = session
        if dev.lost_at is not None:
            log.info(
                "reconnected device=%s transport=%s address=%s latency=%.2fs",
                peer.id,
                session.transport,
                session.address,
                time.monotonic() - dev.lost_at,
            )
            dev.lost_at = None
        self._end_grace(dev)
        log.info(
            "connected to %s (%s) via %s %s, paired=%s/%s, SAS %s",
            peer.name,
            peer.id,
            session.transport,
            session.address,
            session.local_paired,
            session.remote_paired,
            session.sas,
        )
        pin = self.store.get(peer.id)
        if pin and session.local_paired:
            address = session.address if session.manual else pin.address
            if (pin.name, pin.platform, pin.address) != (peer.name, peer.platform, address):
                pin.name, pin.platform, pin.address = peer.name, peer.platform, address
                self.store.save()
        session.start()
        if not session.paired:
            if explicit:
                try:
                    await session.request_pairing()
                except Exception as e:
                    await session.close(f"sending pair_request failed: {e}")
                    raise ApiError("failed", f"sending pair_request failed: {e}") from None
                self.changed("pairing")
            else:
                # Only pinned devices are auto-connected; the phone no longer has us pinned.
                log.warning("%s (%s) no longer trusts this computer; unpinning it", peer.name, peer.id)
                self.store.remove(peer.id)
                await session.close("phone does not trust this computer")
        self.changed("devices")
        return session

    def session_closed(self, session: Session) -> None:
        self.sessions.pop(session.id, None)
        dev = self.devices.get(session.peer.id)
        if dev is not None and dev.session is session:
            dev.session = None
            if session.paired and dev.auto and self.store.get(dev.id) and not self._stopping:
                # Expected back: keep the last activities, marked stale, for the grace period.
                dev.lost_at = time.monotonic()
                dev.stale = True
                dev.grace = asyncio.get_running_loop().call_later(self.stale_grace, self._grace_over, dev)
                self.changed("activities")
            else:
                self._clear_activities(dev)
            self._transition(dev)
            dev.wake.set()
            self._prune(dev)
        self.changed("devices", "pairing")

    def _transition(self, dev: Device) -> None:
        dev.transition.set()
        dev.transition = asyncio.Event()

    def _end_grace(self, dev: Device) -> None:
        if dev.grace is not None:
            dev.grace.cancel()
            dev.grace = None
            self._transition(dev)

    def _grace_over(self, dev: Device) -> None:
        dev.grace = None
        log.info("%s (%s) did not come back within %g s; dropping its activities", dev.name, dev.id, self.stale_grace)
        self._clear_activities(dev)
        self._transition(dev)
        self.changed("devices")

    def _clear_activities(self, dev: Device) -> None:
        dev.stale = False
        if dev.activities:
            dev.activities.clear()
            self.changed("activities")

    # ---- session callbacks -----------------------------------------------------------------

    def pair_completed(self, session: Session) -> None:
        peer = session.peer
        old = self.store.get(peer.id)
        address = session.address if session.manual else old.address if old else None
        self.store.pin(PinnedPeer(peer.id, peer.name, peer.platform, peer.static, address))
        log.info("paired with %s (%s)", peer.name, peer.id)
        dev = self._device(peer.id)
        dev.auto = True
        self._ensure_connector(dev)
        self.changed("devices", "pairing")

    def unpaired_by_phone(self, session: Session) -> None:
        log.info("%s (%s) unpaired this computer", session.peer.name, session.peer.id)
        self.store.remove(session.peer.id)
        dev = self.devices.get(session.peer.id)
        if dev:
            dev.art.clear()
        self.changed("devices")

    def _live(self, session: Session) -> Device | None:
        dev = self.devices.get(session.peer.id)
        return dev if dev is not None and dev.session is session else None

    def set_activities(self, session: Session, activities: list[dict[str, Any]], now: int) -> None:
        if dev := self._live(session):
            dev.activities = {a["id"]: to_local(a, dev.id, now, session.offset) for a in activities}
            dev.stale = False
            self.changed("activities")

    def upsert_activity(self, session: Session, activity: dict[str, Any], now: int) -> None:
        if dev := self._live(session):
            dev.activities[activity["id"]] = to_local(activity, dev.id, now, session.offset)
            self.changed("activities")

    def remove_activity(self, session: Session, activity_id: str) -> None:
        if (dev := self._live(session)) and dev.activities.pop(activity_id, None) is not None:
            self.changed("activities")

    def store_art(self, session: Session, msg: dict[str, Any]) -> None:
        dev = self._live(session)
        key, mime, data = msg.get("key"), msg.get("mime"), msg.get("data")
        if (
            dev is None
            or not isinstance(key, str)
            or mime not in ("image/png", "image/jpeg")
            or not isinstance(data, str)
        ):
            return
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            log.warning("art %s from %s is not valid base64", key, dev.id)
            return
        if hashlib.sha256(raw).hexdigest()[:16] != key:
            log.warning("art key %s from %s does not match its data", key, dev.id)
            return
        dev.art[key] = (mime, raw)
        dev.art.move_to_end(key)
        self._evict_art(dev)

    def _evict_art(self, dev: Device) -> None:
        """LRU-evict art over the limits, never art referenced by a current activity (it is not resent)."""
        referenced = {a.get(k) for a in dev.activities.values() for k in ("artKey", "iconKey")}
        for key in [k for k in dev.art if k not in referenced]:
            if len(dev.art) <= ART_MAX_ENTRIES and sum(len(v[1]) for v in dev.art.values()) <= ART_MAX_BYTES:
                break
            del dev.art[key]

    # ---- local API operations --------------------------------------------------------------

    def _known(self, dev_id: Any) -> Device:
        if not isinstance(dev_id, str) or not dev_id:
            raise ApiError("bad_request", "'id' must be a device id")
        dev = self.devices.get(dev_id)
        if dev is None:
            raise ApiError("not_found", f"unknown device {dev_id}")
        return dev

    async def api_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        if "host" in params or "port" in params:
            host, port = params.get("host"), params.get("port")
            if not isinstance(host, str) or not host or not isinstance(port, int) or not 0 < port < 65536:
                raise ApiError("bad_request", "'host' must be a string and 'port' an integer 1-65535")
            route = Route("lan", format_hostport(host, port), lambda: open_tcp(host, port), None, True)
            session = await self.connect_routes([route], explicit=True)
            return {"id": session.peer.id}
        dev = self._known(params.get("id"))
        dev.auto = True
        if dev.session is not None:
            return {"id": dev.id}
        session = await self.connect_device(dev, explicit=True)
        return {"id": session.peer.id}  # differs from the request for a provisional device

    async def api_disconnect(self, params: dict[str, Any]) -> bool:
        dev = self._known(params.get("id"))
        if dev.session is None and dev.grace is None:
            raise ApiError("not_connected", f"{dev.id} is not connected")
        dev.auto = False
        if dev.session is None:  # reconnecting: stop
            self._end_grace(dev)
            self._clear_activities(dev)
            self.changed("devices")
        else:
            await dev.session.close("disconnected by user", bye=True)
        return True

    async def api_forget(self, params: dict[str, Any]) -> bool:
        dev = self._known(params.get("id"))
        self.store.remove(dev.id)
        if dev.connector:
            dev.connector.cancel()
        if session := dev.session:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.send({"t": "unpair"}), 2)
            await session.close("forgotten by user")
        self._end_grace(dev)
        self._clear_activities(dev)
        dev.art.clear()
        self._prune(dev)
        self.changed("devices", "activities", "pairing")
        return True

    async def api_pairing_respond(self, params: dict[str, Any]) -> bool:
        sid, accept = params.get("session"), params.get("accept")
        if not isinstance(sid, str) or not isinstance(accept, bool):
            raise ApiError("bad_request", "'session' must be a string and 'accept' a boolean")
        session = self.sessions.get(sid)
        if session is None or session.pairing is None:
            raise ApiError("not_found", f"no pending pairing {sid}")
        await session.decide(accept)
        return True

    async def api_action(self, params: dict[str, Any]) -> dict[str, Any]:
        dev_id, activity, action = params.get("device"), params.get("activity"), params.get("action")
        value = params.get("value")
        if not all(isinstance(v, str) and v for v in (dev_id, activity, action)):
            raise ApiError("bad_request", "'device', 'activity' and 'action' must be non-empty strings")
        dev = self.devices.get(dev_id)
        if dev is None:
            raise ApiError("not_found", f"unknown device {dev_id}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.action_wait
        while True:
            session = dev.session
            if session is None:
                if dev.grace is None:
                    raise ApiError("not_connected", f"{dev_id} is not connected")
                # Reconnecting: hold the action until the session is back, up to action_wait.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(dev.transition.wait(), deadline - loop.time())
                if dev.session is None and loop.time() >= deadline:
                    raise ApiError("not_connected", "reconnecting")
                continue
            if not session.paired:
                raise ApiError("not_paired", f"{dev_id} is not paired")
            if activity not in dev.activities:
                raise ApiError("not_found", f"unknown activity {activity}")
            try:
                return await session.action(activity, action, value, "value" in params, self.action_timeout)
            except ActionNotSent as e:
                log.info("action %s on %s not sent (%s); waiting for the next session", action, dev_id, e)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(session.closed.wait(), max(0.0, deadline - loop.time()))

    def api_art(self, params: dict[str, Any]) -> dict[str, Any]:
        dev_id, key = params.get("device"), params.get("key")
        if not isinstance(dev_id, str) or not isinstance(key, str):
            raise ApiError("bad_request", "'device' and 'key' must be strings")
        dev = self.devices.get(dev_id)
        if dev is None or key not in dev.art:
            raise ApiError("not_found", f"no art {key} for device {dev_id}")
        dev.art.move_to_end(key)
        mime, raw = dev.art[key]
        return {"mime": mime, "data": base64.b64encode(raw).decode("ascii")}

    def api_status(self) -> dict[str, Any]:
        return {"version": __version__, "name": self.name, "deviceId": self.device_id}
