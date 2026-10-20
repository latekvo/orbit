"""BLE central transport (spec/PROTOCOL.md 1.2): an L2CAP channel, else GATT, presented as a FrameStream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from bleak.exc import BleakCharacteristicNotFoundError

from ..proto import FrameStream
from .session import LinkDown

log = logging.getLogger(__name__)

SERVICE_UUID = "616dfa5c-069e-4839-8335-87edbeb2c1a5"
RX_UUID = "616dfa5d-069e-4839-8335-87edbeb2c1a5"  # I -> R, write with response
TX_UUID = "616dfa5e-069e-4839-8335-87edbeb2c1a5"  # R -> I, notify
PSM_UUID = "616dfa5f-069e-4839-8335-87edbeb2c1a5"  # read: the phone's L2CAP PSM, u16 little-endian

# bleak raises these before handing a write to the OS, so nothing reached the phone.
_NOT_SENT_MESSAGES = ("Not connected", "Service Discovery has not been performed yet")

# Peripheral address -> called (on the event loop) when the phone invalidates the Orbit service.
_invalidation_listeners: dict[str, Callable[[], None]] = {}
# Peripheral address -> its pending ``openL2CAPChannel:``, resolved with the channel.
_channel_waiters: dict[str, asyncio.Future] = {}
_hook_loop: asyncio.AbstractEventLoop | None = None
# How long to wait for BlueZ's MTU exchange after connecting before writing with the default MTU.
BLUEZ_MTU_WAIT = 1.0
# Bound on reading the PSM and opening the channel; past it the session runs over GATT (spec 1.2.1).
L2CAP_OPEN_TIMEOUT = 5.0
# "auto" (L2CAP when the phone offers it), "gatt" or "l2cap" (no GATT fallback): for measurements.
TRANSPORT_ENV = "ORBIT_BLE_TRANSPORT"
_IO_SIZE = 65536


def chunk_size(mtu: int) -> int:
    return max(20, min(512, mtu - 3))


class BleLink:
    """StreamWriter-like adapter over one BLE connection.

    ``drain`` writes buffered bytes to RX in ``ATT_MTU - 3`` chunks, in order, or, after
    ``use_channel``, hands them to the L2CAP channel. Any sign that the link is dead (disconnect,
    invalidated services, a failed write, the channel ending) ends the read side at once, so the
    session closes immediately instead of on its next write or keepalive timeout.
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
        # Bytes handed to the radio: the phone may have them (a write's acknowledgement can be lost).
        self.handed = 0
        self.received = 0
        self.via = "GATT"
        self.started: float | None = None
        self._pump: _ChannelPump | None = None
        self._opening: asyncio.Future | None = None
        self._queued = 0  # bytes given to the channel's pump
        self._progress = asyncio.Event()

    def set_chunk(self, chunk: int) -> None:
        self._chunk = chunk

    def feed(self, data: bytes) -> None:
        if self.lost_cause is None:
            self.received += len(data)
            self._reader.feed_data(data)

    def use_channel(self, channel: Any, psm: int) -> None:
        """Carry the session over ``channel`` (a ``CBL2CAPChannel``) from now on."""
        self.via = f"L2CAP PSM {psm}"
        self._pump = _ChannelPump(channel, f"orbit-l2cap-{self.address}", self.feed, self._sent, self.lost)

    def lost(self, cause: str, eof: bool = False) -> None:
        """``eof``: the phone closed the stream, so what it sent before is still read."""
        if self.lost_cause is not None:
            return
        self.lost_cause = cause
        log.debug("BLE link to %s lost: %s", self.address, cause)
        if eof:
            self._reader.feed_eof()
        else:
            self._reader.set_exception(LinkDown(f"BLE link lost: {cause}", sent=True))
        self.close()

    def write(self, data: bytes) -> None:
        if self.lost_cause is not None:
            raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=False)
        self._buf += data

    async def drain(self) -> None:
        if self._pump is not None:
            await self._drain_channel()
            return
        async with self._lock:
            first = True
            while self._buf:
                if self.lost_cause is not None:
                    raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=not first)
                chunk = bytes(self._buf[: self._chunk])
                del self._buf[: self._chunk]
                self.handed += len(chunk)
                try:
                    await self._client.write_gatt_char(RX_UUID, chunk, response=True)
                except Exception as e:
                    pre_send = isinstance(e, BleakCharacteristicNotFoundError) or str(e) in _NOT_SENT_MESSAGES
                    if pre_send:
                        self.handed -= len(chunk)
                    self.lost(f"write failed: {e}")
                    raise LinkDown(f"BLE write failed: {e}", sent=not (first and pre_send)) from e
                first = False

    async def _drain_channel(self) -> None:
        """Wait until the channel's output stream accepted every byte written so far."""
        assert self._pump is not None
        start = self._queued
        if self._buf:
            if self.lost_cause is not None:
                raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=False)
            self._queued += len(self._buf)
            self._pump.send(bytes(self._buf))
            self._buf.clear()
        while self.handed < self._queued:
            if self.lost_cause is not None:
                raise LinkDown(f"BLE link lost: {self.lost_cause}", sent=self.handed > start)
            self._progress.clear()
            await self._progress.wait()

    def _sent(self, n: int) -> None:
        self.handed += n
        self._progress.set()

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
        self._progress.set()
        if self._opening is not None and not self._opening.done():
            self._opening.set_exception(LinkDown(f"BLE link lost: {self.lost_cause}", sent=False))
        if self._pump is not None:
            self._pump.close()
        if self.started is not None:
            log.info(
                "BLE link to %s via %s ended after %.1f s: %d bytes in, %d bytes out (%s)",
                self.address,
                self.via,
                time.monotonic() - self.started,
                self.received,
                self.handed,
                self.lost_cause,
            )
        task = asyncio.ensure_future(self._client.disconnect())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _on_invalidated(self) -> None:
        self.lost("phone invalidated the Orbit GATT service")


class _ChannelPump:
    """Moves bytes between a ``CBL2CAPChannel``'s NSStreams and the event loop.

    NSStreams deliver their events only on the run loop they are scheduled on and are not
    thread-safe, so a thread of the pump's own runs an NSRunLoop with both streams on it and is the
    only one touching them. The event loop queues bytes and wakes it (``performSelector:onThread:``)
    to write or to stop; it reports bytes read, bytes the output stream accepted and the end of the
    channel with ``call_soon_threadsafe``. ``close`` stops the thread, which closes the streams and
    drops the channel (CoreBluetooth closes a channel nobody retains).
    """

    def __init__(
        self,
        channel: Any,
        name: str,
        on_data: Callable[[bytes], None],
        on_sent: Callable[[int], None],
        on_end: Callable[[str, bool], None],
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._channel = channel  # retained while the pump runs
        self._in, self._out = channel.inputStream(), channel.outputStream()
        self._on_data, self._on_sent, self._on_end = on_data, on_sent, on_end
        self._outbox = bytearray()
        self._lock = threading.Lock()
        self._stopping = False
        self._ended = False
        self._nsthread: Any = None
        self._delegate = _stream_delegate_class().alloc().init()
        self._delegate.pump = self
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    # ---- event loop side -------------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        with self._lock:
            self._outbox += data
        self._wake()

    def close(self) -> None:
        self._stopping = True
        with self._lock:
            self._outbox.clear()
        self._wake()

    def _wake(self) -> None:
        # Before the thread has its NSThread nothing is open yet: it writes once the stream has space.
        if self._nsthread is not None:
            self._delegate.performSelector_onThread_withObject_waitUntilDone_(b"wake:", self._nsthread, None, False)

    # ---- pump thread -----------------------------------------------------------------------------

    def _run(self) -> None:
        import objc
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop, NSThread

        with objc.autorelease_pool():
            self._nsthread = NSThread.currentThread()
            run_loop = NSRunLoop.currentRunLoop()
            streams = (self._in, self._out)
            try:
                for s in streams:
                    s.setDelegate_(self._delegate)
                    s.scheduleInRunLoop_forMode_(run_loop, NSDefaultRunLoopMode)
                    s.open()
                while not self._stopping:
                    with objc.autorelease_pool():
                        # Returns after each event or wake; the limit is only a safety net.
                        run_loop.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(1.0))
            except Exception as e:
                log.exception("L2CAP stream thread failed")
                self._end(f"internal error: {e}")
            finally:
                for s in streams:
                    s.setDelegate_(None)
                    s.close()
                    s.removeFromRunLoop_forMode_(run_loop, NSDefaultRunLoopMode)
                self._delegate.pump = None
                self._channel = self._in = self._out = None

    def _event(self, stream: Any, event: int) -> None:
        from Foundation import (
            NSStreamEventEndEncountered,
            NSStreamEventErrorOccurred,
            NSStreamEventHasBytesAvailable,
            NSStreamEventHasSpaceAvailable,
        )

        if event & NSStreamEventHasBytesAvailable:
            self._read()
        if event & NSStreamEventHasSpaceAvailable:
            self._write()
        if event & NSStreamEventErrorOccurred:
            self._end(f"L2CAP stream error: {_describe(stream.streamError())}")
        if event & NSStreamEventEndEncountered:
            self._end("the phone closed the L2CAP channel", eof=True)

    def _read(self) -> None:
        while not self._ended and self._in.hasBytesAvailable():
            n, data = self._in.read_maxLength_(None, _IO_SIZE)
            if n > 0:
                self._post(self._on_data, bytes(data[:n]))
            elif n == 0:  # the end; ended here too, so this loop cannot spin until EndEncountered
                self._end("the phone closed the L2CAP channel", eof=True)
            else:
                self._end(f"L2CAP read failed: {_describe(self._in.streamError())}")

    def _write(self) -> None:
        with self._lock:
            # Only while there is space: a write to a full NSOutputStream blocks.
            while self._outbox and not self._ended and self._out.hasSpaceAvailable():
                data = bytes(self._outbox[:_IO_SIZE])
                n = self._out.write_maxLength_(data, len(data))
                if n < 0:
                    self._end(f"L2CAP write failed: {_describe(self._out.streamError())}")
                elif n == 0:
                    break
                else:
                    del self._outbox[:n]
                    self._post(self._on_sent, n)

    def _end(self, cause: str, eof: bool = False) -> None:
        if not self._ended:
            self._ended = True
            self._post(self._on_end, cause, eof)

    def _post(self, fn: Callable[..., None], *args: Any) -> None:
        with contextlib.suppress(RuntimeError):  # the event loop is gone (orbitd is exiting)
            self._loop.call_soon_threadsafe(fn, *args)


_delegate_class: Any = None


def _stream_delegate_class() -> Any:
    """The NSStreamDelegate that forwards a pump's stream events and wakes to it (defined once per process)."""
    global _delegate_class
    if _delegate_class is None:
        import objc
        from Foundation import NSObject

        class OrbitL2capStreamDelegate(NSObject):
            def stream_handleEvent_(self, stream: Any, event: int) -> None:
                self._call(lambda pump: pump._event(stream, event))

            def wake_(self, _arg: Any) -> None:
                self._call(lambda pump: pump._write())

            @objc.python_method
            def _call(self, fn: Callable[[_ChannelPump], None]) -> None:
                pump = self.pump
                try:
                    fn(pump)
                except Exception as e:  # must not propagate into the run loop
                    log.exception("L2CAP stream handling failed")
                    pump._end(f"internal error: {e}")

        _delegate_class = OrbitL2capStreamDelegate
    return _delegate_class


def _describe(error: Any) -> str:
    return str(error.localizedDescription()) if error is not None else "unknown error"


def services_invalidated(address: str, service_uuids: list[str]) -> None:
    """Entry point for platform hooks: the peripheral at ``address`` dropped some of its services."""
    if SERVICE_UUID in (u.lower() for u in service_uuids) and (cb := _invalidation_listeners.get(address)):
        cb()


def channel_opened(address: str, channel: Any, error: Any) -> None:
    """Entry point for platform hooks: ``openL2CAPChannel:`` on the peripheral at ``address`` finished."""
    waiter = _channel_waiters.pop(address, None)
    if waiter is None or waiter.done():
        log.debug("dropping a late L2CAP channel to %s", address)  # released, so CoreBluetooth closes it
    elif error is not None:
        waiter.set_exception(ConnectionError(_describe(error)))
    else:
        waiter.set_result(channel)


def _install_corebluetooth_hook() -> None:
    """Route the delegate calls bleak ignores to :func:`services_invalidated` and :func:`channel_opened`."""
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

    def did_open_channel(self: Any, peripheral: Any, channel: Any, error: Any) -> None:  # CoreBluetooth queue
        try:
            address = str(peripheral.identifier().UUIDString())
            _hook_loop.call_soon_threadsafe(channel_opened, address, channel, error)
        except Exception:
            log.debug("ignoring peripheral:didOpenL2CAPChannel:error:", exc_info=True)

    objc.classAddMethods(
        ObjcPeripheralDelegate,
        [
            objc.selector(did_modify_services, selector=b"peripheral:didModifyServices:", signature=b"v@:@@"),
            objc.selector(did_open_channel, selector=b"peripheral:didOpenL2CAPChannel:error:", signature=b"v@:@@@"),
        ],
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


def _transport_mode() -> str:
    mode = os.environ.get(TRANSPORT_ENV, "auto").strip().lower() or "auto"
    if mode not in ("auto", "gatt", "l2cap"):
        log.warning("ignoring %s=%s (expected auto, gatt or l2cap)", TRANSPORT_ENV, mode)
        return "auto"
    return mode


async def _open_channel(client: Any, link: BleLink, mode: str) -> tuple[Any, int] | str:
    """The phone's L2CAP channel and its PSM (spec 1.2.1), else why the session runs over GATT."""
    if mode == "gatt":
        return f"{TRANSPORT_ENV}=gatt"
    if sys.platform != "darwin":
        # BlueZ opens LE channels through AF_BLUETOOTH sockets, but Python's socket module cannot set
        # the LE address type they need, and nothing but CoreBluetooth was tried on hardware.
        return "L2CAP channels are implemented for CoreBluetooth only"
    char = client.services.get_characteristic(PSM_UUID) if client.services else None
    if char is None:
        return "the phone offers no L2CAP channel"
    step = "reading the PSM"
    try:
        async with asyncio.timeout(L2CAP_OPEN_TIMEOUT):
            value = await client.read_gatt_char(char)
            if len(value) != 2:
                return f"bad PSM value {bytes(value).hex() or '(empty)'}"
            psm = int.from_bytes(value, "little")
            step = f"opening L2CAP PSM {psm}"
            link._opening = _channel_waiters[link.address] = asyncio.get_running_loop().create_future()
            client._backend._peripheral.openL2CAPChannel_(psm)
            return await link._opening, psm
    except TimeoutError:
        return f"{step} took over {L2CAP_OPEN_TIMEOUT:g} s"
    except LinkDown:
        raise
    except Exception as e:
        return f"{step} failed: {e}"
    finally:
        if link._opening is not None and _channel_waiters.get(link.address) is link._opening:
            del _channel_waiters[link.address]
        link._opening = None


async def open_ble(device: Any, link_timeout: float) -> FrameStream:
    """Connect, open the session's L2CAP channel or subscribe to TX, and return the link as a FrameStream.

    ``link_timeout`` bounds only establishing the radio link (an address the phone has rotated away
    from never answers); service discovery afterwards is bounded by the caller.
    """
    from bleak import BleakClient

    if sys.platform == "darwin":
        try:
            _install_corebluetooth_hook()
        except Exception as e:  # never block connecting on the hook
            log.warning("cannot watch for invalidated GATT services or L2CAP channels: %s", e)

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
        link = BleLink(client, reader, 20)
        _invalidation_listeners[link.address] = link._on_invalidated
        mode = _transport_mode()
        opened = await _open_channel(client, link, mode)
        if isinstance(opened, tuple):
            link.use_channel(*opened)  # the phone's session starts with the channel: no notifications
        elif mode == "l2cap":
            raise ConnectionError(f"no L2CAP channel ({TRANSPORT_ENV}=l2cap): {opened}")
        else:
            # Subscribe before the first write: the session starts when notifications are enabled.
            await client.start_notify(TX_UUID, lambda _ch, data: link.feed(bytes(data)))
            link.via = f"GATT ({opened})"
        mtu = await _mtu(client)
        link.set_chunk(chunk_size(mtu))
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            _abort_pending_connect(client)
        if link is not None:
            link.close()
        else:
            with contextlib.suppress(Exception):
                await client.disconnect()
        raise
    link.started = time.monotonic()
    log.info("BLE link to %s, MTU %d, via %s", client.address, mtu, link.via)
    return FrameStream(reader, link)


async def _mtu(client: Any) -> int:
    """The connection's ATT MTU.

    On BlueZ bleak only knows it after ``_acquire_mtu``, which calls AcquireNotify on TX and closes
    the fd: BlueZ then writes the TX CCCD to 0 and the phone ends the session it just started
    (spec 1.2). The characteristic's ``MTU`` property (BlueZ >= 5.62) has it without side effects;
    it reads 23 until the MTU exchange that BlueZ starts on connect completes.
    """
    backend = getattr(client, "_backend", None)
    if not hasattr(backend, "_acquire_mtu"):
        return client.mtu_size
    char = client.services.get_characteristic(RX_UUID) if client.services else None
    if char is None:
        return 23
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BLUEZ_MTU_WAIT
    while (mtu := char.max_write_without_response_size + 3) <= 23 and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)  # a D-Bus property change; bleak offers no event for it
    return mtu
