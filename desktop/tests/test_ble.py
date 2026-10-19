"""BLE transport and LAN preference, with bleak's client replaced by an in-memory GATT link to a responder."""

import asyncio
import sys
import time
from types import SimpleNamespace

import bleak
import pytest
from daemon_helpers import RawPhone, pin_in, until

from orbit.client import ApiError
from orbit.daemon import ble, core
from orbit.daemon.ble import RX_UUID, TX_UUID, BleLink, chunk_size
from orbit.daemon.session import SENT_THEN_LOST
from orbit.daemon.timeconv import now_ms
from orbit.proto import FrameStream, KeyPair, device_id, respond

SNAPSHOT = [{"id": "media:x", "kind": "media", "title": "Over BLE", "actions": []}]


async def phone_session(ch, _link=None):
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
    script = None  # responder coroutine(channel, link), else phone_session
    instances: list["FakeGattLink"] = []
    # Addresses the phone rotated away from: like CoreBluetooth, connect() waits out bleak's timeout.
    gone: set[str] = set()
    mtu = 23

    def __init__(self, device, disconnected_callback=None, timeout=10.0, **_):
        self.address = device.address
        self.mtu_size = self.mtu
        self.timeout = timeout
        self.log = []
        self.write_error = None  # raised by the next write_gatt_char, before it reaches the phone
        self.lose_write_response = False  # the next write reaches the phone, then the link drops
        self.acks = True  # False: writes are never acknowledged (a stalled link)
        self.notify_budget = None  # notifications still delivered before the link stalls
        self.notify_delay = 0.0  # seconds between notifications
        self.write_delay = 0.0  # seconds each acknowledged write takes
        self._outbox = asyncio.Queue()
        self.connected = False
        self._disconnected = disconnected_callback
        self._notify = None
        self._phone_rx = asyncio.StreamReader()
        central = SimpleNamespace(cancelPeripheralConnection_=lambda _p: self.log.append("cancelPeripheralConnection"))
        self._backend = SimpleNamespace(
            _peripheral=object(), _central_manager_delegate=SimpleNamespace(central_manager=central)
        )
        FakeGattLink.instances.append(self)

    async def connect(self):
        self.log.append("connect")
        if self.address in self.gone:
            await asyncio.sleep(self.timeout)
            raise TimeoutError
        self.connected = True
        link = self

        class NotifyWriter:
            def write(self, data):
                for i in range(0, len(data), 20):
                    link._outbox.put_nowait(bytearray(data[i : i + 20]))

            async def drain(self):
                await asyncio.sleep(0)

            def close(self):
                pass

        async def run_phone():
            identity = self.identities.get(self.address) or self.phone_identity
            ch = await respond(FrameStream(self._phone_rx, NotifyWriter()), identity, "BLE Phone", "android")
            await (self.script or phone_session)(ch, self)

        async def notify():
            while True:
                chunk = await self._outbox.get()
                if self.notify_delay:
                    await asyncio.sleep(self.notify_delay)
                if self.notify_budget is not None:
                    if self.notify_budget <= 0:
                        continue
                    self.notify_budget -= 1
                self._notify(None, chunk)

        self._task = asyncio.ensure_future(run_phone())
        self._pump = asyncio.ensure_future(notify())

    async def start_notify(self, uuid, callback):
        self.log.append(("notify", uuid))
        self._notify = callback

    async def write_gatt_char(self, uuid, data, response=None):
        if self.write_error:
            raise self.write_error
        if not self.acks:
            self.log.append(("write unacknowledged", len(data)))
            await asyncio.Event().wait()
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.log.append(("write", uuid, len(data), response))
        self._phone_rx.feed_data(bytes(data))
        if self.lose_write_response:
            await asyncio.sleep(0.05)  # the phone reads it
            self.drop()
            raise bleak.exc.BleakError("disconnected")

    async def disconnect(self):
        if self.connected:  # bleak returns early for a peripheral that is not connected
            self.log.append("disconnect")
            self.drop()

    def drop(self):
        """The radio link went away (CoreBluetooth ``didDisconnectPeripheral``)."""
        self.connected = False
        if hasattr(self, "_task"):
            self._task.cancel()
            self._pump.cancel()
        if self._disconnected:
            callback, self._disconnected = self._disconnected, None
            callback(self)


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
    sent = []

    class Client:
        address = "AA"

        async def write_gatt_char(self, uuid, data, response):
            await asyncio.sleep(0)
            sent.append(bytes(data))

    w = BleLink(Client(), asyncio.StreamReader(), chunk_size(mtu))
    payload = bytes(range(256)) * 4
    w.write(payload[:500])
    w.write(payload[500:])
    await asyncio.gather(w.drain(), w.drain())
    assert b"".join(sent) == payload
    assert max(map(len, sent)) == expected


async def pairing_phone(ch, _link=None):
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
    FakeGattLink.gone = set()
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


async def test_provisional_entry_merges_into_mdns_discovered_device(make_daemon, api_for, gatt):
    phone_id = device_id(KeyPair.generate().public)
    daemon = await make_daemon()
    api = await api_for(daemon)
    daemon.on_ble(phone_id[:16], SimpleNamespace(address="DD"), "Pixel D")
    daemon.on_lan(
        "Pixel D._orbit._tcp.local.",
        {"id": phone_id, "name": "Pixel D", "platform": "android", "addresses": [("127.0.0.1", 9)]},
    )
    (dev,) = await api.call("devices.list")
    assert (dev["id"], dev["provisional"], dev["discovered"]) == (phone_id, False, ["lan", "ble"])


# ---- link loss, address rotation and reconnecting (spec/LOCAL_API.md "state" / "stale") ----------


async def serving_phone(ch, link):
    """Paired phone that answers actions, recording each one it receives on ``link.actions``."""
    await ch.send({"t": "ready", "paired": True, "now": now_ms()})
    await ch.recv()
    await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
    while True:
        msg = await ch.recv()
        if msg["t"] == "ping":
            await ch.send({"t": "pong", "req": msg["req"]})
        elif msg["t"] == "action":
            link.actions.append(msg["action"])
            if not link.hold_results:
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})


@pytest.fixture
async def ble_phone(make_daemon, api_for, gatt, short_tmp, monkeypatch):
    """A paired BLE-only phone and a daemon with fast reconnect timings; returns (daemon, api, device id)."""
    monkeypatch.setattr(core, "STABLE_SESSION", 0.0)
    monkeypatch.setattr(FakeGattLink, "actions", None, raising=False)
    monkeypatch.setattr(FakeGattLink, "hold_results", False, raising=False)
    orig_init = FakeGattLink.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.actions = []

    monkeypatch.setattr(FakeGattLink, "__init__", init)
    gatt.gone = set()
    gatt.script = staticmethod(serving_phone)
    gatt.phone_identity = KeyPair.generate()
    pin_in(short_tmp / "daemon", gatt.phone_identity, name="BLE Phone")

    async def make(**kw):
        kw.setdefault("ble_link_timeout", 0.3)
        daemon = await make_daemon(**kw)
        return daemon, await api_for(daemon), next(iter(daemon.devices))

    return make


def link_to(address):
    return next((i for i in reversed(FakeGattLink.instances) if i.address == address), None)


def connected_via(daemon, dev_id, address):
    s = daemon.devices[dev_id].session
    return (
        s is not None
        and s.address == address
        and daemon.devices[dev_id].activities
        and not daemon.devices[dev_id].stale
    )


async def test_invalidated_services_end_the_session_at_once(ble_phone):
    daemon, api, dev_id = await ble_phone()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    session = daemon.devices[dev_id].session
    writes = len(link_to("A1").log)

    ble.services_invalidated("A1", [ble.SERVICE_UUID.upper()])
    await asyncio.wait_for(session.closed.wait(), 0.1)
    assert "invalidated" in session.close_reason
    assert link_to("A1").log[writes:] == ["disconnect"]  # detected without writing anything
    # Another peripheral's or another service's invalidation is not ours.
    await until(lambda: connected_via(daemon, dev_id, "A1"), timeout=0.5)  # reconnected without a backoff
    session = daemon.devices[dev_id].session
    ble.services_invalidated("B2", [ble.SERVICE_UUID])
    ble.services_invalidated("A1", ["180F"])
    await asyncio.sleep(0.05)
    assert not session.closed.is_set()


async def test_freshest_advertisement_is_tried_first_and_stale_ones_time_out_fast(ble_phone):
    daemon, api, dev_id = await ble_phone(connect_timeout=3.0)
    FakeGattLink.gone = {"OLD"}
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="OLD"))
    await until(lambda: link_to("OLD"))
    started = asyncio.get_running_loop().time()
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"id": dev_id})
    # bleak's own link timeout (0.3 s), not the 3 s connect_timeout, bounds a rotated-away address.
    assert e.value.code == "timeout" and asyncio.get_running_loop().time() - started < 1.0
    assert link_to("OLD").timeout == 0.3

    daemon.on_ble(dev_id[:16], SimpleNamespace(address="NEW"))
    await until(lambda: connected_via(daemon, dev_id, "NEW"))
    first = daemon.devices[dev_id].session
    FakeGattLink.instances.clear()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="OLD"))  # seen again, but less recently than NEW...
    daemon.devices[dev_id].ble["NEW"] = (daemon.devices[dev_id].ble["NEW"][0], time.monotonic() + 1)
    first.channel.close()
    await until(lambda: connected_via(daemon, dev_id, "NEW") and daemon.devices[dev_id].session is not first)
    assert [i.address for i in FakeGattLink.instances] == ["NEW"]  # ...so NEW goes first


async def test_new_advertisement_abandons_an_attempt_to_a_stale_address(ble_phone):
    daemon, api, dev_id = await ble_phone(ble_link_timeout=30.0)
    FakeGattLink.gone = {"STALE"}
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="STALE"))
    await until(lambda: link_to("STALE"))
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="FRESH"))
    await until(lambda: connected_via(daemon, dev_id, "FRESH"), timeout=1.0)
    # The CoreBluetooth connect to the stale peripheral is cancelled, not left pending.
    assert link_to("STALE").log == ["connect", "cancelPeripheralConnection"]


async def test_action_during_reconnect_waits_and_is_sent_once(ble_phone):
    daemon, api, dev_id = await ble_phone()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    FakeGattLink.gone = {"A1"}  # rotated: reconnecting needs the next advertisement
    link_to("A1").drop()
    await until(lambda: daemon.devices[dev_id].session is None)
    dev = (await api.call("devices.list"))[0]
    assert (dev["connected"], dev["state"]) == (False, "reconnecting")
    acts = await api.call("activities.list")
    assert [a["id"] for a in acts] == ["media:x"] and acts[0]["stale"] is True

    action = asyncio.ensure_future(
        api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.pause"})
    )
    await asyncio.sleep(0.5)
    assert not action.done()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A2"))
    assert await asyncio.wait_for(action, 2) == {"ok": True}
    assert link_to("A1").actions == [] and link_to("A2").actions == ["media.pause"]
    assert (await api.call("devices.list"))[0]["state"] == "connected"
    assert all(a["stale"] is False for a in await api.call("activities.list"))


async def test_unsent_action_moves_to_the_next_session(ble_phone):
    daemon, api, dev_id = await ble_phone()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    # bleak refuses the write before it reaches the radio (the real log's services-invalidated case).
    link_to("A1").write_error = bleak.exc.BleakError("Service Discovery has not been performed yet")
    result = await api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.next"})
    assert result == {"ok": True}
    assert [i.actions for i in FakeGattLink.instances] == [[], ["media.next"]]


@pytest.mark.parametrize("loss", ["awaiting result", "write in flight"])
async def test_action_sent_before_the_link_dropped_is_not_retried(ble_phone, loss, monkeypatch):
    monkeypatch.setattr(FakeGattLink, "mtu", 247)  # the action fits in one write
    daemon, api, dev_id = await ble_phone()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    FakeGattLink.hold_results = True
    link_to("A1").lose_write_response = loss == "write in flight"
    action = asyncio.ensure_future(
        api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.play"})
    )
    await until(lambda: link_to("A1").actions)
    FakeGattLink.hold_results = False
    if loss == "awaiting result":
        link_to("A1").drop()
    with pytest.raises(ApiError) as e:
        await asyncio.wait_for(action, 2)
    assert e.value.code == "failed" and "may have been applied" in e.value.message
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    await asyncio.sleep(0.1)
    assert [i.actions for i in FakeGattLink.instances] == [["media.play"], []]


async def test_action_gives_up_with_reconnecting_and_activities_expire(ble_phone):
    daemon, api, dev_id = await ble_phone(action_wait=0.3, stale_grace=0.8)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    FakeGattLink.gone = {"A1"}
    link_to("A1").drop()
    await until(lambda: daemon.devices[dev_id].session is None)
    params = {"device": dev_id, "activity": "media:x", "action": "media.pause"}
    started = asyncio.get_running_loop().time()
    with pytest.raises(ApiError) as e:
        await api.call("activities.action", params)
    assert (e.value.code, e.value.message) == ("not_connected", "reconnecting")
    assert 0.25 < asyncio.get_running_loop().time() - started < 0.6
    await until(lambda: daemon.devices[dev_id].grace is None)
    assert await api.call("activities.list") == []
    assert (await api.call("devices.list"))[0]["state"] == "offline"
    with pytest.raises(ApiError) as e:  # offline: no waiting
        await asyncio.wait_for(api.call("activities.action", params), 0.1)
    assert (e.value.code, e.value.message) == ("not_connected", "phone offline")


async def test_real_log_scenario_invalidated_services_rotated_address(ble_phone, caplog):
    """orbitd.log 21:21:43: the phone re-registered its GATT service and rotated its address while
    an older advertisement (6212D300) was still cached; three actions were clicked in the gap."""
    caplog.set_level("INFO", "orbit")
    daemon, api, dev_id = await ble_phone(ble_link_timeout=10.0)  # the old bleak default: only the race helps
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="81A73D45"))
    await until(lambda: connected_via(daemon, dev_id, "81A73D45"))
    FakeGattLink.gone = {"81A73D45", "6212D300"}
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="6212D300"))  # advertised while connected
    await api.call("subscribe")
    loop = asyncio.get_running_loop()

    t0 = loop.time()
    ble.services_invalidated("81A73D45", [ble.SERVICE_UUID.upper()])
    clicks = [
        asyncio.ensure_future(api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": a}))
        for a in ("media.pause", "media.play", "media.next")
    ]
    await until(lambda: link_to("6212D300"))  # reconnect starts at once, with the freshest advertisement
    await asyncio.sleep(0.3)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="1372BEF3"))  # the service is back, new address
    results = await asyncio.wait_for(asyncio.gather(*clicks), 3)
    elapsed = loop.time() - t0

    assert results == [{"ok": True}] * 3
    assert sorted(link_to("1372BEF3").actions) == ["media.next", "media.pause", "media.play"]
    assert link_to("81A73D45").actions == [] and link_to("6212D300").log == ["connect", "cancelPeripheralConnection"]
    assert elapsed < 1.5
    events = queued_events(api)
    states = [d["devices"][0]["state"] for e, d in events if e == "devices.changed"]
    assert "reconnecting" in states and states[-1] == "connected"
    acts = [d["activities"] for e, d in events if e == "activities.changed"]
    assert all(acts) and acts[-1][0]["stale"] is False  # never blanked while reconnecting
    lines = [r.getMessage() for r in caplog.records]
    assert any("session_closed" in m and "invalidated" in m for m in lines)
    assert any(m.startswith("reconnected device=") and "1372BEF3" in m for m in lines)


def queued_events(api):
    items = []
    while not api._events.empty():
        items.append(api._events.get_nowait())
    return [i for i in items if i is not None]


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreBluetooth only")
async def test_corebluetooth_did_modify_services_reaches_the_link():
    from bleak.backends.corebluetooth.PeripheralDelegate import ObjcPeripheralDelegate

    ble._install_corebluetooth_hook()
    calls = []
    ble._invalidation_listeners["PERIPHERAL-1"] = lambda: calls.append(1)

    class Peripheral:
        def identifier(self):
            return SimpleNamespace(UUIDString=lambda: "PERIPHERAL-1")

    service = SimpleNamespace(UUID=lambda: SimpleNamespace(UUIDString=lambda: ble.SERVICE_UUID.upper()))
    try:
        delegate = ObjcPeripheralDelegate.alloc().init()
        # Through the Objective-C runtime, as CoreBluetooth calls it.
        delegate.performSelector_withObject_withObject_(b"peripheral:didModifyServices:", Peripheral(), [service])
        await until(lambda: calls)
    finally:
        del ble._invalidation_listeners["PERIPHERAL-1"]


# ---- stalled links and bounded API answers (orbitd.log 22:07:50-22:08:22) -------------------------


@pytest.fixture
async def unretrieved():
    """Collects "exception was never retrieved" reports for futures and tasks."""
    import gc

    loop = asyncio.get_running_loop()
    seen = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx.get("message")))

    def check():
        gc.collect()
        return [m for m in seen if "never retrieved" in (m or "")]

    yield check
    loop.set_exception_handler(previous)


async def test_stalled_link_is_torn_down_and_clicks_answered_within_the_bound(ble_phone, unretrieved):
    """The real log: after a notification burst the link stalled both ways; a write was never
    acknowledged and bleak waited ~30 s for the ATT timeout while clicks hung."""
    daemon, api, dev_id = await ble_phone(send_timeout=0.5, action_deadline=3.0)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    stalled = link_to("A1")
    stalled.acks, stalled.notify_budget = False, 0
    session = daemon.devices[dev_id].session
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    def click(action):
        params = {"device": dev_id, "activity": "media:x", "action": action}
        return asyncio.ensure_future(api.call("activities.action", params))

    first = click("media.pause")
    await until(lambda: any(e[0] == "write unacknowledged" for e in stalled.log if isinstance(e, tuple)))
    second = click("media.next")  # queued behind the stalled write: never left this computer
    await asyncio.wait_for(session.closed.wait(), 1.0)
    assert "link stalled" in session.close_reason and loop.time() - t0 < 1.0
    assert "disconnect" in stalled.log

    with pytest.raises(ApiError) as e:
        await first
    assert (e.value.code, e.value.message) == ("failed", SENT_THEN_LOST)
    assert await second == {"ok": True}
    assert loop.time() - t0 < 3.0
    fresh = FakeGattLink.instances[-1]
    assert fresh is not stalled and fresh.actions == ["media.next"] and stalled.actions == []
    assert connected_via(daemon, dev_id, "A1")
    assert unretrieved() == []


async def test_frame_stalled_mid_way_kills_the_link(ble_phone):
    daemon, api, dev_id = await ble_phone(stall_after=0.3, dead_after=10.0, keepalive=10.0)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    session = daemon.devices[dev_id].session
    link_to("A1").notify_budget = 1  # the action_result frame stops after its first 20 bytes
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(ApiError) as e:
        await api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.play"})
    assert e.value.code == "failed"
    assert "mid-frame" in session.close_reason and 0.25 < loop.time() - t0 < 1.0


async def test_slowly_arriving_frame_is_not_mistaken_for_a_dead_link(ble_phone, monkeypatch):
    # Each notification resets the receive timer, not each completed message.
    monkeypatch.setattr(FakeGattLink, "mtu", 23)
    daemon, api, dev_id = await ble_phone(dead_after=0.4, stall_after=0.3, keepalive=10.0)
    orig_connect = FakeGattLink.connect

    async def slow_connect(self):
        await orig_connect(self)
        self.notify_delay = 0.02

    monkeypatch.setattr(FakeGattLink, "connect", slow_connect)
    big = [{"id": "media:x", "kind": "media", "title": "x" * 1500, "actions": []}]
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOT", big)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    # ~80 notifications at 20 ms: the snapshot takes ~1.6 s, four times dead_after.
    await until(lambda: daemon.devices[dev_id].activities, timeout=5)
    assert len(FakeGattLink.instances) == 1 and daemon.devices[dev_id].session is not None


async def test_every_api_call_has_an_upper_bound(make_daemon, api_for, monkeypatch):
    from orbit.daemon import api as api_module

    monkeypatch.setattr(api_module, "CALL_TIMEOUT", 0.2)
    daemon = await make_daemon()
    client = await api_for(daemon)

    async def hang(_params):
        await asyncio.Event().wait()

    monkeypatch.setattr(daemon, "api_connect", hang)
    with pytest.raises(ApiError) as e:
        await asyncio.wait_for(client.call("devices.connect", {"id": "x"}), 1)
    assert (e.value.code, e.value.message) == ("timeout", "orbitd gave up after 0.2 s")


async def test_action_without_a_result_times_out_within_the_bound(ble_phone):
    daemon, api, dev_id = await ble_phone(action_deadline=0.5)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    FakeGattLink.hold_results = True
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(ApiError) as e:
        await api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.play"})
    assert (e.value.code, e.value.message) == ("timeout", "phone did not respond")
    assert 0.45 < loop.time() - t0 < 0.8


async def test_giving_up_on_an_action_never_cuts_its_frame_in_half(ble_phone):
    daemon, api, dev_id = await ble_phone(action_deadline=0.15)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    session, link = daemon.devices[dev_id].session, link_to("A1")
    link.write_delay = 0.05  # the action frame needs ~5 writes: the deadline passes mid-frame
    params = {"device": dev_id, "activity": "media:x", "action": "media.play"}
    with pytest.raises(ApiError) as e:
        await api.call("activities.action", params)
    assert e.value.code == "timeout"
    link.write_delay = 0.0
    await until(lambda: link.actions == ["media.play"])  # the frame was completed and understood
    daemon.action_deadline = 3.0
    assert await api.call("activities.action", params) == {"ok": True}
    assert daemon.devices[dev_id].session is session


async def test_action_queued_behind_a_send_on_a_closing_tcp_session_moves_on(
    make_daemon, api_for, short_tmp, monkeypatch
):
    got = []

    async def phone(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
        while True:
            msg = await ch.recv()
            if msg["t"] == "action":
                got.append(msg["action"])
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})
            elif msg["t"] == "ping":
                await ch.send({"t": "pong", "req": msg["req"]})

    monkeypatch.setattr(core, "STABLE_SESSION", 0.0)
    async with RawPhone(phone) as raw:
        pin_in(short_tmp / "daemon", raw.identity, address=f"127.0.0.1:{raw.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[raw.id].activities and not daemon.devices[raw.id].stale)
        first = daemon.devices[raw.id].session
        await first._send_lock.acquire()  # a send in progress
        params = {"device": raw.id, "activity": "media:x", "action": "media.next"}
        action = asyncio.ensure_future(api.call("activities.action", params))
        await asyncio.sleep(0.1)
        await first.close("test: link lost while the action waited")
        first._send_lock.release()
        assert await asyncio.wait_for(action, 3) == {"ok": True}
        assert got == ["media.next"] and daemon.devices[raw.id].session is not first


# ---- BLE discovery lifecycle, pairing vs mDNS, connect deadline --------------------------------


class FakeScanner:
    """Stands in for ``bleak.BleakScanner``; ``mode`` is "off" (raise), "block" (hang) or "on"."""

    mode = "off"
    starts = 0

    def __init__(self, detection_callback=None, service_uuids=None):
        pass

    async def start(self):
        FakeScanner.starts += 1
        if self.mode == "block":  # a macOS Bluetooth permission prompt nobody answered yet
            await asyncio.Event().wait()
        if self.mode == "off":
            raise bleak.exc.BleakError("Bluetooth device is turned off")

    async def stop(self):
        pass


@pytest.fixture
def scanner(monkeypatch):
    monkeypatch.setattr(bleak, "BleakScanner", FakeScanner)
    monkeypatch.setattr(core, "BLE_RETRY_MIN", 0.05)
    FakeScanner.mode, FakeScanner.starts = "off", 0
    return FakeScanner


async def test_ble_discovery_is_retried_until_bluetooth_comes_on(make_daemon, api_for, scanner):
    daemon = await asyncio.wait_for(make_daemon(ble=True), 2)  # discovery starts in the background
    api = await api_for(daemon)
    status = await until(lambda: _ble_status(api, "unavailable"))
    assert "turned off" in status["bleError"]
    scanner.mode = "on"
    status = await until(lambda: _ble_status(api, "scanning"))
    assert status["bleError"] is None and scanner.starts >= 2


async def _ble_status(api, state):
    status = await api.call("status")
    return status if status["ble"] == state else None


async def test_blocked_ble_start_does_not_hold_up_startup_or_lan(make_daemon, api_for, scanner, short_tmp):
    from daemon_helpers import RawPhone

    scanner.mode = "block"

    async def phone(ch):
        await phone_session(ch)

    async with RawPhone(phone) as raw:
        pin_in(short_tmp / "daemon", raw.identity, address=f"127.0.0.1:{raw.port}")
        daemon = await asyncio.wait_for(make_daemon(ble=True), 2)
        api = await api_for(daemon)
        assert (await api.call("status"))["ble"] == "starting"
        await until(lambda: daemon.devices[raw.id].session)  # LAN connects meanwhile
        await asyncio.wait_for(daemon.stop(), 2)


async def undecided_phone(ch, _link=None):
    """Unpaired phone showing the SAS; the user has not tapped anything yet."""
    await ch.send({"t": "ready", "paired": False, "now": now_ms()})
    await ch.recv()
    while True:
        msg = await ch.recv()
        if msg["t"] == "ping":
            await ch.send({"t": "pong", "req": msg["req"]})


async def test_pending_ble_pairing_survives_mdns_discovery(make_daemon, api_for, gatt, caplog):
    from daemon_helpers import RawPhone

    phone = KeyPair.generate()
    pid = device_id(phone.public)
    gatt.identities, gatt.script = {"AA": phone}, staticmethod(undecided_phone)
    daemon = await make_daemon()
    api = await api_for(daemon)
    daemon.on_ble(pid[:16], SimpleNamespace(address="AA"), None)
    assert await api.call("devices.connect", {"id": pid[:16]}) == {"id": pid}
    (pairing,) = await api.call("pairing.list")
    async with RawPhone(undecided_phone, identity=phone) as lan:
        daemon.on_lan(
            "P._orbit._tcp.local.",
            {"id": pid, "name": "P", "platform": "android", "addresses": [("127.0.0.1", lan.port)]},
        )
        await asyncio.sleep(0.5)
        assert not lan._writers  # no upgrade attempted while pairing
        assert [p["session"] for p in await api.call("pairing.list")] == [pairing["session"]]
        # Connecting to it explicitly does not replace the pending BLE pairing either.
        assert await api.call("devices.connect", {"host": "127.0.0.1", "port": lan.port}) == {"id": pid}
        assert [p["session"] for p in await api.call("pairing.list")] == [pairing["session"]]
        assert daemon.devices[pid].session.transport == "ble"
    assert "no longer trusts" not in caplog.text


async def test_devices_connect_answers_within_its_deadline(ble_phone):
    daemon, api, dev_id = await ble_phone(ble_link_timeout=30.0, connect_deadline=0.3)
    daemon.devices[dev_id].auto = False  # only the explicit connect below
    FakeGattLink.gone = {"AA"}
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="AA"))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"id": dev_id})
    assert e.value.code == "timeout" and e.value.message.startswith("no connection within 0.3 s")
    assert loop.time() - t0 < 0.6
