"""BLE GATT central transport (spec/PROTOCOL.md 1.2), presented as a FrameStream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from typing import Any

from bleak.exc import BleakCharacteristicNotFoundError

from ..proto import FrameStream
from .session import LinkDown

log = logging.getLogger(__name__)

SERVICE_UUID = "616dfa5c-069e-4839-8335-87edbeb2c1a5"
RX_UUID = "616dfa5d-069e-4839-8335-87edbeb2c1a5"  # I -> R, write with response
TX_UUID = "616dfa5e-069e-4839-8335-87edbeb2c1a5"  # R -> I, notify

# bleak raises these before handing a write to the OS, so nothing reached the phone.
_NOT_SENT_MESSAGES = ("Not connected", "Service Discovery has not been performed yet")

# Peripheral address -> called (on the event loop) when the phone invalidates the Orbit service.
_invalidation_listeners: dict[str, Callable[[], None]] = {}
_hook_loop: asyncio.AbstractEventLoop | None = None


def chunk_size(mtu: int) -> int:
    return max(20, min(512, mtu - 3))


class BleLink:
    """StreamWriter-like adapter over one GATT connection.

    ``drain`` writes buffered bytes to RX in ``ATT_MTU - 3`` chunks, in order. Any sign that the
    link is dead (disconnect, invalidated services, a failed write) ends the read side at once,
    so the session closes immediately instead of on its next write or keepalive timeout.
    """

    def __init__(self, client: Any, reader: asyncio.StreamReader, chunk: int) -> None:
        self._client = client
        self._reader = reader
        self._chunk = chunk
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self.address = str(client.address)
        self.lost_cause: str | None = None
        self._closed = False

    def lost(self, cause: str) -> None:
        if self.lost_cause is not None:
            return
        self.lost_cause = cause
        log.debug("BLE link to %s lost: %s", self.address, cause)
        self._reader.set_exception(LinkDown(f"BLE link lost: {cause}", sent=True))
        self.close()

    def write(self, data: bytes) -> None:
        if self.lost_cause is not None:
            raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=False)
        self._buf += data

    async def drain(self) -> None:
        async with self._lock:
            first = True
            while self._buf:
                if self.lost_cause is not None:
                    raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=not first)
                chunk = bytes(self._buf[: self._chunk])
                del self._buf[: self._chunk]
                try:
                    await self._client.write_gatt_char(RX_UUID, chunk, response=True)
                except Exception as e:
                    pre_send = first and (
                        isinstance(e, BleakCharacteristicNotFoundError) or str(e) in _NOT_SENT_MESSAGES
                    )
                    self.lost(f"write failed: {e}")
                    raise LinkDown(f"BLE write failed: {e}", sent=not pre_send) from e
                first = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if _invalidation_listeners.get(self.address) == self._on_invalidated:
            del _invalidation_listeners[self.address]
        self._buf.clear()
        if self.lost_cause is None:
            self.lost_cause = "closed locally"
            self._reader.feed_eof()
        task = asyncio.ensure_future(self._client.disconnect())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _on_invalidated(self) -> None:
        self.lost("phone invalidated the Orbit GATT service")


def services_invalidated(address: str, service_uuids: list[str]) -> None:
    """Entry point for platform hooks: the peripheral at ``address`` dropped some of its services."""
    if SERVICE_UUID in (u.lower() for u in service_uuids) and (cb := _invalidation_listeners.get(address)):
        cb()


def _install_corebluetooth_hook() -> None:
    """bleak ignores ``peripheral:didModifyServices:``; route it to :func:`services_invalidated`."""
    import objc
    from bleak.backends.corebluetooth.PeripheralDelegate import ObjcPeripheralDelegate

    global _hook_loop
    _hook_loop = asyncio.get_running_loop()
    if getattr(ObjcPeripheralDelegate, "_orbit_hooked", False):
        return

    def did_modify_services(self: Any, peripheral: Any, invalidated: Any) -> None:  # CoreBluetooth queue thread
        try:
            address = str(peripheral.identifier().UUIDString())
            uuids = [str(s.UUID().UUIDString()) for s in invalidated]
            _hook_loop.call_soon_threadsafe(services_invalidated, address, uuids)
        except Exception:  # must not propagate into CoreBluetooth
            log.debug("ignoring peripheral:didModifyServices:", exc_info=True)

    objc.classAddMethods(
        ObjcPeripheralDelegate,
        [objc.selector(did_modify_services, selector=b"peripheral:didModifyServices:", signature=b"v@:@@")],
    )
    ObjcPeripheralDelegate._orbit_hooked = True


def _abort_pending_connect(client: Any) -> None:
    """CoreBluetooth keeps a cancelled ``connect()`` pending forever unless it is cancelled explicitly."""
    backend = getattr(client, "_backend", None)
    peripheral = getattr(backend, "_peripheral", None)
    manager = getattr(backend, "_central_manager_delegate", None)
    if peripheral is not None and manager is not None:
        with contextlib.suppress(Exception):
            manager.central_manager.cancelPeripheralConnection_(peripheral)


async def open_ble(device: Any, link_timeout: float) -> FrameStream:
    """Connect, subscribe to TX and return the link as a FrameStream.

    ``link_timeout`` bounds only establishing the radio link (an address the phone has rotated away
    from never answers); service discovery afterwards is bounded by the caller.
    """
    from bleak import BleakClient

    if sys.platform == "darwin":
        try:
            _install_corebluetooth_hook()
        except Exception as e:  # never block connecting on the hook
            log.warning("cannot watch for invalidated GATT services: %s", e)

    reader = asyncio.StreamReader()
    link: BleLink | None = None

    def disconnected(_client: Any) -> None:
        if link is not None:
            link.lost("peripheral disconnected")
        else:
            reader.feed_eof()

    client = BleakClient(device, disconnected_callback=disconnected, timeout=link_timeout)
    try:
        await client.connect()
        backend = getattr(client, "_backend", None)
        if hasattr(backend, "_acquire_mtu"):  # BlueZ reports 23 until the MTU is acquired
            with contextlib.suppress(Exception):
                await backend._acquire_mtu()
        link = BleLink(client, reader, chunk_size(client.mtu_size))
        _invalidation_listeners[link.address] = link._on_invalidated
        # Subscribe before the first write: the session starts when notifications are enabled.
        await client.start_notify(TX_UUID, lambda _ch, data: reader.feed_data(bytes(data)))
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            _abort_pending_connect(client)
        if link is not None:
            link.close()
        else:
            with contextlib.suppress(Exception):
                await client.disconnect()
        raise
    log.info("BLE link to %s, MTU %d", client.address, client.mtu_size)
    return FrameStream(reader, link)
