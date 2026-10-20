"""BLE sessions over an L2CAP channel (spec/PROTOCOL.md 1.2.1), the fallbacks to GATT and the channel's end.

The phone's side of a channel is a loopback TCP connection; on macOS the laptop's side is a real
CFStream pair over it, driven by orbitd's own stream pump as a ``CBL2CAPChannel``'s streams would be.
"""

import asyncio
import os
import re
import socket
import sys
import threading
import time
from types import SimpleNamespace

import bleak
import pytest
from daemon_helpers import until
from test_ble import (  # noqa: F401  (fixtures)
    SNAPSHOT,
    FakeGattLink,
    ble_phone,
    connected_via,
    gatt,
    link_to,
    phone_session,
)

from orbit.client import ApiError
from orbit.daemon import ble
from orbit.daemon.ble import PSM_UUID, TX_UUID, BleLink
from orbit.daemon.session import LinkDown, WatchedWriter
from orbit.daemon.timeconv import now_ms
from orbit.proto import FrameStream, respond

PSM = 0x81
darwin = pytest.mark.skipif(sys.platform != "darwin", reason="drives real NSStreams")


def tcp_pair():
    server = socket.create_server(("127.0.0.1", 0))
    with server:
        laptop = socket.create_connection(server.getsockname())
        phone, _ = server.accept()
    return laptop, phone


class FakeChannel:
    """Stands in for ``CBL2CAPChannel``: CFStreams over the laptop's end of a TCP connection."""

    def __init__(self, laptop: socket.socket) -> None:
        import CoreFoundation as CF

        self._in, self._out = CF.CFStreamCreatePairWithSocket(None, laptop.detach(), None, None)
        for s in (self._in, self._out):  # closing the streams closes the channel, as in CoreBluetooth
            s.setProperty_forKey_(True, CF.kCFStreamPropertyShouldCloseNativeSocket)

    def inputStream(self):
        return self._in

    def outputStream(self):
        return self._out


class CountingIO:
    """The phone's end of the channel as a FrameStream reader and writer, counting bytes both ways."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.got = self.sent = 0

    async def readexactly(self, n):
        data = await self.reader.readexactly(n)
        self.got += n
        return data

    def write(self, data):
        self.sent += len(data)
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()

    def close(self):
        self.writer.close()


class FakeChannelLink(FakeGattLink):
    """A phone that also offers an L2CAP channel (PSM characteristic reads ``psm``).

    ``answer`` to ``openL2CAPChannel_``, after ``answer_delay`` s on another thread (CoreBluetooth's
    queue): "channel" (macOS only), "error", "silent" (never), or "late object" (a channel no one may use).
    """

    psm = PSM.to_bytes(2, "little")
    answer = "channel"
    answer_delay = 0.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.phone_io = None
        self.channel_task = None

    def open_l2cap(self, psm):
        super().open_l2cap(psm)
        loop = asyncio.get_running_loop()
        if self.answer == "silent":
            return
        if self.answer == "error":
            result = (None, SimpleNamespace(localizedDescription=lambda: "Connection refused"))
        elif self.answer == "late object":
            result = (object(), None)
        else:
            laptop, phone = tcp_pair()
            result = (FakeChannel(laptop), None)
            self.channel_task = asyncio.ensure_future(self._channel_phone(phone))

        def callback():
            time.sleep(self.answer_delay)
            loop.call_soon_threadsafe(ble.channel_opened, self.address, *result)

        threading.Thread(target=callback).start()

    async def _channel_phone(self, sock):
        reader, writer = await asyncio.open_connection(sock=sock)
        self.phone_io = io = CountingIO(reader, writer)
        identity = self.identities.get(self.address) or self.phone_identity
        try:
            ch = await respond(FrameStream(io, io), identity, "BLE Phone", "android")
            await (self.script or phone_session)(ch, self)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the laptop closed the channel
        finally:
            writer.close()


@pytest.fixture
def l2cap(ble_phone, monkeypatch):  # noqa: F811
    """``ble_phone`` with phones that offer an L2CAP channel; CoreBluetooth's code path on any OS."""
    monkeypatch.setattr(bleak, "BleakClient", FakeChannelLink)
    monkeypatch.setattr(ble, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.delenv(ble.TRANSPORT_ENV, raising=False)
    for name, value in (("psm", FakeChannelLink.psm), ("answer", "channel"), ("answer_delay", 0.0)):
        monkeypatch.setattr(FakeChannelLink, name, value)
    return ble_phone


def pump_threads():
    return [t for t in threading.enumerate() if t.name.startswith("orbit-l2cap-")]


def ble_link(daemon, dev_id) -> BleLink:
    return daemon.devices[dev_id].session.channel.stream.writer._writer


def without_writes(log):
    return [e for e in log if not (isinstance(e, tuple) and e[0] == "write")]


# ---- choosing the transport ----------------------------------------------------------------------


@darwin
async def test_session_runs_over_the_l2cap_channel(l2cap, api_for, caplog):
    caplog.set_level("INFO", "orbit")
    daemon, api, dev_id = await l2cap()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    assert [a["id"] for a in await api.call("activities.list")] == [a["id"] for a in SNAPSHOT]
    phone = link_to("A1")
    # Neither TX notifications nor RX writes: the whole session is on the channel.
    assert phone.log == ["connect", ("read", PSM_UUID), ("open l2cap", PSM)]
    assert "BLE link to A1, MTU 23, via L2CAP PSM 129" in caplog.text

    params = {"device": dev_id, "activity": "media:x", "action": "media.pause"}
    assert await api.call("activities.action", params) == {"ok": True}
    assert phone.actions == ["media.pause"]
    assert (await api.call("devices.ping", {"id": dev_id}))["rttMs"] > 0
    link = ble_link(daemon, dev_id)
    await until(lambda: (link.received, link.handed) == (phone.phone_io.sent, phone.phone_io.got))
    assert WatchedWriter(link).delivered() == link.handed > 0
    (pump,) = pump_threads()
    assert pump.name == "orbit-l2cap-A1"

    # Closing locally closes the channel (the phone reads its end), stops the pump and disconnects.
    await api.call("devices.disconnect", {"id": dev_id})
    await asyncio.wait_for(phone.channel_task, 1)
    await until(lambda: not pump_threads())
    assert phone.log[-1] == "disconnect" and not phone.connected
    (line,) = [r.getMessage() for r in caplog.records if " ended after " in r.getMessage()]
    m = re.fullmatch(
        r"BLE link to A1 via L2CAP PSM 129 ended after [\d.]+ s: (\d+) bytes in, (\d+) bytes out \(.*\)", line
    )
    assert m and (int(m[1]), int(m[2])) == (phone.phone_io.sent, link.handed)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("no characteristic", "the phone offers no L2CAP channel"),
        ("read error", "reading the PSM failed: Unlikely error"),
        ("bad value", "bad PSM value 81"),
        ("open error", "opening L2CAP PSM 129 failed: Connection refused"),
        ("no answer", "opening L2CAP PSM 129 took over 0.3 s"),
        ("read hangs", "reading the PSM took over 0.3 s"),
        ("forced", "ORBIT_BLE_TRANSPORT=gatt"),
    ],
)
async def test_gatt_on_the_same_connection_when_there_is_no_channel(l2cap, monkeypatch, caplog, case, reason):
    caplog.set_level("INFO", "orbit")
    monkeypatch.setattr(ble, "L2CAP_OPEN_TIMEOUT", 0.3)
    match case:
        case "no characteristic":
            monkeypatch.setattr(FakeChannelLink, "psm", None)
        case "read error":
            monkeypatch.setattr(FakeChannelLink, "psm", bleak.exc.BleakError("Unlikely error"))
        case "bad value":
            monkeypatch.setattr(FakeChannelLink, "psm", b"\x81")
        case "open error":
            monkeypatch.setattr(FakeChannelLink, "answer", "error")
        case "no answer":
            monkeypatch.setattr(FakeChannelLink, "answer", "silent")
        case "read hangs":
            monkeypatch.setattr(FakeChannelLink, "read_gatt_char", lambda self, char: asyncio.Event().wait())
        case "forced":
            monkeypatch.setenv(ble.TRANSPORT_ENV, " GATT ")
    daemon, api, dev_id = await l2cap()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    (phone,) = FakeGattLink.instances  # the same connection
    tried = {
        "no characteristic": [],
        "read hangs": [],
        "forced": [],
        "read error": [("read", PSM_UUID)],
        "bad value": [("read", PSM_UUID)],
    }.get(case, [("read", PSM_UUID), ("open l2cap", PSM)])
    assert without_writes(phone.log) == ["connect", *tried, ("notify", TX_UUID)]
    assert f"BLE link to A1, MTU 23, via GATT ({reason})" in caplog.text
    assert not pump_threads() and not ble._channel_waiters


async def test_bluez_stays_on_gatt(l2cap, monkeypatch, caplog):
    caplog.set_level("INFO", "orbit")
    monkeypatch.setattr(ble, "sys", SimpleNamespace(platform="linux"))
    daemon, api, dev_id = await l2cap()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    assert without_writes(link_to("A1").log) == ["connect", ("notify", TX_UUID)]
    assert "via GATT (L2CAP channels are implemented for CoreBluetooth only)" in caplog.text


async def test_forced_l2cap_does_not_fall_back(l2cap, monkeypatch):
    monkeypatch.setenv(ble.TRANSPORT_ENV, "l2cap")
    monkeypatch.setattr(FakeChannelLink, "psm", None)
    daemon, api, dev_id = await l2cap()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"id": dev_id})
    assert "no L2CAP channel (ORBIT_BLE_TRANSPORT=l2cap): the phone offers no L2CAP channel" in e.value.message
    assert FakeGattLink.instances
    assert all(i.log == ["connect", "disconnect"] for i in FakeGattLink.instances)


@pytest.mark.parametrize("loss", ["peripheral disconnected", "services invalidated"])
async def test_link_lost_while_the_channel_opens_fails_at_once(l2cap, monkeypatch, loss):
    await l2cap()
    monkeypatch.setattr(FakeChannelLink, "answer", "silent")
    opening = asyncio.ensure_future(ble.open_ble(SimpleNamespace(address="A1"), 1.0))
    await until(lambda: link_to("A1") and ("open l2cap", PSM) in link_to("A1").log)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    if loss == "peripheral disconnected":
        link_to("A1").drop()
    else:
        ble.services_invalidated("A1", [ble.SERVICE_UUID])
    with pytest.raises(LinkDown) as e:
        await asyncio.wait_for(opening, 1)  # not after L2CAP_OPEN_TIMEOUT, and not over GATT
    assert loss.split()[1] in str(e.value) and loop.time() - t0 < 0.2
    assert ("notify", TX_UUID) not in link_to("A1").log and not link_to("A1").connected
    assert not ble._channel_waiters


async def test_channel_that_opens_too_late_is_dropped(l2cap, monkeypatch, caplog):
    caplog.set_level("DEBUG", "orbit.daemon.ble")
    monkeypatch.setattr(ble, "L2CAP_OPEN_TIMEOUT", 0.2)
    monkeypatch.setattr(FakeChannelLink, "answer", "late object")  # unusable: any use would fail
    monkeypatch.setattr(FakeChannelLink, "answer_delay", 0.4)
    daemon, api, dev_id = await l2cap()
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    session = daemon.devices[dev_id].session
    await until(lambda: "dropping a late L2CAP channel to A1" in caplog.text)
    await asyncio.sleep(0.05)
    assert daemon.devices[dev_id].session is session and not session.closed.is_set()
    assert ble_link(daemon, dev_id).via == "GATT (opening L2CAP PSM 129 took over 0.2 s)"
    assert len(FakeGattLink.instances) == 1 and not pump_threads()


# ---- the end of a channel session ----------------------------------------------------------------


@darwin
@pytest.mark.parametrize(
    "loss,cause",
    [
        ("phone closes the channel", "connection closed by phone"),
        ("peripheral disconnected", "BLE link lost: peripheral disconnected"),
        ("services invalidated", "BLE link lost: phone invalidated the Orbit GATT service"),
        ("stream error", "BLE link lost: L2CAP stream error: unknown error"),
    ],
)
async def test_channel_loss_ends_the_session_at_once(l2cap, loss, cause):
    from Foundation import NSStreamEventErrorOccurred

    daemon, api, dev_id = await l2cap(keepalive=60, dead_after=60)
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    await until(lambda: connected_via(daemon, dev_id, "A1"))
    daemon.devices[dev_id].auto = False  # no reconnecting afterwards
    session, phone, link = daemon.devices[dev_id].session, link_to("A1"), ble_link(daemon, dev_id)
    match loss:
        case "phone closes the channel":
            phone.phone_io.writer.close()
        case "peripheral disconnected":
            phone.drop()
        case "services invalidated":
            ble.services_invalidated("A1", [ble.SERVICE_UUID])
        case "stream error":
            pump = link._pump
            pump._delegate.stream_handleEvent_(pump._in, NSStreamEventErrorOccurred)
    await asyncio.wait_for(session.closed.wait(), 0.5)
    assert cause in session.close_reason
    await asyncio.wait_for(phone.channel_task, 1)  # the phone's end of the channel closed
    await until(lambda: not pump_threads(), timeout=1)
    assert not phone.connected and link._pump._channel is None


@darwin
async def test_what_the_phone_sent_before_closing_the_channel_is_read(l2cap, monkeypatch, caplog):
    caplog.set_level("INFO", "orbit")

    async def leaving_phone(ch, link):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": SNAPSHOT})
        await ch.send({"t": "bye", "reason": "Orbit turned off"})
        link.phone_io.writer.close()

    daemon, api, dev_id = await l2cap()
    monkeypatch.setattr(FakeGattLink, "script", staticmethod(leaving_phone))
    daemon.on_ble(dev_id[:16], SimpleNamespace(address="A1"))
    closed = await until(
        lambda: [r.getMessage() for r in caplog.records if r.getMessage().startswith("session_closed")]
    )
    assert closed[0].endswith("cause='phone said bye (Orbit turned off)'")


@darwin
async def test_pump_writes_only_what_the_stream_accepts_and_keeps_the_order():
    laptop, phone = tcp_pair()
    got, sent, ended = [], [], []
    pump = ble._ChannelPump(
        FakeChannel(laptop), "orbit-l2cap-test", got.append, sent.append, lambda *e: ended.append(e)
    )
    try:
        payload = os.urandom(32 * 1024 * 1024)
        pump.send(payload[:1000])
        pump.send(payload[1000:])
        await asyncio.sleep(0.3)
        assert 0 < sum(sent) < len(payload)  # the phone reads nothing yet: the stream holds the rest back
        reader, writer = await asyncio.open_connection(sock=phone)
        assert await reader.readexactly(len(payload)) == payload
        await until(lambda: sum(sent) == len(payload))
        writer.write(b"pong")
        await until(lambda: b"".join(got) == b"pong")
        writer.close()
        await until(lambda: ended)
        assert ended == [("the phone closed the L2CAP channel", True)]
    finally:
        pump.close()
    await until(lambda: not pump.thread.is_alive(), timeout=2)


@darwin
async def test_pump_stops_at_once_while_the_phone_reads_nothing():
    laptop, phone = tcp_pair()
    sent = []
    pump = ble._ChannelPump(FakeChannel(laptop), "orbit-l2cap-T", lambda _d: None, sent.append, lambda *_e: None)
    pump.send(bytes(32 * 1024 * 1024))
    await until(lambda: sent)
    await asyncio.sleep(0.2)  # the stream is full: the pump waits for space, without blocking its thread
    t0 = time.monotonic()
    pump.close()
    await until(lambda: not pump.thread.is_alive(), timeout=1)
    assert time.monotonic() - t0 < 0.2
    phone.settimeout(5)
    while phone.recv(1 << 20):  # the channel was closed: the phone reads what was accepted, then the end
        pass
    phone.close()


@darwin
async def test_corebluetooth_did_open_l2cap_channel_reaches_the_waiter():
    from bleak.backends.corebluetooth.PeripheralDelegate import ObjcPeripheralDelegate
    from Foundation import NSError, NSLocalizedDescriptionKey

    ble._install_corebluetooth_hook()

    class Peripheral:
        def identifier(self):
            return SimpleNamespace(UUIDString=lambda: "PERIPHERAL-2")

    delegate = ObjcPeripheralDelegate.alloc().init()
    channel = object()
    error = NSError.errorWithDomain_code_userInfo_("CBErrorDomain", 1, {NSLocalizedDescriptionKey: "No resources"})
    loop = asyncio.get_running_loop()
    try:
        for args in ((channel, None), (None, error)):
            waiter = ble._channel_waiters["PERIPHERAL-2"] = loop.create_future()
            # Through the Objective-C runtime, on another thread, as CoreBluetooth calls it.
            call = delegate.peripheral_didOpenL2CAPChannel_error_
            threading.Thread(target=call, args=(Peripheral(), *args)).start()
            if args[1] is None:
                assert await asyncio.wait_for(waiter, 1) is channel
            else:
                with pytest.raises(ConnectionError, match="No resources"):
                    await asyncio.wait_for(waiter, 1)
    finally:
        ble._channel_waiters.pop("PERIPHERAL-2", None)


# ---- the link's writer over a channel ------------------------------------------------------------


class FakePump:
    def __init__(self, channel, name, on_data, on_sent, on_end):
        self.on_data, self.on_sent, self.on_end = on_data, on_sent, on_end
        self.queued = bytearray()
        self.closed = False

    def send(self, data):
        self.queued += data

    def close(self):
        self.closed = True


class Client:
    address = "AA"
    disconnected = False

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
async def channel_link(monkeypatch):
    monkeypatch.setattr(ble, "_ChannelPump", FakePump)
    client, reader = Client(), asyncio.StreamReader()
    link = BleLink(client, reader, 20)
    link.use_channel(object(), PSM)
    return link, link._pump, client, reader


async def test_channel_drain_waits_until_the_stream_accepted_every_byte(channel_link):
    link, pump, _, _ = channel_link
    link.write(b"a" * 60)
    link.write(b"b" * 40)
    drain = asyncio.ensure_future(link.drain())
    await asyncio.sleep(0)
    assert pump.queued == b"a" * 60 + b"b" * 40 and link.handed == 0
    pump.on_sent(70)
    await asyncio.sleep(0)
    assert not drain.done() and WatchedWriter(link).delivered() == 70
    pump.on_sent(30)
    await asyncio.wait_for(drain, 0.1)
    assert link.handed == 100


@pytest.mark.parametrize("accepted", [0, 1])
async def test_channel_loss_during_a_drain_tells_whether_the_frame_may_have_left(channel_link, accepted):
    link, pump, client, reader = channel_link
    link.write(b"x" * 10)
    drain = asyncio.ensure_future(link.drain())
    await asyncio.sleep(0)
    if accepted:
        pump.on_sent(accepted)
    pump.on_end("L2CAP stream error: Broken pipe", False)
    with pytest.raises(LinkDown) as e:
        await asyncio.wait_for(drain, 0.1)
    assert e.value.sent is bool(accepted) and link.handed == accepted
    with pytest.raises(LinkDown) as e:
        await reader.read(1)
    assert "Broken pipe" in str(e.value)
    with pytest.raises(LinkDown) as e:
        link.write(b"y")
    assert e.value.sent is False
    await asyncio.sleep(0)
    assert pump.closed and client.disconnected


async def test_channel_end_delivers_what_arrived_before_it(channel_link):
    link, pump, client, reader = channel_link
    pump.on_data(b"last words")
    pump.on_end("the phone closed the L2CAP channel", True)
    pump.on_data(b"after the end")
    assert await reader.read() == b"last words"  # then EOF, not an error
    assert link.received == 10 and link.lost_cause == "the phone closed the L2CAP channel"
    await asyncio.sleep(0)
    assert pump.closed and client.disconnected


async def test_closing_a_channel_link_stops_its_pump_and_logs_its_traffic(channel_link, caplog):
    caplog.set_level("INFO", "orbit")
    link, pump, client, reader = channel_link
    link.started = time.monotonic() - 2
    pump.on_data(b"12345")
    link.write(b"abc")
    drain = asyncio.ensure_future(link.drain())
    await asyncio.sleep(0)
    pump.on_sent(3)
    await drain
    link.close()
    assert await reader.read() == b"12345"
    await asyncio.sleep(0)
    assert pump.closed and client.disconnected
    assert re.search(
        r"BLE link to AA via L2CAP PSM 129 ended after 2\.\d s: 5 bytes in, 3 bytes out \(closed locally\)", caplog.text
    )
