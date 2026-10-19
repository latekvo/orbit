"""BLE connectivity faults replayed against the real daemon (sessions, reconnect policy, local API).

The GATT link is test_ble.FakeGattLink; tests/ble_radio.py adds range, advertisements and address
rotation. Timings are orbitd's defaults unless a test says otherwise. ``slow`` tests take minutes
of real time and run only with ORBIT_SLOW=1.
"""

import asyncio
import contextlib
import os
import resource
import time
from types import SimpleNamespace

import bleak
import pytest
from ble_radio import Radio, StateLog
from daemon_helpers import until
from test_ble import (  # noqa: F401  (fixtures)
    FakeGattLink,
    ble_phone,
    connected_via,
    gatt,
    link_to,
)

from orbit.client import ApiError

slow = pytest.mark.skipif(not os.environ.get("ORBIT_SLOW"), reason="minutes of real time; set ORBIT_SLOW=1")
DEFAULTS = {"ble_link_timeout": 4.0, "connect_timeout": 10.0}


async def connected_phone(ble_phone, **kw):  # noqa: F811
    daemon, api, dev_id = await ble_phone(**{**DEFAULTS, **kw})
    radio = Radio(daemon, dev_id)
    await until(lambda: connected_via(daemon, dev_id, radio.address))
    return daemon, api, dev_id, radio


def pause(api, dev_id):
    return api.call("activities.action", {"device": dev_id, "activity": "media:x", "action": "media.pause"})


# ---- out of range, then back --------------------------------------------------------------------


async def _outage(ble_phone, seconds):  # noqa: F811
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    log = StateLog(api, dev_id)
    await asyncio.sleep(0.2)
    log.mark()
    loop = asyncio.get_running_loop()
    leaving = asyncio.ensure_future(radio.leave())
    await asyncio.sleep(seconds)
    await leaving
    back = loop.time()
    radio.come_back()
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=120)
    latency = loop.time() - back
    radio.stop()
    log.stop()
    return latency, log


@pytest.mark.parametrize("seconds", [5, 8, 12, 15, 20, 25, 28, 32])
async def test_back_in_range_reconnects_within_5s(ble_phone, seconds):  # noqa: F811
    latency, log = await _outage(ble_phone, seconds)
    print(f"outage {seconds} s: reconnected {latency:.1f} s after coming back; states {log.states}")
    assert latency < 5.0, log.states


@slow
@pytest.mark.parametrize("seconds", [60, 600])
async def test_back_in_range_after_long_outage_reconnects_within_5s(ble_phone, seconds):  # noqa: F811
    latency, log = await _outage(ble_phone, seconds)
    print(f"outage {seconds} s: reconnected {latency:.1f} s after coming back; states {log.states}")
    assert latency < 5.0, log.states


# ---- phone Bluetooth toggled: GATT server restarted ---------------------------------------------


@pytest.mark.parametrize("new_address", [False, True], ids=["same-address", "new-address"])
@pytest.mark.parametrize("off_for", [3, 10])
async def test_phone_bluetooth_toggle_reconnects_within_5s(ble_phone, off_for, new_address):  # noqa: F811
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    loop = asyncio.get_running_loop()
    first = daemon.devices[dev_id].session
    await radio.phone_bluetooth_off()
    await until(lambda: first.closed.is_set(), timeout=2)
    await asyncio.sleep(off_for)
    back = loop.time()
    radio.phone_bluetooth_on("ADDR-2" if new_address else None)
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=60)
    latency = loop.time() - back
    print(f"phone BT off {off_for} s, {'new' if new_address else 'same'} address: back after {latency:.1f} s")
    radio.stop()
    assert latency < 5.0


# ---- a click while the phone is away --------------------------------------------------------------


async def test_click_while_reconnecting_is_delivered_once_the_phone_is_back(ble_phone):  # noqa: F811
    """spec: an action on a "reconnecting" device waits up to 5 s for the session to come back."""
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    loop = asyncio.get_running_loop()
    leaving = asyncio.ensure_future(radio.leave())
    await asyncio.sleep(8)
    await leaving
    click = asyncio.ensure_future(pause(api, dev_id))  # the user clicks...
    await asyncio.sleep(0.5)
    t0 = loop.time()
    radio.come_back()  # ...and the phone is back 0.5 s later, well within the 5 s the action waits
    try:
        result = await click
    except ApiError as e:
        result = (e.code, e.message)
    print(f"click answered {loop.time() - t0:.1f} s after the phone came back: {result}")
    radio.stop()
    assert result == {"ok": True}
    assert sum(len(i.actions) for i in FakeGattLink.instances) == 1


# ---- invalidated services / ATT errors in the middle of an action frame --------------------------


class InterruptedGatt(FakeGattLink):
    """``on_write(link, n)`` runs before the n-th RX write of this connection (1-based)."""

    on_write = None

    async def write_gatt_char(self, uuid, data, response=None):
        self.writes = getattr(self, "writes", 0) + 1
        if self.on_write:
            self.on_write(self, self.writes)
        await super().write_gatt_char(uuid, data, response)


@pytest.fixture
def interrupted(monkeypatch):
    monkeypatch.setattr(bleak, "BleakClient", InterruptedGatt)
    monkeypatch.setattr(InterruptedGatt, "on_write", None)
    return InterruptedGatt


async def _armed_click(ble_phone, interrupted, hook):  # noqa: F811
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    link = link_to(radio.address)
    start = link.writes
    interrupted.on_write = lambda lk, n: hook(lk, n - start) if lk is link else None
    try:
        result = await pause(api, dev_id)
    except ApiError as e:
        result = (e.code, e.message)
    radio.stop()
    return result, link


async def test_services_invalidated_after_the_first_chunk_of_an_action_retries_it(
    ble_phone,  # noqa: F811
    interrupted,
):
    """MTU 23: the action frame takes ~5 writes. The phone re-registers its service (didModifyServices)
    while the first one is in flight; the phone can never decrypt a 20-byte prefix of a frame."""
    from orbit.daemon import ble

    def hook(link, n):
        if n == 1:
            asyncio.get_running_loop().call_soon(ble.services_invalidated, link.address, [ble.SERVICE_UUID])

    result, link = await _armed_click(ble_phone, interrupted, hook)
    applied = [i.actions for i in FakeGattLink.instances]
    print(f"result {result}; actions per link {applied}; writes on the first link {link.writes}")
    assert link.actions == []  # provably not applied: the frame never completed
    assert result == {"ok": True} and sum(map(len, applied)) == 1


async def test_att_error_on_the_first_chunk_retries_the_action(ble_phone, interrupted):  # noqa: F811
    """The phone answered the write with an ATT error (BleServer: no link for this central): it
    refused the bytes, so nothing of the action reached the session."""

    def hook(link, n):
        if n == 1:
            link.write_error = bleak.exc.BleakGATTProtocolError(0x0E)

    result, link = await _armed_click(ble_phone, interrupted, hook)
    applied = [i.actions for i in FakeGattLink.instances]
    print(f"result {result}; actions per link {applied}")
    assert link.actions == []
    assert result == {"ok": True} and sum(map(len, applied)) == 1


# ---- BLE -> LAN switch while an action is in flight ----------------------------------------------


async def test_switch_to_lan_does_not_fail_an_action_awaiting_its_result(ble_phone, monkeypatch):  # noqa: F811
    """The phone's Wi-Fi comes back (mDNS) while a click on the BLE session waits for its result."""
    from daemon_helpers import RawPhone
    from test_ble import SNAPSHOT

    from orbit.daemon.timeconv import now_ms

    async def slow_ble_phone(ch, link):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
        while True:
            msg = await ch.recv()
            if msg["t"] == "ping":
                await ch.send({"t": "pong", "req": msg["req"]})
            elif msg["t"] == "action":
                link.actions.append(msg["action"])
                await asyncio.sleep(1.0)  # e.g. the media app takes a moment
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    lan_actions = []

    async def lan_phone(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
        while True:
            msg = await ch.recv()
            if msg["t"] == "ping":
                await ch.send({"t": "pong", "req": msg["req"]})
            elif msg["t"] == "action":
                lan_actions.append(msg["action"])
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    monkeypatch.setattr(FakeGattLink, "script", staticmethod(slow_ble_phone))
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    async with RawPhone(lan_phone, identity=FakeGattLink.phone_identity) as lan:
        click = asyncio.ensure_future(pause(api, dev_id))
        await until(lambda: link_to(radio.address).actions)
        daemon.on_lan(
            "P._orbit._tcp.local.",
            {"id": dev_id, "name": "P", "platform": "android", "addresses": [("127.0.0.1", lan.port)]},
        )
        try:
            result = await asyncio.wait_for(click, 5)
        except ApiError as e:
            result = (e.code, e.message)
        await until(lambda: daemon.devices[dev_id].session and daemon.devices[dev_id].session.transport == "lan")
    radio.stop()
    print(f"result {result}; BLE actions {link_to(radio.address).actions}; LAN actions {lan_actions}")
    assert result == {"ok": True}  # the phone applied it and answered; nothing was lost
    assert lan_actions == []


# ---- the laptop's Bluetooth adapter ---------------------------------------------------------------


@pytest.fixture
def adapter(monkeypatch):
    from ble_radio import Adapter

    a = Adapter()
    monkeypatch.setattr(bleak, "BleakScanner", a.Scanner)
    return a


async def _ble_status(api):
    return (await api.call("status"))["ble"]


@pytest.mark.parametrize("off_for", [5, 40])
async def test_adapter_power_cycle_while_connected_recovers(ble_phone, adapter, off_for):  # noqa: F811
    """Bluetooth switched off and on in the menu bar (or the adapter reset by suspend/resume).

    Then, 35 s later, the link has a hiccup: the daemon must still be able to reconnect.
    """
    from ble_radio import AdapterRadio

    daemon, api, dev_id = await ble_phone(ble=True, **DEFAULTS)
    radio = AdapterRadio(daemon, dev_id, adapter)
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=10)
    loop = asyncio.get_running_loop()
    adapter.power_off()
    await asyncio.sleep(off_for)
    status_while_off = await _ble_status(api)
    t0 = loop.time()
    adapter.power_on()
    latency = hiccup = None
    with contextlib.suppress(AssertionError):
        await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=45)
        latency = loop.time() - t0
        await asyncio.sleep(35)
        link_to(radio.address).drop()
        t1 = loop.time()
        await asyncio.sleep(0.1)
        await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=20)
        hiccup = loop.time() - t1
    dev = (await api.call("devices.list"))[0]
    print(
        f"off {off_for} s: status while off {status_while_off!r}; reconnected after power on: {latency}; "
        f"after a later hiccup: {hiccup}; scanner starts {adapter.starts}; device {dev['state']} "
        f"discovered {dev['discovered']}; ble status {await _ble_status(api)!r}"
    )
    radio.stop()
    assert status_while_off == "unavailable"
    assert latency is not None and latency < 35
    assert hiccup is not None and hiccup < 10


async def test_bluetooth_off_at_startup_then_on(ble_phone, adapter, caplog):  # noqa: F811
    from ble_radio import AdapterRadio

    adapter.powered = False
    caplog.set_level("INFO", "orbit")
    daemon, api, dev_id = await ble_phone(ble=True, **DEFAULTS)
    radio = AdapterRadio(daemon, dev_id, adapter)
    loop = asyncio.get_running_loop()
    await until(lambda: _is(api, "unavailable"))
    await asyncio.sleep(35)
    t0 = loop.time()
    adapter.power_on()
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=40)
    latency = loop.time() - t0
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "BLE discovery unavailable" in r.getMessage()]
    print(f"connected {latency:.1f} s after Bluetooth came on; {adapter.starts} scan starts, {len(warnings)} warnings")
    radio.stop()
    assert latency < 32  # spec: "unavailable" is retried with backoff up to every 30 s


async def _is(api, state):
    return await _ble_status(api) == state


# ---- every exception bleak raises, at every step ---------------------------------------------------


def _bleak_exceptions():
    from bleak.exc import (
        BleakBluetoothNotAvailableError,
        BleakBluetoothNotAvailableReason,
        BleakCharacteristicNotFoundError,
        BleakDBusError,
        BleakDeviceNotFoundError,
        BleakError,
        BleakGATTProtocolError,
    )

    from orbit.daemon.ble import RX_UUID

    return {
        "disconnected": BleakError("disconnected"),
        "not-connected": BleakError("Not connected"),
        "no-discovery": BleakError("Service Discovery has not been performed yet"),
        "cb-failed-connect": BleakError("failed to connect: Error Domain=CBErrorDomain Code=14"),
        "bluez-discovery": BleakError("failed to discover services, device disconnected"),
        "dbus-abort": BleakDBusError("org.bluez.Error.Failed", ["le-connection-abort-by-local"]),
        "dbus-inprogress": BleakDBusError("org.bluez.Error.InProgress", ["In Progress"]),
        "dbus-unknown-object": BleakDBusError("org.freedesktop.DBus.Error.UnknownObject", []),
        "dbus-notready": BleakDBusError("org.bluez.Error.NotReady", ["Resource Not Ready"]),
        "device-not-found": BleakDeviceNotFoundError("ADDR-1", "Device with address ADDR-1 was not found"),
        "bt-off": BleakBluetoothNotAvailableError(
            "Bluetooth device is turned off", BleakBluetoothNotAvailableReason.POWERED_OFF
        ),
        "att-error": BleakGATTProtocolError(0x0E),
        "char-not-found": BleakCharacteristicNotFoundError(RX_UUID),
        "timeout": TimeoutError(),
        "notify-started": ValueError("Characteristic notifications already started"),
        "assert": AssertionError(),
        "fd-eof": RuntimeError("Unexpected EOF on notification file handle"),
        "dbus-eof": EOFError(),
        "dbus-reset": ConnectionResetError(54, "Connection reset by peer"),
        "not-implemented": NotImplementedError("Pairing is not available in Core Bluetooth."),
    }


class FaultyGatt(FakeGattLink):
    """Raises ``faults[step]`` once at that step: connect, start_notify, write (handshake), action, disconnect."""

    faults: dict = {}

    def _fault(self, step):
        if step in self.faults:
            raise self.faults.pop(step)

    async def connect(self):
        self._fault("connect")
        await super().connect()

    async def start_notify(self, uuid, callback):
        self._fault("start_notify")
        await super().start_notify(uuid, callback)

    async def write_gatt_char(self, uuid, data, response=None):
        self._fault("write" if not getattr(self, "_notify", None) or not self.actions_armed else "action")
        await super().write_gatt_char(uuid, data, response)

    async def disconnect(self):
        if "disconnect" in self.faults:
            self.drop()
        self._fault("disconnect")
        await super().disconnect()

    actions_armed = False


@pytest.mark.parametrize("step", ["connect", "start_notify", "write", "action", "disconnect"])
@pytest.mark.parametrize("name", list(_bleak_exceptions()))
async def test_no_bleak_exception_escapes_or_wedges_reconnecting(ble_phone, monkeypatch, step, name, caplog):  # noqa: F811
    monkeypatch.setattr(bleak, "BleakClient", FaultyGatt)
    monkeypatch.setattr(FaultyGatt, "actions_armed", False)
    loop = asyncio.get_running_loop()
    unhandled = []
    loop.set_exception_handler(lambda _l, ctx: unhandled.append(ctx.get("message")))
    caplog.set_level("INFO", "orbit")
    exc = _bleak_exceptions()[name]
    FaultyGatt.faults = {} if step in ("action", "disconnect") else {step: exc}
    daemon, api, dev_id = await ble_phone(**DEFAULTS)
    radio = Radio(daemon, dev_id)
    try:
        await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=8)
        if step in ("action", "disconnect"):
            FaultyGatt.actions_armed = True
            FaultyGatt.faults = {step: exc}
            if step == "action":
                with contextlib.suppress(ApiError):
                    await asyncio.wait_for(pause(api, dev_id), 13)
            else:
                daemon.devices[dev_id].session.channel.close()  # any local close disconnects
            await asyncio.sleep(0.1)
            await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=8)
        assert (await api.call("devices.list"))[0]["state"] == "connected"
        assert len(daemon.sessions) == 1
        assert sum(1 for i in FakeGattLink.instances if i.connected) == 1
    finally:
        radio.stop()
        import gc

        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(None)
    errors = [r.getMessage() for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
    assert errors == [] and unhandled == []


# ---- crowded air ----------------------------------------------------------------------------------


@pytest.mark.parametrize("orbit_per_s", [0, 5, 30])
async def test_crowded_scan_costs_little_cpu(make_daemon, api_for, orbit_per_s):
    """500 unrelated advertisers (10 adverts/s each, no Orbit service data) plus ``orbit_per_s`` new
    'Orbit' prefixes per second (strangers' phones rotating addresses, or a spoofer) for 20 s, with
    the tray applet subscribed."""
    import secrets

    from orbit.daemon import discovery

    daemon = await make_daemon()
    api = await api_for(daemon)
    await api.call("subscribe")
    scanner = discovery.BleDiscovery(daemon.on_ble)
    unrelated = SimpleNamespace(service_data={"0000fe9f-0000-1000-8000-00805f9b34fb": b"\x01" * 20}, local_name=None)
    loop = asyncio.get_running_loop()
    events = 0
    biggest = 0

    async def drain():
        nonlocal events, biggest
        async for event, data in api.events():
            if event == "devices.changed":
                events += 1
                biggest = max(biggest, len(data["devices"]))

    reader = asyncio.ensure_future(drain())
    cpu0, t0 = time.process_time(), loop.time()
    ticks = 0
    while loop.time() - t0 < 20:
        for i in range(250):  # every 50 ms
            scanner._detected(SimpleNamespace(address=f"U{(ticks * 250 + i) % 500}"), unrelated)
        for i in range(orbit_per_s if ticks % 20 == 0 else 0):
            prefix = secrets.token_bytes(8)
            adv = SimpleNamespace(service_data={ble_uuid(): prefix}, local_name=None)
            scanner._detected(SimpleNamespace(address=f"R{ticks}-{i}"), adv)
        ticks += 1
        await asyncio.sleep(0.05)
    cpu = time.process_time() - cpu0
    reader.cancel()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024  # bytes on macOS
    print(
        f"cpu {cpu:.1f} s over 20 s ({cpu / 20:.0%}); {len(daemon.devices)} devices listed; "
        f"{events} devices.changed events, largest {biggest} devices; peak rss {rss} kB"
    )
    assert cpu / 20 < 0.10


def ble_uuid():
    from orbit.daemon.ble import SERVICE_UUID

    return SERVICE_UUID


# ---- what outlives a session ------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["phone-drop", "invalidated", "stall", "rotation"])
async def test_reconnect_cycles_leak_nothing(ble_phone, how, monkeypatch):  # noqa: F811
    import gc

    from orbit.daemon import ble

    daemon, api, dev_id = await ble_phone(send_timeout=0.2, **{**DEFAULTS, "ble_link_timeout": 0.3})
    radio = Radio(daemon, dev_id, adv_interval=0.05)
    await until(lambda: connected_via(daemon, dev_id, radio.address))
    baseline = None
    for cycle in range(60):
        session = daemon.devices[dev_id].session
        link = link_to(radio.address)
        if how == "phone-drop":
            link.drop()
        elif how == "invalidated":
            ble.services_invalidated(link.address, [ble.SERVICE_UUID])
        elif how == "stall":
            link.acks = False
            with contextlib.suppress(ApiError):
                await pause(api, dev_id)
        else:
            radio.rotate(f"ADDR-R{cycle}")
            FakeGattLink.gone = {link.address}
            link.drop()
        await until(lambda session=session: session.closed.is_set())
        await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=10)
        if cycle == 9:
            gc.collect()
            baseline = (len(asyncio.all_tasks()), len(gc.get_objects()))
    gc.collect()
    tasks, objects = len(asyncio.all_tasks()), len(gc.get_objects())
    links = sum(1 for o in gc.get_objects() if isinstance(o, ble.BleLink))
    dev = daemon.devices[dev_id]
    print(
        f"{how}: tasks {baseline[0]} -> {tasks}, gc objects {baseline[1]} -> {objects}, "
        f"live BleLinks {links}, listeners {len(ble._invalidation_listeners)}, "
        f"addresses kept {len(dev.ble)}, sessions {len(daemon.sessions)}, "
        f"connected fakes {sum(1 for i in FakeGattLink.instances if i.connected)}"
    )
    # The fakes keep themselves (and so each BleLink's closures) in FakeGattLink.instances.
    current = link_to(radio.address)
    FakeGattLink.instances[:] = [current]
    gc.collect()
    held = sum(1 for o in gc.get_objects() if isinstance(o, ble.BleLink))
    print(f"  BleLinks still alive without the fakes' list: {held}")
    assert held <= 2
    radio.stop()
    assert tasks <= baseline[0] + 2
    assert len(daemon.sessions) == 1 and len(ble._invalidation_listeners) <= 1
    assert sum(1 for i in FakeGattLink.instances if i.connected) == 1


# ---- address rotation, advertisement contents ----------------------------------------------------


async def test_rotation_while_connected_then_drop_reconnects_to_the_new_address(ble_phone):  # noqa: F811
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    session = daemon.devices[dev_id].session
    radio.rotate("ADDR-2")  # the old address stops being reachable at once
    await asyncio.sleep(1)
    assert daemon.devices[dev_id].session is session  # a new address alone does not disturb the session
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    link_to("ADDR-1").drop()
    await until(lambda: connected_via(daemon, dev_id, "ADDR-2"), timeout=20)
    latency = loop.time() - t0
    print(
        f"reconnected to the rotated address after {latency:.1f} s; tried {[i.address for i in FakeGattLink.instances]}"
    )
    radio.stop()
    assert latency < 2.0


def test_advertisement_without_service_data_then_with():
    """Android puts the id prefix in the scan response: the first report may only carry the UUID."""
    from orbit.daemon import discovery
    from orbit.daemon.ble import SERVICE_UUID

    seen = []
    scanner = discovery.BleDiscovery(lambda *a: seen.append(a))
    dev = SimpleNamespace(address="A")
    scanner._detected(dev, SimpleNamespace(service_data={}, local_name=None))
    scanner._detected(dev, SimpleNamespace(service_data={SERVICE_UUID: b"\x01\x02\x03"}, local_name=None))
    assert seen == []
    scanner._detected(dev, SimpleNamespace(service_data={SERVICE_UUID: bytes(range(10))}, local_name="Px"))
    assert seen == [("0001020304050607", dev, "Px")]


# ---- stalls with orbitd's default timings ----------------------------------------------------------


async def test_unacknowledged_write_answers_clicks_within_the_spec_bound(ble_phone):  # noqa: F811
    """ATT write with response never acknowledged (bleak would wait 30 s for the ATT timeout)."""
    daemon, api, dev_id, radio = await connected_phone(ble_phone, send_timeout=5.0)
    loop = asyncio.get_running_loop()
    link_to(radio.address).acks = False
    t0 = loop.time()
    first = asyncio.ensure_future(pause(api, dev_id))
    await asyncio.sleep(0.5)
    second = asyncio.ensure_future(pause(api, dev_id))
    results = []
    for click in (first, second):
        try:
            results.append((await click, round(loop.time() - t0, 1)))
        except ApiError as e:
            results.append(((e.code, e.message), round(loop.time() - t0, 1)))
    print(f"clicks: {results}; actions applied {[i.actions for i in FakeGattLink.instances]}")
    radio.stop()
    assert all(t < 12 for _, t in results)
    assert results[1][0] == {"ok": True}


async def test_notifications_stop_mid_frame_with_default_timings(ble_phone):  # noqa: F811
    daemon, api, dev_id, radio = await connected_phone(ble_phone)
    loop = asyncio.get_running_loop()
    session = daemon.devices[dev_id].session
    link_to(radio.address).notify_budget = 1
    t0 = loop.time()
    try:
        result = await pause(api, dev_id)
    except ApiError as e:
        result = (e.code, e.message)
    answered = loop.time() - t0
    await until(lambda: connected_via(daemon, dev_id, radio.address) and daemon.devices[dev_id].session is not session)
    back = loop.time() - t0
    print(
        f"click answered after {answered:.1f} s: {result}; session closed: {session.close_reason!r}; "
        f"back after {back:.1f} s"
    )
    radio.stop()
    assert answered < 12 and back < 15


# ---- LAN gone, BLE still there ----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["closed", "silent"])
async def test_wifi_drop_falls_back_to_ble_quickly(ble_phone, how):  # noqa: F811
    """On LAN with BLE also advertising; the phone leaves Wi-Fi. mDNS still lists its addresses
    (records outlive the phone by their TTL), which now black-hole. "closed": the laptop sees the
    TCP connection end; "silent": it does not (the phone's interface just went away)."""
    from daemon_helpers import RawPhone
    from test_ble import SNAPSHOT

    from orbit.daemon.timeconv import now_ms

    mute = asyncio.Event()

    async def lan_phone(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
        while True:
            msg = await ch.recv()
            if msg["t"] == "ping" and not mute.is_set():
                await ch.send({"t": "pong", "req": msg["req"]})

    daemon, api, dev_id = await ble_phone(**DEFAULTS)
    loop = asyncio.get_running_loop()
    async with RawPhone(lan_phone, identity=FakeGattLink.phone_identity) as lan:
        daemon.on_lan(
            "P._orbit._tcp.local.",
            {"id": dev_id, "name": "P", "platform": "android", "addresses": [("127.0.0.1", lan.port)]},
        )
        await until(lambda: connected_via(daemon, dev_id, f"127.0.0.1:{lan.port}"))
        radio = Radio(daemon, dev_id)
        await asyncio.sleep(1)
        log = StateLog(api, dev_id)
        await asyncio.sleep(0.2)
        log.mark()
        daemon.devices[dev_id].lan["P._orbit._tcp.local."].addresses = [("10.255.255.1", 47820), ("fd00::dead", 47820)]
        t0 = loop.time()
        if how == "silent":
            mute.set()
            click = asyncio.ensure_future(pause(api, dev_id))  # clicked while the LAN link is silently dead
            await until(lambda: daemon.devices[dev_id].session is None, timeout=40)
    lost = loop.time() - t0
    if how == "closed":
        click = asyncio.ensure_future(pause(api, dev_id))
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=60)
    on_ble = loop.time() - t0
    try:
        result = await click
    except ApiError as e:
        result = (e.code, e.message)
    print(f"{how}: LAN lost after {lost:.1f} s, on BLE after {on_ble:.1f} s; click: {result}; states {log.states}")
    radio.stop()
    log.stop()
    assert on_ble < 10 and result == {"ok": True}


async def test_bluetooth_permission_denied(ble_phone, adapter, caplog):  # noqa: F811
    adapter.powered, adapter.reason = False, "DENIED_BY_USER"
    caplog.set_level("INFO", "orbit")
    daemon, api, dev_id = await ble_phone(ble=True, **DEFAULTS)
    await until(lambda: _is(api, "unavailable"))
    status = await api.call("status")
    await asyncio.sleep(62)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    print(f"status {status}; {adapter.starts} scan starts and {len(warnings)} warnings in 62 s")
    assert "denied" in status["bleError"]
