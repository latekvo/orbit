"""Discovery of Orbit responders: mDNS/DNS-SD (spec 1.1) and BLE advertisements (spec 1.2)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import Callable
from typing import Any

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from ..proto import VERSION
from .ble import SERVICE_UUID

log = logging.getLogger(__name__)

SERVICE_TYPE = "_orbit._tcp.local."


def _address_order(addr: str) -> tuple[int, str]:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    return (0 if ip.version == 4 and not ip.is_loopback else 1 if ip.version == 4 else 2, addr)


class LanDiscovery:
    """Browse ``_orbit._tcp``; calls ``on_change(instance, info | None)``.

    ``info`` = ``{"id", "name", "platform", "addresses": [(host, port)]}``, IPv4 first.
    """

    def __init__(self, on_change: Callable[[str, dict[str, Any] | None], None]) -> None:
        self._on_change = on_change
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._azc = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(self._azc.zeroconf, SERVICE_TYPE, handlers=[self._on_state])
        log.info("browsing %s via mDNS", SERVICE_TYPE)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._browser:
            await self._browser.async_cancel()
        if self._azc:
            await self._azc.async_close()

    def _on_state(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            self._on_change(name, None)
            return
        task = asyncio.ensure_future(self._resolve(zeroconf, service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000) or not info.port:
            log.debug("could not resolve %s", name)
            return
        props = info.decoded_properties
        dev_id = props.get("id") or ""
        if props.get("v") != str(VERSION) or len(dev_id) != 32:
            log.debug("ignoring %s with TXT %s", name, props)
            return
        addrs = sorted(info.parsed_scoped_addresses(IPVersion.All), key=_address_order)
        if not addrs:
            return
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
            log.warning("BLE discovery unavailable (will retry): %s", e)
            self._scanner = None
            return str(e) or type(e).__name__
        log.info("scanning for Orbit devices via BLE")
        return None

    async def stop(self) -> None:
        if self._scanner:
            with contextlib.suppress(Exception):
                await self._scanner.stop()

    def _detected(self, device: Any, adv: Any) -> None:
        data = adv.service_data.get(SERVICE_UUID)
        if data and len(data) >= 8:
            self._on_seen(bytes(data[:8]).hex(), device, adv.local_name)
