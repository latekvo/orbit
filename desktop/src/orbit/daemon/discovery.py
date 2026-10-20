"""Discovery of Orbit responders: mDNS/DNS-SD (spec 1.1) and BLE advertisements (spec 1.2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

import ifaddr
from zeroconf import (
    DNSOutgoing,
    DNSQuestion,
    IPVersion,
    RecordUpdate,
    RecordUpdateListener,
    ServiceStateChange,
    Zeroconf,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
from zeroconf.const import _CLASS_IN, _FLAGS_QR_QUERY, _TYPE_A, _TYPE_AAAA, _TYPE_PTR, _TYPE_SRV, _TYPE_TXT

from ..proto import VERSION
from .ble import SERVICE_UUID
from .core import address_order

log = logging.getLogger(__name__)

SERVICE_TYPE = "_orbit._tcp.local."
# Local interfaces are compared this often; a change rebinds the mDNS sockets.
INTERFACE_POLL = 3.0
# While something is wanted (a paired phone is not connected) the service is queried this often:
# zeroconf's own browser backs off to an hour, so one lost announcement would otherwise be final.
REQUERY_INTERVAL = 15.0
# IPv6 as well (IPv6-only networks; link-local addresses get their scope): on Linux. On macOS a
# dual-stack zeroconf misses IPv4 announcements, and AAAA records still arrive over IPv4.
MDNS_IP_VERSION = IPVersion.V4Only if sys.platform == "darwin" else IPVersion.All


def supported_version(txt: str | None) -> bool:
    """TXT ``v`` lists the protocol versions a responder speaks, comma-separated (spec 1.1)."""
    return str(VERSION) in {v.strip() for v in (txt or "").split(",")}


def _interfaces() -> frozenset[tuple[str, str]]:
    out = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            host = ip.ip if isinstance(ip.ip, str) else ip.ip[0]
            if not host.startswith("127.") and host != "::1":
                out.add((adapter.name, host))
    return frozenset(out)


class _Seen(RecordUpdateListener):
    def __init__(self, owner: LanDiscovery) -> None:
        self._owner = owner

    def async_update_records(self, zc: Zeroconf, now: float, records: list[RecordUpdate]) -> None:
        for update in records:
            self._owner._record(update.new)

    def async_update_records_complete(self) -> None:
        pass


class LanDiscovery:
    """Browse ``_orbit._tcp``; calls ``on_change(instance, info | None)``.

    ``info`` = ``{"id", "name", "platform", "addresses": [(host, port)]}``, IPv4 first. ``on_seen(instance)``
    is called for every record of a known instance that arrives, also an unchanged one (a phone that
    restarted re-announces the same records, which the browser does not report). ``on_network_change()``
    when the computer's interfaces change. ``wanted()`` returns the instances worth querying for now.
    """

    def __init__(
        self,
        on_change: Callable[[str, dict[str, Any] | None], None],
        on_seen: Callable[[str], None] | None = None,
        on_network_change: Callable[[], None] | None = None,
        wanted: Callable[[], list[str]] | None = None,
    ) -> None:
        self._on_change = on_change
        self._on_seen = on_seen or (lambda _instance: None)
        self._on_network_change = on_network_change or (lambda: None)
        self._wanted = wanted or (lambda: [])
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task] = set()
        # Lower-cased instance name -> (instance name, SRV target host) of resolved instances.
        self._known: dict[str, tuple[str, str]] = {}
        self._seen_at: dict[str, float] = {}
        self._interfaces: frozenset[tuple[str, str]] = frozenset()
        self._last_query = 0.0
        self._listener = _Seen(self)

    async def start(self) -> str | None:
        """Start browsing; returns why it could not (e.g. UDP 5353 taken)."""
        self._interfaces = _interfaces()
        try:
            try:
                self._azc = AsyncZeroconf(ip_version=MDNS_IP_VERSION)
            except OSError as e:
                if MDNS_IP_VERSION is IPVersion.V4Only:
                    raise
                log.info("mDNS over IPv6 unavailable (%s); using IPv4 only", e)
                self._azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        except Exception as e:
            log.warning("mDNS discovery unavailable (will retry): %s", e)
            self._azc = None
            return str(e) or type(e).__name__
        zc = self._azc.zeroconf
        zc.async_add_listener(self._listener, None)
        self._browser = AsyncServiceBrowser(zc, SERVICE_TYPE, handlers=[self._on_state])
        self._spawn(self._watch())
        log.info("browsing %s via mDNS", SERVICE_TYPE)
        return None

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._azc:
            self._azc.zeroconf.async_remove_listener(self._listener)
        if self._browser:
            await self._browser.async_cancel()
        if self._azc:
            await self._azc.async_close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def refresh(self, instances: list[str] | None = None) -> None:
        """Ask the network for the service (and ``instances``) now, without known answers."""
        if self._azc is None or time.monotonic() - self._last_query < 1.0:
            return
        self._last_query = time.monotonic()
        out = DNSOutgoing(_FLAGS_QR_QUERY)
        out.add_question(DNSQuestion(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN))
        for instance in instances or []:
            out.add_question(DNSQuestion(instance, _TYPE_SRV, _CLASS_IN))
            out.add_question(DNSQuestion(instance, _TYPE_TXT, _CLASS_IN))
            if known := self._known.get(instance.lower()):
                out.add_question(DNSQuestion(known[1], _TYPE_A, _CLASS_IN))
                out.add_question(DNSQuestion(known[1], _TYPE_AAAA, _CLASS_IN))
        with contextlib.suppress(Exception):
            self._azc.zeroconf.async_send(out)

    async def _watch(self) -> None:
        assert self._azc is not None
        while True:
            await asyncio.sleep(INTERFACE_POLL)
            interfaces = await asyncio.get_running_loop().run_in_executor(None, _interfaces)
            if interfaces != self._interfaces:
                log.info("network interfaces changed: %s", sorted(h for _, h in interfaces) or "none")
                self._interfaces = interfaces
                try:
                    await self._azc.zeroconf.async_update_interfaces()
                except Exception as e:
                    log.warning("could not rebind mDNS to the new interfaces: %s", e)
                self._last_query = 0.0
                self.refresh(self._wanted())
                self._on_network_change()
            self._expire()
            wanted = self._wanted()
            if (wanted or not self._known) and time.monotonic() - self._last_query >= REQUERY_INTERVAL:
                self.refresh(wanted)

    def _expire(self) -> None:
        """Forget instances whose SRV record expired: a phone killed without a goodbye is gone."""
        assert self._azc is not None
        cache = self._azc.zeroconf.cache
        now = time.time() * 1000
        for key, (instance, _host) in list(self._known.items()):
            records = cache.async_all_by_details(instance, _TYPE_SRV, _CLASS_IN)
            if not any(not r.is_expired(now) for r in records):
                log.info("mDNS records of %s expired", instance)
                del self._known[key]
                self._on_change(instance, None)

    def _record(self, record: Any) -> None:
        name = record.name.lower()
        hits = [inst for key, (inst, host) in self._known.items() if name == key or name == host.lower()]
        if record.type == _TYPE_PTR and getattr(record, "alias", "").lower() in self._known:
            hits.append(self._known[record.alias.lower()][0])
        now = time.monotonic()
        for instance in hits:
            if now - self._seen_at.get(instance, 0.0) >= 1.0:
                self._seen_at[instance] = now
                self._on_seen(instance)

    def _on_state(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            self._known.pop(name.lower(), None)
            self._on_change(name, None)
            return
        self._spawn(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000) or not info.port:
            log.debug("could not resolve %s", name)
            return
        props = info.decoded_properties
        dev_id = props.get("id") or ""
        if len(dev_id) != 32:
            log.debug("ignoring %s with TXT %s", name, props)
            return
        if not supported_version(props.get("v")):
            log.info(
                "ignoring %s: it speaks Orbit protocol version %s, this computer %s; update Orbit on the older side",
                name,
                props.get("v"),
                VERSION,
            )
            return
        addrs = sorted(info.parsed_scoped_addresses(IPVersion.All), key=address_order)
        if not addrs:
            return
        if info.server:
            self._known[name.lower()] = (name, info.server)
        self._on_change(
            name,
            {
                "id": dev_id.lower(),
                "name": name.removesuffix("." + service_type),
                "platform": props.get("pf") or "",
                "addresses": [(a, info.port) for a in addrs],
            },
        )


class BleDiscovery:
    """Scan for the Orbit service UUID; calls ``on_seen(id_prefix_hex16, BLEDevice, local_name)`` per advertisement."""

    def __init__(self, on_seen: Callable[[str, Any, str | None], None]) -> None:
        self._on_seen = on_seen
        self._scanner = None

    async def start(self) -> str | None:
        """Start scanning; returns why it could not (no adapter, Bluetooth off, no permission...)."""
        try:
            from bleak import BleakScanner

            self._scanner = BleakScanner(detection_callback=self._detected, service_uuids=[SERVICE_UUID])
            await self._scanner.start()
        except Exception as e:
            self._scanner = None
            # bleak's BleakBluetoothNotAvailableError carries (message, reason): show the message.
            message = e.args[0] if e.args and isinstance(e.args[0], str) else str(e)
            return message or type(e).__name__
        return None

    async def stop(self) -> None:
        if self._scanner:
            with contextlib.suppress(Exception):
                await self._scanner.stop()

    async def powered(self) -> bool | None:
        """Whether the adapter is powered on (a scan does not survive it being switched off); None if unknown."""
        backend = getattr(self._scanner, "_backend", None)
        manager = getattr(getattr(backend, "_manager", None), "central_manager", None)
        if manager is not None:  # CoreBluetooth
            try:
                return int(manager.state()) == 5  # CBManagerStatePoweredOn
            except Exception:
                return None
        if type(backend).__module__.startswith("bleak.backends.bluezdbus"):
            try:
                from bleak.backends.bluezdbus.manager import get_global_bluez_manager

                bluez = await get_global_bluez_manager()
                adapter = getattr(backend, "_adapter", None)
                path = f"/org/bluez/{adapter}" if adapter else bluez.get_default_adapter()
                return bool(bluez._properties[path]["org.bluez.Adapter1"]["Powered"])
            except Exception:
                return False  # no powered adapter left (get_default_adapter raises), or it vanished
        return None

    def _detected(self, device: Any, adv: Any) -> None:
        data = adv.service_data.get(SERVICE_UUID)
        if data and len(data) >= 8:
            self._on_seen(bytes(data[:8]).hex(), device, adv.local_name)
