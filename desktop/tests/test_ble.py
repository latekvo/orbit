"""BLE transport and LAN preference, with bleak's client replaced by an in-memory GATT link to a responder."""

import asyncio
from types import SimpleNamespace

import bleak
import pytest
from conftest import RawPhone, pin_in, until

from orbit.client import ApiError
from orbit.daemon.ble import RX_UUID, TX_UUID, chunk_size
from orbit.daemon.timeconv import now_ms
from orbit.proto import FrameStream, KeyPair, device_id, respond

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
    identities: dict[str, KeyPair] = {}  # per peripheral address, else phone_identity
    script = None  # responder coroutine(channel), else phone_session
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
            identity = self.identities.get(self.address, self.phone_identity)
            ch = await respond(FrameStream(self._phone_rx, NotifyWriter()), identity, "BLE Phone", "android")
            await (self.script or phone_session)(ch)

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
    FakeGattLink.identities, FakeGattLink.script, FakeGattLink.instances = {}, None, []
    monkeypatch.setattr(bleak, "BleakClient", FakeGattLink)
    pin_in(short_tmp / "daemon", identity, name="BLE Phone")
    daemon = await make_daemon()
    api = await api_for(daemon)
    dev_id = next(iter(daemon.devices))

    # Advertisement service data: first 8 bytes of the device id. Another prefix is a different device.
    daemon.on_ble("00" * 8, SimpleNamespace(address="11:11"))
    await asyncio.sleep(0.05)
    assert (await api.call("devices.list"))[0]["discovered"] == [] and not FakeGattLink.instances
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="AA:BB:CC:DD:EE:FF"))
    acts = await until(lambda: api.call("activities.list"))
    assert [a["id"] for a in acts] == ["media:x", "pad"]
    dev = next(d for d in await api.call("devices.list") if d["id"] == dev_id)
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
        dev = next(d for d in await api.call("devices.list") if d["id"] == dev_id)
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


async def pairing_phone(ch):
    """Unpaired BLE-only phone that accepts pairing, then serves a snapshot."""
    await ch.send({"t": "ready", "paired": False, "now": now_ms()})
    await ch.recv()
    assert (await ch.recv())["t"] == "pair_request"
    await ch.send({"t": "pair_decision", "accepted": True})
    assert (await ch.recv())["t"] == "pair_decision"
    await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
    while True:
        msg = await ch.recv()
        if msg["t"] == "ping":
            await ch.send({"t": "pong", "req": msg["req"]})


@pytest.fixture
def gatt(monkeypatch):
    monkeypatch.setattr(bleak, "BleakClient", FakeGattLink)
    FakeGattLink.identities, FakeGattLink.script, FakeGattLink.instances = {}, staticmethod(pairing_phone), []
    return FakeGattLink


async def test_first_pairing_over_ble_with_provisional_ids(make_daemon, api_for, gatt):
    a, b = KeyPair.generate(), KeyPair.generate()
    a_id, b_id = device_id(a.public), device_id(b.public)
    gatt.identities = {"AA": a, "BB": b}
    daemon = await make_daemon()
    api = await api_for(daemon)
    await api.call("subscribe")
    daemon.on_ble(a_id[:16], SimpleNamespace(address="AA"), "Pixel A")
    daemon.on_ble(b_id[:16], SimpleNamespace(address="BB"), None)
    devices = {d["id"]: d for d in await until(lambda: api.call("devices.list"))}
    assert set(devices) == {a_id[:16], b_id[:16]}
    for prefix, name in ((a_id[:16], "Pixel A"), (b_id[:16], f"Orbit device {b_id[:4]}")):
        d = devices[prefix]
        assert (d["name"], d["provisional"], d["paired"], d["connected"], d["discovered"]) == (
            name,
            True,
            False,
            False,
            ["ble"],
        )
    assert not gatt.instances  # unpaired peripherals are never connected automatically

    assert await api.call("devices.connect", {"id": a_id[:16]}) == {"id": a_id}
    devices = {d["id"]: d for d in await api.call("devices.list")}
    assert set(devices) == {a_id, b_id[:16]}  # the provisional entry was replaced by the real id
    assert devices[a_id]["provisional"] is False and devices[a_id]["transport"] == "ble"
    assert devices[a_id]["address"] == "AA" and devices[b_id[:16]]["provisional"] is True

    (pairing,) = await api.call("pairing.list")
    assert pairing["deviceId"] == a_id
    await api.call("pairing.respond", {"session": pairing["session"], "accept": True})
    await until(lambda: daemon.store.get(a_id) and daemon.devices[a_id].activities)

    # Later advertisements with that prefix belong to the real device; no new provisional entry.
    daemon.on_ble(a_id[:16], SimpleNamespace(address="AA"), "Pixel A")
    assert set(daemon.devices) == {a_id, b_id[:16]}


async def test_ble_responder_must_match_advertised_prefix(make_daemon, api_for, gatt):
    impostor, advertised = KeyPair.generate(), KeyPair.generate()
    gatt.identities = {"CC": impostor}
    prefix = device_id(advertised.public)[:16]
    daemon = await make_daemon()
    api = await api_for(daemon)
    daemon.on_ble(prefix, SimpleNamespace(address="CC"), "Victim")
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"id": prefix})
    assert e.value.code == "failed" and device_id(impostor.public) in e.value.message
    assert "disconnect" in gatt.instances[0].log
    assert set(daemon.devices) == {prefix} and daemon.devices[prefix].provisional
    assert not daemon.store.peers and await api.call("pairing.list") == []
