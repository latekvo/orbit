"""BLE transport and LAN preference, with bleak's client replaced by an in-memory GATT link to a responder."""

import asyncio
from types import SimpleNamespace

import bleak
import pytest
from conftest import RawPhone, pin_in, until

from orbit.daemon.ble import RX_UUID, TX_UUID, chunk_size
from orbit.daemon.timeconv import now_ms
from orbit.proto import FrameStream, KeyPair, respond

SNAPSHOT = [{"id": "media:x", "kind": "media", "title": "Over BLE", "actions": []}]


async def phone_session(ch):
    await ch.send({"t": "ready", "paired": True, "now": now_ms()})
    await ch.recv()
    # Large enough to need many notification chunks.
    await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT + [{"id": "pad", "text": "x" * 3000}]})
    while True:
        msg = await ch.recv()
        if msg["t"] == "ping":
            await ch.send({"t": "pong", "req": msg["req"]})


class FakeGattLink:
    """Stands in for ``bleak.BleakClient``: RX writes feed a responder, its output arrives as TX notifications."""

    phone_identity: KeyPair
    instances: list["FakeGattLink"] = []

    def __init__(self, device, disconnected_callback=None, **_):
        self.address = device.address
        self.mtu_size = 23
        self.log = []
        self._disconnected = disconnected_callback
        self._notify = None
        self._phone_rx = asyncio.StreamReader()
        FakeGattLink.instances.append(self)

    async def connect(self):
        self.log.append("connect")
        link = self

        class NotifyWriter:
            def write(self, data):
                for i in range(0, len(data), 20):
                    asyncio.get_running_loop().call_soon(link._notify, None, bytearray(data[i : i + 20]))

            async def drain(self):
                await asyncio.sleep(0)

            def close(self):
                pass

        async def run_phone():
            ch = await respond(FrameStream(self._phone_rx, NotifyWriter()), self.phone_identity, "BLE Phone", "android")
            await phone_session(ch)

        self._task = asyncio.ensure_future(run_phone())

    async def start_notify(self, uuid, callback):
        self.log.append(("notify", uuid))
        self._notify = callback

    async def write_gatt_char(self, uuid, data, response=None):
        self.log.append(("write", uuid, len(data), response))
        self._phone_rx.feed_data(bytes(data))

    async def disconnect(self):
        self.log.append("disconnect")
        self._task.cancel()
        if self._disconnected:
            self._disconnected(self)


def test_chunk_size_bounds():
    assert (chunk_size(23), chunk_size(185), chunk_size(10), chunk_size(1000)) == (20, 182, 20, 512)


async def test_ble_session_then_upgrade_to_lan(make_daemon, short_tmp, monkeypatch, api_for):
    identity = KeyPair.generate()
    FakeGattLink.phone_identity = identity
    FakeGattLink.instances = []
    monkeypatch.setattr(bleak, "BleakClient", FakeGattLink)
    pin_in(short_tmp / "daemon", identity, name="BLE Phone")
    daemon = await make_daemon()
    api = await api_for(daemon)
    dev_id = next(iter(daemon.devices))

    # Advertisement service data: first 8 bytes of the device id. Other prefixes are ignored.
    daemon.on_ble("00" * 8, SimpleNamespace(address="11:11"))
    await asyncio.sleep(0.05)
    assert (await api.call("devices.list"))[0]["discovered"] == [] and not FakeGattLink.instances
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="AA:BB:CC:DD:EE:FF"))
    acts = await until(lambda: api.call("activities.list"))
    assert [a["id"] for a in acts] == ["media:x", "pad"]
    (dev,) = await api.call("devices.list")
    assert (dev["transport"], dev["address"], dev["discovered"]) == ("ble", "AA:BB:CC:DD:EE:FF", ["ble"])

    (link,) = FakeGattLink.instances
    first_write = next(i for i, e in enumerate(link.log) if e[0] == "write")
    assert link.log[:2] == ["connect", ("notify", TX_UUID)] and first_write == 2
    writes = [e for e in link.log if e[0] == "write"]
    assert all(uuid == RX_UUID and size <= 20 and resp is True for _, uuid, size, resp in writes)
    assert len(writes) > 3  # the handshake did not fit into one chunk

    async def lan_script(ch):
        await phone_session(ch)

    async with RawPhone(lan_script, identity=identity) as lan_phone:
        daemon.on_lan(
            "BLE Phone._orbit._tcp.local.",
            {"id": dev_id, "name": "BLE Phone", "platform": "android", "addresses": [("127.0.0.1", lan_phone.port)]},
        )
        await until(lambda: daemon.devices[dev_id].session and daemon.devices[dev_id].session.transport == "lan")
        assert "disconnect" in link.log
        (dev,) = await api.call("devices.list")
        assert dev["transport"] == "lan" and dev["discovered"] == ["lan", "ble"]
        assert [a["id"] for a in await until(lambda: api.call("activities.list"))] == ["media:x", "pad"]


@pytest.mark.parametrize("mtu,expected", [(23, 20), (247, 244)])
async def test_ble_writer_chunks_in_order(mtu, expected):
    from orbit.daemon.ble import BleWriter

    sent = []

    class Client:
        async def write_gatt_char(self, uuid, data, response):
            await asyncio.sleep(0)
            sent.append(bytes(data))

    w = BleWriter(Client(), chunk_size(mtu))
    payload = bytes(range(256)) * 4
    w.write(payload[:500])
    w.write(payload[500:])
    await asyncio.gather(w.drain(), w.drain())
    assert b"".join(sent) == payload
    assert max(map(len, sent)) == expected
