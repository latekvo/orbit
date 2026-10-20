"""orbitd state: devices, sessions, activities, art, pairing and (re)connection policy."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import errno
import fcntl
import hashlib
import ipaddress
import logging
import os
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
from ..proto import FrameStream, ProtocolError, device_id, initiate
from ..store import PeerStore, PinnedPeer, load_or_create_identity
from .api import AlreadyRunning
from .session import NO_RESPONSE, ActionNotSent, Session, WatchedReader, WatchedWriter
from .timeconv import shift_local, to_local

log = logging.getLogger(__name__)

ART_MAX_ENTRIES = 64
ART_MAX_BYTES = 8 * 1024 * 1024
BLE_STALE_AFTER = 30.0
# A gap this long between two advertisements means the phone was away and is back: retry now.
BLE_RETURN_GAP = 2.5
# Reconnect delays after failed attempts: short while the device is expected back (its stale
# grace period), then RECONNECT_SOON_MAX for RECONNECT_SOON_FOR, then RECONNECT_MAX_DELAY.
RECONNECT_GRACE_MAX = 2.0
RECONNECT_SOON_MAX = 5.0
RECONNECT_SOON_FOR = 300.0
RECONNECT_MAX_DELAY = 30.0
# Woken by discovery, the connector still waits this long after a failed attempt.
RECONNECT_MIN_GAP = 0.5
# A session that lasted this long counts as healthy even if it carried no snapshot.
STABLE_SESSION = 30.0
CONNECTOR_RESTART_DELAY = 5.0
BLE_RETRY_MIN, BLE_RETRY_MAX = 2.0, 5.0
# Adapter state (powered) is polled this often while scanning.
BLE_WATCH_INTERVAL = 2.0
LAN_RETRY = 10.0
# Happy eyeballs: the next LAN address is tried this long after the previous one (or as soon as it fails).
LAN_STAGGER = 0.25
# BLE is tried this long after the LAN attempts began (at once if every LAN address failed recently).
BLE_AFTER_LAN = 2.0
# An address that failed is tried after the others for this long.
FAILED_ADDRESS_FOR = 60.0
# An address that answered as a different device is not tried automatically for this long.
MISMATCH_FOR = 600.0
# A device reachable only at its manually entered address that has not answered for this long is
# shown with a problem: the phone may have a new address.
MANUAL_SILENT_FOR = 60.0
# Liveness checks (spec/LOCAL_API.md): after a wake, a network change or before reusing a quiet session.
PROBE_TIMEOUT = 4.0
# A LAN session silent this long is checked (answer within LAN_CHECK) when the phone's BLE advertisement shows up.
LAN_QUIET_WITH_BLE = 4.0
LAN_CHECK = 2.0
CLOCK_TICK = 2.0
CLOCK_JUMP = 3.0
LOCK_FILE = "orbitd.lock"

Plan = tuple[float, Callable[[], Awaitable["Session"]]]  # (start offset, attempt) for Daemon._race


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
    # Art keys announced in our ``ready``: the phone does not resend them, so they are not evicted.
    art_kept: set[str] = field(default_factory=set)
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
    # LAN address -> when connecting to it last failed.
    failed: dict[str, float] = field(default_factory=dict)
    # LAN address -> (id of the device that answered there instead, when).
    mismatch: dict[str, tuple[str, float]] = field(default_factory=dict)
    # Why the device cannot be used as it is (shown to the user), else None.
    problem: str | None = None
    upgrading: bool = False
    # The connector is in the middle of an attempt.
    connecting: bool = False


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


def address_order(host: str) -> tuple[int, int, str]:
    """IPv4, routable IPv6, IPv4 link-local, IPv6 link-local (it needs a scope); numerically within each."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return (9, 0, host)
    if ip.version == 4:
        rank = 2 if ip.is_loopback or ip.is_link_local else 0
    else:
        rank = 3 if ip.is_link_local else 1
    return (rank, int(ip), host)


def local_platform() -> str:
    return "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform


async def open_tcp(host: str, port: int) -> FrameStream:
    reader, writer = await asyncio.open_connection(host, port)
    return FrameStream(reader, writer)


def _sleep_clock() -> float | None:
    """A clock that keeps counting while the computer sleeps (time.monotonic does not)."""
    clock = getattr(time, "CLOCK_BOOTTIME", None) if sys.platform.startswith("linux") else None
    if sys.platform == "darwin":
        clock = time.CLOCK_MONOTONIC  # mach_continuous_time; time.monotonic is mach_absolute_time
    return None if clock is None else time.clock_gettime(clock)


class Daemon:
    def __init__(
        self,
        config_dir: Path,
        name: str,
        socket_path: Path | None = None,
        *,
        mdns: bool = True,
        ble: bool = True,
        keepalive: float = 10.0,
        dead_after: float = 30.0,
        stall_after: float = 10.0,
        send_timeout: float = 5.0,
        pairing_timeout: float = 120.0,
        action_timeout: float = 10.0,
        action_probe: float = 5.0,
        connect_timeout: float = 10.0,
        tcp_timeout: float = 8.0,
        ble_link_timeout: float = 4.0,
        stale_grace: float = 15.0,
        action_wait: float = 5.0,
        action_deadline: float = 12.0,
        connect_deadline: float = 12.0,
    ) -> None:
        from .api import ApiServer

        ensure_private_dir(config_dir)
        self.config_dir = config_dir
        self.name = name
        self.platform = local_platform()
        self.identity = load_or_create_identity(config_dir)
        self.device_id = device_id(self.identity.public)
        self.store = PeerStore(config_dir)
        self.keepalive = keepalive
        self.dead_after = dead_after
        self.stall_after = stall_after
        self.send_timeout = send_timeout
        self.pairing_timeout = pairing_timeout
        self.action_timeout = action_timeout
        self.action_probe = action_probe
        self.connect_timeout = connect_timeout
        self.tcp_timeout = tcp_timeout
        self.ble_link_timeout = ble_link_timeout
        self.stale_grace = stale_grace
        self.action_wait = action_wait
        self.action_deadline = action_deadline
        self.connect_deadline = connect_deadline
        self.devices: dict[str, Device] = {}
        self.sessions: dict[str, Session] = {}
        self.api = ApiServer(self, socket_path)
        self._use_mdns = mdns
        self._use_ble = ble
        self._lan = None
        self._ble = None
        self.lan_state = "off"
        self.lan_error: str | None = None
        self.ble_state = "off"
        self.ble_error: str | None = None
        self._stopping = False
        self._dirty: set[str] = set()
        self._flush_scheduled = False
        self._bg: set[asyncio.Task] = set()
        self._lock_fd: int | None = None
        self._ble_wake = asyncio.Event()
        self._clocks: tuple[float, float | None, float] | None = None
        for pin in self.store.peers.values():
            self.devices[pin.id] = Device(pin.id, pin.name, pin.platform)

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        self._lock_config()
        try:
            await self.api.start()
        except BaseException:
            self._unlock_config()
            raise
        log.info("orbitd %s: %s (%s), local API at %s", __version__, self.name, self.device_id, self.api.path)
        if self._use_mdns:
            self.lan_state = "starting"
            await self._start_lan_once()
            if self._lan is None:
                self._spawn(self._start_lan())
        for dev in self.devices.values():
            self._ensure_connector(dev)
        if self._use_ble:
            # In the background: starting can block on a macOS permission prompt, or fail until
            # Bluetooth is switched on.
            self.ble_state = "starting"
            self._spawn(self._run_ble())
            self._spawn(self._ble_expiry())
        self.check_clocks()
        self._spawn(self._clock_watch())

    def _lock_config(self) -> None:
        """One orbitd per identity: a second one would open duplicate sessions to every phone."""
        path = self.config_dir / LOCK_FILE
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise AlreadyRunning(f"another orbitd is already using {self.config_dir}") from None
            raise
        self._lock_fd = fd

    def _unlock_config(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _start_lan_once(self) -> None:
        from .discovery import LanDiscovery

        lan = LanDiscovery(self.on_lan, self.on_lan_seen, self.on_network_change, self._lan_wanted)
        error = await lan.start()
        if error is None:
            self._lan, self.lan_state, self.lan_error = lan, "browsing", None
        else:
            self.lan_state, self.lan_error = "unavailable", error

    async def _start_lan(self) -> None:
        while self._lan is None:
            await asyncio.sleep(LAN_RETRY)
            await self._start_lan_once()

    async def _run_ble(self) -> None:
        """Keep a BLE scan running: start it (retrying while Bluetooth is off or unavailable), and
        restart it when the adapter comes back after being powered off (scans do not survive that)."""
        from .discovery import BleDiscovery

        delay = BLE_RETRY_MIN
        while True:
            scanner = BleDiscovery(self.on_ble)
            error = await scanner.start()
            if error is not None:
                if (self.ble_state, self.ble_error) != ("unavailable", error):
                    log.warning("BLE discovery unavailable (will retry): %s", error)
                self.ble_state, self.ble_error = "unavailable", error
                await asyncio.sleep(delay)
                delay = min(delay * 2, BLE_RETRY_MAX)
                continue
            log.info("scanning for Orbit devices via BLE")
            self._ble, self.ble_state, self.ble_error = scanner, "scanning", None
            delay = BLE_RETRY_MIN
            while (powered := await scanner.powered()) is not False:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._ble_wake.wait(), BLE_WATCH_INTERVAL)
                if self._ble_wake.is_set():
                    self._ble_wake.clear()
                    if powered is None:  # the adapter state is unknown: a fresh scan is the safe bet
                        break
            log.info("Bluetooth adapter is off or reset; restarting BLE discovery when it is back")
            self._ble = None
            await scanner.stop()
            if powered is False:
                self.ble_state, self.ble_error = "unavailable", "Bluetooth is turned off"

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
        self._unlock_config()

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
        elif pin and pin.last_lan:
            address = pin.last_lan
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
            "problem": dev.problem,
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
        if self.store.get(dev.id) or dev.session or dev.lan or self._ble_visible(dev) or dev.problem:
            return
        if self.devices.pop(dev.id, None) is not None:
            self.changed("devices")

    def routes(self, dev: Device) -> list[Route]:
        return self._lan_routes(dev) + self._ble_routes(dev)

    def _lan_routes(self, dev: Device, automatic: bool = True) -> list[Route]:
        """LAN routes, best first: the address that worked last time, the advertised ones, then the
        manual one; an address that failed recently goes after the others. ``automatic`` leaves out
        addresses where a different device answered recently."""
        now = time.monotonic()
        pin = self.store.get(dev.id)
        advertised: list[tuple[str, int]] = [a for rec in dev.lan.values() for a in rec.addresses]
        candidates: list[tuple[str, int, bool]] = []
        last = None
        if pin and pin.last_lan:
            with contextlib.suppress(ValueError):
                last = parse_hostport(pin.last_lan)
        if last and (not advertised or last in advertised):
            candidates.append((*last, False))
        candidates += [(h, p, False) for h, p in sorted(advertised, key=lambda a: address_order(a[0]))]
        if last:
            candidates.append((*last, False))
        if pin and pin.address:
            with contextlib.suppress(ValueError):
                candidates.append((*parse_hostport(pin.address), True))
        routes: list[Route] = []
        seen: set[str] = set()
        for host, port, manual in candidates:
            addr = format_hostport(host, port)
            if addr in seen:
                continue
            seen.add(addr)
            other = dev.mismatch.get(addr)
            if automatic and other and now - other[1] < MISMATCH_FOR:
                continue
            routes.append(Route("lan", addr, lambda h=host, p=port: open_tcp(h, p), dev.id, manual))
        return sorted(routes, key=lambda r: now - dev.failed.get(r.address, -1e9) < FAILED_ADDRESS_FOR)

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
        old = dev.lan.get(instance)
        dev.lan[instance] = LanRecord(info["name"], info["platform"], info["addresses"])
        if not dev.name:
            dev.name = info["name"]
        if not dev.platform:
            dev.platform = info["platform"]
        if old is None or old.addresses != info["addresses"]:
            log.info("discovered %s (%s) via mDNS at %s", info["name"], dev.id, info["addresses"])
            for addr in {format_hostport(*a) for a in info["addresses"]}:
                dev.failed.pop(addr, None)  # re-announced: worth trying again
        self.changed("devices")
        dev.wake.set()
        s = dev.session
        addrs = {format_hostport(*a) for a in info["addresses"]}
        added = old is not None and addrs - {format_hostport(*a) for a in old.addresses}
        if s and s.transport == "lan" and (s.address not in addrs or added):
            # The phone moved: the session may be talking to an address that is gone (and that the
            # mDNS cache still lists).
            s.probe("the phone announced a new address", PROBE_TIMEOUT)
        if s and s.transport == "ble" and s.paired and dev.auto:
            self._spawn(self._upgrade_to_lan(dev))

    def on_lan_seen(self, instance: str) -> None:
        """An mDNS record of ``instance`` arrived, maybe identical to the cached one (a re-announcement)."""
        for dev in self.devices.values():
            if instance in dev.lan and dev.session is None:
                dev.wake.set()

    def on_network_change(self) -> None:
        """The computer's network interfaces or addresses changed (roaming, Wi-Fi off/on, VPN...)."""
        log.info("network interfaces changed; checking connections")
        for dev in self.devices.values():
            dev.failed.clear()
            dev.wake.set()
            if dev.session and dev.session.transport == "lan":
                dev.session.probe("the network changed", PROBE_TIMEOUT)

    def _lan_wanted(self) -> list[str]:
        """mDNS instances worth asking for now: those of paired devices that are not connected."""
        out = []
        for dev in self.devices.values():
            if self.store.get(dev.id) and dev.session is None and dev.auto:
                out += list(dev.lan)
        return out

    def on_ble(self, prefix: str, ble_device: Any, local_name: str | None = None) -> None:
        matches = [d for d in self.devices.values() if not d.provisional and d.id.startswith(prefix)]
        if not matches:
            dev = self.devices.get(prefix)
            if dev is None:
                dev = self.devices[prefix] = Device(prefix, provisional=True)
            dev.name = local_name or dev.name or f"Orbit device {prefix[:4]}"
            matches = [dev]
        now = time.monotonic()
        for dev in matches:
            fresh = not self._ble_visible(dev)
            moved = ble_device.address not in dev.ble
            last_seen = max((e[1] for e in dev.ble.values()), default=None)
            dev.ble[ble_device.address] = (ble_device, now)
            if fresh:
                log.info("discovered %s (%s) via BLE at %s", dev.name, dev.id, ble_device.address)
                self.changed("devices")
            elif moved:
                log.info("%s (%s) now advertises via BLE at %s", dev.name, dev.id, ble_device.address)
            if fresh or moved:
                dev.ble_moved.set()
            back = last_seen is not None and now - last_seen >= BLE_RETURN_GAP
            if fresh or moved or (back and dev.session is None):
                dev.wake.set()
            s = dev.session
            if s and s.transport == "lan" and s.quiet_for() >= LAN_QUIET_WITH_BLE:
                # The phone is in Bluetooth range: make sure the quiet Wi-Fi link still works, to fall back soon.
                s.probe("checking Wi-Fi while the phone is in Bluetooth range", LAN_CHECK)

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
        now = time.monotonic()
        routes = [r for r in self._lan_routes(dev) if now - dev.failed.get(r.address, -1e9) >= FAILED_ADDRESS_FOR]
        if dev.upgrading or not routes:
            return
        dev.upgrading = True
        try:
            with contextlib.suppress(ApiError):
                await self._race(self._staggered(dev, routes, explicit=False), [])
        finally:
            dev.upgrading = False

    # ---- clocks: sleep and clock steps -----------------------------------------------------------

    async def _clock_watch(self) -> None:
        while True:
            await asyncio.sleep(CLOCK_TICK)
            self.check_clocks()

    def check_clocks(self) -> None:
        """Notice that the computer slept (monotonic time stood still) or that the wall clock was stepped.

        Called every CLOCK_TICK and before acting on a request, so that an action sent right after
        a wake does not go out on a session that died during the sleep.
        """
        mono, sleep, wall = time.monotonic(), _sleep_clock(), time.time()
        if self._clocks is not None:
            mono0, sleep0, wall0 = self._clocks
            ran = mono - mono0
            if sleep is not None and sleep0 is not None:
                slept = (sleep - sleep0) - ran
                if slept > CLOCK_JUMP:
                    self._woke(slept)
                ran += max(0.0, slept)
            step = (wall - wall0) - ran
            if abs(step) > CLOCK_JUMP:
                self._clock_stepped(step)
        self._clocks = mono, sleep, wall

    def _woke(self, slept: float) -> None:
        log.info("the computer slept for about %.0f s; checking every connection", slept)
        for dev in self.devices.values():
            dev.wake.set()
            dev.failed.clear()
            if dev.session:
                if dev.activities and not dev.stale:
                    dev.stale = True
                    self.changed("activities")
                dev.session.probe("the computer slept", PROBE_TIMEOUT, hold_actions=True)
        if self._lan:
            self._lan.refresh()
        self._ble_wake.set()

    def _clock_stepped(self, step: float) -> None:
        """Converted timestamps follow the local clock (spec/LOCAL_API.md, DeviceActivity)."""
        delta = int(step * 1000)
        log.info("the system clock was stepped by %+.1f s; shifting activity times", step)
        for s in self.sessions.values():
            s.offset += delta
        for dev in self.devices.values():
            for a in dev.activities.values():
                shift_local(a, delta)
            if dev.activities:
                self.changed("activities")

    def session_verified(self, session: Session) -> None:
        """A suspect session answered its liveness check: its activities are current again."""
        dev = self._live(session)
        if dev is not None and dev.stale and session.productive:
            dev.stale = False
            self.changed("activities")

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

    async def _wait_wake(self, dev: Device, timeout: float | None, min_wait: float = 0.0) -> None:
        if min_wait > 0:
            await asyncio.sleep(min_wait)
            timeout = None if timeout is None else max(0.0, timeout - min_wait)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(dev.wake.wait(), timeout)
        dev.wake.clear()

    def _retry_delay(self, failures: int, failing_for: float) -> float:
        cap = (
            RECONNECT_GRACE_MAX
            if failing_for < self.stale_grace
            else RECONNECT_SOON_MAX
            if failing_for < RECONNECT_SOON_FOR
            else RECONNECT_MAX_DELAY
        )
        return min(2.0 ** (failures - 1), cap)

    async def _connector(self, dev: Device) -> None:
        """Keep a paired device connected.

        After a session that carried data (the phone's snapshot) or lasted STABLE_SESSION, the device
        is reconnected at once. Failed attempts, and sessions that end before any data, back off
        exponentially, with a low cap while the device is expected back (see ``_retry_delay``);
        discovery (an mDNS announcement, a returning BLE advertisement, a network change, waking
        from sleep) cuts a wait short.
        """
        failures = 0
        failing_since: float | None = None
        while not self._stopping and self.store.get(dev.id) and self.devices.get(dev.id) is dev:
            session = dev.session
            if session is None:
                if not dev.auto or not self.routes(dev):
                    await self._wait_wake(dev, BLE_STALE_AFTER if dev.ble else None)
                    continue
                dev.connecting = True
                try:
                    session = await self.connect_device(dev, explicit=False)
                except ApiError as e:
                    error: ApiError | None = e
                else:
                    error = None
                finally:
                    dev.connecting = False
                if error is not None:
                    failures += 1
                    now = time.monotonic()
                    failing_since = failing_since or dev.lost_at or now
                    delay = self._retry_delay(failures, now - failing_since)
                    log.info("connecting to %s (%s) failed: %s; retrying in %g s", dev.name, dev.id, error, delay)
                    self._manual_silent(dev, now - failing_since)
                    if self._lan and any(r.kind == "lan" for r in self._lan_routes(dev)):
                        self._lan.refresh(list(dev.lan))
                    await self._wait_wake(dev, delay, min_wait=min(RECONNECT_MIN_GAP, delay))
                    continue
            started = time.monotonic()
            await session.closed.wait()
            dev.wake.clear()  # set by session_closed itself; only later discovery changes may cut the wait short
            if session.productive or time.monotonic() - started >= STABLE_SESSION:
                failures, failing_since = 0, None
                continue
            failures += 1
            now = time.monotonic()
            failing_since = failing_since or now
            await self._wait_wake(dev, self._retry_delay(failures, now - failing_since))

    def _attempt(self, dev: Device | None, route: Route, *, explicit: bool, moved: asyncio.Event | None = None):
        async def attempt() -> Session:
            try:
                session = await self._connect_route(route, explicit=explicit, moved=moved)
            except ApiError:
                if dev is not None and route.kind == "lan":
                    dev.failed[route.address] = time.monotonic()
                raise
            if dev is not None:
                dev.failed.pop(route.address, None)
            return session

        return attempt

    async def connect_device(self, dev: Device, *, explicit: bool, errors: list[ApiError] | None = None) -> Session:
        """Try the LAN addresses (staggered, happy-eyeballs style) and BLE in parallel; first session wins.

        BLE starts BLE_AFTER_LAN after the first LAN attempt, or at once when every LAN address
        failed recently. Its addresses are tried most recently advertised first, and an attempt to
        a BLE address is abandoned as soon as the phone advertises a new one: a rotated-away address
        must not hold up a reachable one.
        """
        lan = self._lan_routes(dev, automatic=not explicit)
        if not lan and not self._ble_visible(dev):
            raise ApiError("not_connected", "no known address for this device")
        errors = [] if errors is None else errors
        now = time.monotonic()
        plan = self._staggered(dev, lan, explicit=explicit)
        if self._ble_visible(dev):
            lan_dead = all(now - dev.failed.get(r.address, -1e9) < FAILED_ADDRESS_FOR for r in lan)
            plan.append((0.0 if lan_dead else BLE_AFTER_LAN, lambda: self._connect_ble(dev, explicit, errors)))
        try:
            return await self._race(plan, errors)
        except ApiError as e:
            raise self._explained(dev, e, errors) from None

    async def _connect_ble(self, dev: Device, explicit: bool, errors: list[ApiError]) -> Session:
        tried: set[str] = set()
        while route := next((r for r in self._ble_routes(dev) if r.address not in tried), None):
            tried.add(route.address)
            dev.ble_moved.clear()
            try:
                return await self._connect_route(route, explicit=explicit, moved=dev.ble_moved)
            except ApiError as e:
                errors.append(e)
        raise ApiError("not_connected", "no current BLE address") if not tried else errors.pop()

    def _explained(self, dev: Device, error: ApiError, errors: list[ApiError]) -> ApiError:
        """Add a hint when the phone is visible on the network but no TCP connection gets through."""
        lan_errors = [e for e in errors if e.message.startswith(("connecting to", "cannot connect to"))]
        if dev.lan and lan_errors and len(lan_errors) == len(errors):
            return ApiError(
                error.code,
                f"{error.message} - the phone is visible on the network but does not accept connections: "
                "the network may isolate devices (guest or corporate Wi-Fi); Bluetooth may still work",
            )
        return error

    async def connect_routes(self, routes: list[Route], *, explicit: bool) -> Session:
        if not routes:
            raise ApiError("not_connected", "no known address for this device")
        return await self._race(self._staggered(None, routes, explicit=explicit), [])

    def _staggered(self, dev: Device | None, routes: list[Route], *, explicit: bool) -> list[Plan]:
        return [(i * LAN_STAGGER, self._attempt(dev, r, explicit=explicit)) for i, r in enumerate(routes)]

    async def _race(self, plan: list[Plan], errors: list[ApiError]) -> Session:
        """Run ``(start offset, attempt)`` pairs; an attempt also starts early when nothing else is running.

        Returns the first session; the other attempts are cancelled. Raises the combined errors.
        """
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        pending = list(plan)
        running: set[asyncio.Future] = set()
        try:
            while pending or running:
                while pending and (pending[0][0] <= loop.time() - t0 or not running):
                    running.add(asyncio.ensure_future(pending.pop(0)[1]()))
                timeout = max(0.0, pending[0][0] - (loop.time() - t0)) if pending else None
                done, running = await asyncio.wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
                won = [t for t in done if t.exception() is None]
                if won:
                    for t in done - set(won):
                        t.exception()
                    return won[-1].result()
                for t in done:
                    e = t.exception()
                    if not isinstance(e, ApiError):
                        raise e
                    if e not in errors:
                        errors.append(e)
            raise _combined(errors) if errors else ApiError("not_connected", "no known address for this device")
        finally:
            for t in running:
                t.cancel()
            if running:
                await asyncio.wait(running)
                for t in running:
                    if not t.cancelled():
                        t.exception()

    async def _open(self, route: Route, moved: asyncio.Event | None) -> FrameStream:
        opening = asyncio.ensure_future(route.open())
        opening.add_done_callback(lambda t: t.cancelled() or t.exception())  # also when we are cancelled
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
        stream = FrameStream(WatchedReader(stream.reader), WatchedWriter(stream.writer))
        session = None
        try:
            channel = await asyncio.wait_for(
                initiate(stream, self.identity, self.name, self.platform), self.connect_timeout
            )
            peer = channel.peer
            if route.expected and peer.id != route.expected:
                self._identity_mismatch(route, peer.id, peer.name)
                raise ApiError("failed", f"{route.address} is device {peer.id}, expected {route.expected}")
            if route.prefix and not peer.id.startswith(route.prefix):
                raise ApiError("failed", f"{route.address} is device {peer.id}, not the advertised {route.prefix}")
            session = Session(self, channel, route.kind, route.address, route.manual)
            session.local_paired = self.store.is_pinned(peer.id, peer.static)
            known = self.devices.get(peer.id)
            art = list(known.art)[-ART_MAX_ENTRIES:] if known and session.local_paired else []
            await session.exchange_ready(self.connect_timeout, art)
            if known:
                known.art_kept = set(art)
            # Shielded: an attempt cancelled because another route won must not leave adoption half done.
            return await asyncio.shield(self._adopt(session, explicit))
        except ApiError:
            raise
        except TimeoutError:
            what = "the Orbit handshake" if session is None else "the phone's ready message"
            raise ApiError(
                "timeout",
                f"{route.address} accepted the connection but {what} did not arrive within {self.connect_timeout:g} s",
            ) from None
        except asyncio.IncompleteReadError:
            raise ApiError("failed", f"{route.address} closed the connection during the handshake") from None
        except ProtocolError as e:
            if e.code == "bad_frame" and session is None:
                raise ApiError("failed", f"{route.address} is not an Orbit phone (it does not speak Orbit)") from None
            raise ApiError("failed", f"handshake with {route.address} failed: {e}") from None
        except Exception as e:  # OSError, transport-specific errors
            raise ApiError("failed", f"handshake with {route.address} failed: {e}") from None
        finally:
            if session is None or not session.started:
                stream.close()

    def _identity_mismatch(self, route: Route, other_id: str, other_name: str) -> None:
        dev = self.devices.get(route.expected or "")
        if dev is None:
            return
        dev.mismatch[route.address] = (other_id, time.monotonic())
        if self._drop_if_superseded(dev, other_id, other_name):
            return
        if dev.session is not None or self.store.get(other_id):
            return  # reachable elsewhere, or a known phone sharing the address's host name
        problem = (
            f"{route.address} now answers as a different device ({other_name or '?'}, {other_id[:8]}): "
            f"if {dev.name or 'the phone'} was reset or Orbit reinstalled, pair it again and forget this entry"
        )
        if dev.problem != problem:
            log.warning("%s (%s): %s", dev.name, dev.id, problem)
            dev.problem = problem
            self.changed("devices")

    def _manual_silent(self, dev: Device, failing_for: float) -> None:
        pin = self.store.get(dev.id)
        if dev.problem or not pin or not pin.address or dev.lan or dev.ble or failing_for < MANUAL_SILENT_FOR:
            return
        dev.problem = f"nothing answers at {pin.address}: if the phone's address changed, connect to the new one"
        log.warning("%s (%s): %s", dev.name, dev.id, dev.problem)
        self.changed("devices")

    def _drop_if_superseded(self, old: Device, new_id: str, new_name: str) -> bool:
        """Forget ``old`` if it is the same phone as the paired ``new_id`` under a new identity (reset,
        reinstalled): one of its addresses answered as ``new_id``, and the name is the same."""
        if (
            old.id == new_id
            or old.name != new_name
            or not self.store.get(new_id)
            or not any(i == new_id for i, _ in old.mismatch.values())
        ):
            return False
        log.info("forgetting %s (%s): it was paired again as %s", old.name, old.id, new_id)
        self.store.remove(old.id)
        if old.connector:
            old.connector.cancel()
        self._end_grace(old)
        self.devices.pop(old.id, None)
        self.changed("devices", "activities")
        return True

    async def _adopt(self, session: Session, explicit: bool) -> Session:
        peer = session.peer
        dev = self._device(peer.id)
        self._absorb_provisional(dev)
        if not explicit and (not dev.auto or not self.store.get(peer.id)):
            # devices.disconnect or devices.forget arrived while this attempt was under way.
            await session.close("disconnected by the user", bye=True)
            raise ApiError("not_connected", "disconnected by the user")
        existing = dev.session
        if existing is not None and not existing.closed.is_set():
            # Only a paired BLE session gives way to LAN; an unpaired one may be mid-pairing.
            if not (session.transport == "lan" and existing.transport == "ble" and existing.paired):
                await session.close("already connected", bye=True)
                return existing
            dev.session = None
            self._spawn(existing.retire("switching to LAN", self.action_timeout))
        dev.name, dev.platform = peer.name, peer.platform
        dev.session = session
        dev.problem = None
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
            last_lan = session.address if session.transport == "lan" and not session.manual else pin.last_lan
            if (pin.name, pin.platform, pin.address, pin.last_lan) != (peer.name, peer.platform, address, last_lan):
                pin.name, pin.platform, pin.address, pin.last_lan = peer.name, peer.platform, address, last_lan
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
                dev.problem = f"{peer.name or 'The phone'} no longer trusts this computer; pair again"
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
        last_lan = session.address if session.transport == "lan" and not session.manual else None
        self.store.pin(PinnedPeer(peer.id, peer.name, peer.platform, peer.static, address, last_lan))
        log.info("paired with %s (%s)", peer.name, peer.id)
        for old in list(self.devices.values()):
            self._drop_if_superseded(old, peer.id, peer.name)
        dev = self._device(peer.id)
        dev.auto = True
        dev.problem = None
        self._ensure_connector(dev)
        self.changed("devices", "pairing")

    def unpaired_by_phone(self, session: Session) -> None:
        log.info("%s (%s) unpaired this computer", session.peer.name, session.peer.id)
        self.store.remove(session.peer.id)
        dev = self.devices.get(session.peer.id)
        if dev:
            dev.art.clear()
            dev.problem = f"{session.peer.name or 'The phone'} unpaired this computer; pair again to use it"
        self.changed("devices")

    def _live(self, session: Session) -> Device | None:
        dev = self.devices.get(session.peer.id)
        return dev if dev is not None and dev.session is session else None

    def set_activities(self, session: Session, activities: list[dict[str, Any]], now: int) -> None:
        if dev := self._live(session):
            dev.activities = {a["id"]: to_local(a, dev.id, now, session.offset) for a in activities}
            dev.stale = session.suspect
            dev.art_kept.clear()
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
        referenced = {a.get(k) for a in dev.activities.values() for k in ("artKey", "iconKey")} | dev.art_kept
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
        errors: list[ApiError] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_deadline
        if "host" in params or "port" in params:
            host, port = params.get("host"), params.get("port")
            if (
                not isinstance(host, str)
                or not host
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 0 < port < 65536
            ):
                raise ApiError("bad_request", "'host' must be a string and 'port' an integer 1-65535")
            route = Route("lan", format_hostport(host, port), lambda: open_tcp(host, port), None, True)
            attempt = self.connect_routes([route], explicit=True)
        else:
            dev = self._known(params.get("id"))
            dev.auto = True
            self.check_clocks()
            if dev.session is not None:
                # It may be a session the network silently dropped: make sure it still answers.
                if await dev.session.verify(min(PROBE_TIMEOUT, self.connect_deadline / 2)):
                    return {"id": dev.id}
                await asyncio.sleep(0)  # let session_closed run
                if dev.session is not None:
                    return {"id": dev.id}
            attempt = self.connect_device(dev, explicit=True, errors=errors)
        try:
            async with asyncio.timeout_at(deadline):
                session = await attempt
        except TimeoutError:
            tried = "; ".join(e.message for e in errors)
            raise ApiError(
                "timeout", f"no connection within {self.connect_deadline:g} s" + (f" ({tried})" if tried else "")
            ) from None
        return {"id": session.peer.id}  # differs from the request for a provisional device

    async def api_disconnect(self, params: dict[str, Any]) -> bool:
        dev = self._known(params.get("id"))
        if dev.session is None and dev.grace is None:
            if dev.connecting and dev.auto:  # an attempt is under way: it must not connect
                dev.auto = False
                return True
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
        dev.problem = None
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
        self.check_clocks()
        try:
            async with asyncio.timeout(self.action_deadline):
                return await self._action(dev, activity, action, value, "value" in params)
        except TimeoutError:
            raise ApiError("timeout", NO_RESPONSE) from None

    async def _action(self, dev: Device, activity: str, action: str, value: Any, has_value: bool) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.action_wait
        while True:
            session = dev.session
            if session is None:
                if dev.grace is None:
                    raise ApiError("not_connected", dev.problem or "phone offline")
                # Reconnecting: hold the action until the session is back, up to action_wait.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(dev.transition.wait(), deadline - loop.time())
                if dev.session is None and loop.time() >= deadline:
                    raise ApiError("not_connected", "reconnecting")
                continue
            if not session.paired:
                raise ApiError("not_paired", f"{dev.id} is not paired")
            if activity not in dev.activities:
                raise ApiError("not_found", f"unknown activity {activity}")
            try:
                return await session.action(activity, action, value, has_value, self.action_timeout)
            except ActionNotSent as e:
                log.info("action %s on %s not sent (%s); waiting for the next session", action, dev.id, e)
                # A fresh wait for the next session (still within action_deadline overall).
                deadline = loop.time() + self.action_wait
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
        return {
            "version": __version__,
            "name": self.name,
            "deviceId": self.device_id,
            "lan": self.lan_state,
            "lanError": self.lan_error,
            "ble": self.ble_state,
            "bleError": self.ble_error,
        }
