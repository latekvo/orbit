"""BLE GATT central transport (spec/PROTOCOL.md 1.2), presented as a FrameStream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..proto import FrameStream

log = logging.getLogger(__name__)

SERVICE_UUID = "616dfa5c-069e-4839-8335-87edbeb2c1a5"
RX_UUID = "616dfa5d-069e-4839-8335-87edbeb2c1a5"  # I -> R, write with response
TX_UUID = "616dfa5e-069e-4839-8335-87edbeb2c1a5"  # R -> I, notify


def chunk_size(mtu: int) -> int:
    return max(20, min(512, mtu - 3))


class BleWriter:
    """StreamWriter-like adapter: ``drain`` writes buffered bytes to RX in ``ATT_MTU - 3`` chunks, in order."""

    def __init__(self, client: Any, chunk: int) -> None:
        self._client = client
        self._chunk = chunk
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("BLE link closed")
        self._buf += data

    async def drain(self) -> None:
        async with self._lock:
            while self._buf:
                chunk = bytes(self._buf[: self._chunk])
                del self._buf[: self._chunk]
                await self._client.write_gatt_char(RX_UUID, chunk, response=True)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            task = asyncio.ensure_future(self._client.disconnect())
            task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def open_ble(device: Any) -> FrameStream:
    from bleak import BleakClient

    reader = asyncio.StreamReader()
    client = BleakClient(device, disconnected_callback=lambda _c: reader.feed_eof())
    await client.connect()
    try:
        backend = getattr(client, "_backend", None)
        if hasattr(backend, "_acquire_mtu"):  # BlueZ reports 23 until the MTU is acquired
            with contextlib.suppress(Exception):
                await backend._acquire_mtu()
        # Subscribe before the first write: the session starts when notifications are enabled.
        await client.start_notify(TX_UUID, lambda _ch, data: reader.feed_data(bytes(data)))
    except BaseException:
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise
    log.info("BLE link to %s, MTU %d", client.address, client.mtu_size)
    return FrameStream(reader, BleWriter(client, chunk_size(client.mtu_size)))
