"""orbitd failure paths: reconnect backoff, transport errors, oversize frames, yielding transports, art bounds."""

import asyncio
import base64
import hashlib

import pytest
from conftest import RawPhone, pin_in, until

import orbit.daemon.core as core
from orbit.client import ApiError
from orbit.daemon.timeconv import now_ms
from orbit.proto import FrameStream, KeyPair, device_id


async def ready(ch, paired=True):
    await ch.send({"t": "ready", "paired": paired, "now": now_ms()})
    msg = await ch.recv()
    assert msg["t"] == "ready"


class SlowWriter:
    """Writer whose drain yields like a BLE write-with-response (the bytes are already on their way)."""

    def __init__(self, writer, delay=0.05):
        self.writer, self.delay = writer, delay
        self.closed = False

    def write(self, data):
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()
        await asyncio.sleep(self.delay)

    def close(self):
        self.closed = True
        self.writer.close()


@pytest.fixture
def slow_transport(monkeypatch):
    async def open_slow(host, port):
        reader, writer = await asyncio.open_connection(host, port)
        return FrameStream(reader, SlowWriter(writer))

    monkeypatch.setattr(core, "open_tcp", open_slow)


async def test_phone_that_drops_us_at_once_gets_backoff_not_a_hot_loop(make_daemon, short_tmp):
    connections = []

    async def script(ch):
        connections.append(asyncio.get_running_loop().time())
        await ready(ch)
        await ch.send({"t": "bye", "reason": "busy"})
        await asyncio.sleep(0.05)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        await make_daemon()
        await asyncio.sleep(2.5)
    # Backoff 1 s then 2 s: connections at ~0 s and ~1 s, the next one only at ~3 s.
    assert len(connections) == 2, connections
    assert connections[1] - connections[0] >= 0.9


async def test_non_oserror_transport_failure_closes_stream_and_connector_retries(make_daemon, short_tmp, monkeypatch):
    class TransportError(Exception):  # like bleak.exc.BleakError: not an OSError
        pass

    writers = []

    class FailingWriter(SlowWriter):
        async def drain(self):
            raise TransportError("Not connected")

    async def open_flaky(host, port):
        reader, writer = await asyncio.open_connection(host, port)
        w = FailingWriter(writer) if not writers else writer
        writers.append(w)
        return FrameStream(reader, w)

    monkeypatch.setattr(core, "open_tcp", open_flaky)

    async def script(ch):
        await ready(ch)
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: daemon.devices[phone.id].session is not None, timeout=4)
        assert len(writers) == 2 and writers[0].closed
        assert not daemon.devices[phone.id].connector.done()


async def test_oversize_action_is_bad_request_and_session_survives(make_daemon, short_tmp, api_for):
    seen = []

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "a1", "kind": "generic"}]})
        while True:
            msg = await ch.recv()
            seen.append(msg["t"])
            if msg["t"] == "action":
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].activities)
        params = {"device": phone.id, "activity": "a1", "action": "notif.0"}
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {**params, "value": "x" * (1 << 20)})
        assert e.value.code == "bad_request"
        assert await api.call("activities.action", {**params, "value": "small"}) == {"ok": True}
        assert seen.count("action") == 1


async def test_snapshot_right_after_our_late_accept_is_kept(make_daemon, api_for, slow_transport):
    replies = []

    async def script(ch):
        await ready(ch, paired=False)
        assert (await ch.recv())["t"] == "pair_request"
        await ch.send({"t": "pair_decision", "accepted": True})
        assert (await ch.recv())["t"] == "pair_decision"
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "a1", "kind": "generic"}]})
        while True:
            replies.append((await ch.recv())["t"])

    async with RawPhone(script) as phone:
        daemon = await make_daemon()
        api = await api_for(daemon)
        await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port})
        (pairing,) = await until(lambda: [p for p in daemon.pairings_json() if p["remoteAccepted"]])
        await api.call("pairing.respond", {"session": pairing["session"], "accept": True})
        assert [a["id"] for a in await until(lambda: api.call("activities.list"))] == ["a1"]
        await asyncio.sleep(0.2)
        assert "error" not in replies


async def test_forget_while_phone_closes_on_unpair(make_daemon, short_tmp, api_for, slow_transport):
    art = b"img"
    key = hashlib.sha256(art).hexdigest()[:16]

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "art", "key": key, "mime": "image/png", "data": base64.b64encode(art).decode()})
        while (await ch.recv())["t"] != "unpair":
            pass
        ch.close()
        return "unpaired"

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].art)
        assert await api.call("devices.forget", {"id": phone.id}) is True
        assert phone.results == ["unpaired"]
        assert daemon.store.get(phone.id) is None and phone.id not in daemon.devices


async def test_forget_mid_handshake_closes_the_transport(make_daemon, short_tmp, api_for):
    accepted, eof = asyncio.Event(), asyncio.Event()

    async def silent_phone(reader, writer):
        accepted.set()
        while await reader.read(100):
            pass
        eof.set()

    server = await asyncio.start_server(silent_phone, "127.0.0.1", 0)
    try:
        identity = KeyPair.generate()
        pin_in(short_tmp / "daemon", identity, address=f"127.0.0.1:{server.sockets[0].getsockname()[1]}")
        daemon = await make_daemon(connect_timeout=30)
        api = await api_for(daemon)
        await asyncio.wait_for(accepted.wait(), 3)
        await api.call("devices.forget", {"id": device_id(identity.public)})
        await asyncio.wait_for(eof.wait(), 3)
    finally:
        server.close()


async def test_connect_after_disconnect_resumes_auto_reconnect(make_daemon, short_tmp, api_for):
    sessions = []

    async def script(ch):
        sessions.append(ch)
        await ready(ch)
        while True:
            msg = await ch.recv()
            if msg["t"] == "ping":
                await ch.send({"t": "pong", "req": msg["req"]})

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].session)
        await api.call("devices.disconnect", {"id": phone.id})
        await api.call("devices.connect", {"id": phone.id})
        assert len(sessions) == 2
        sessions[-1].close()  # the phone drops the user-initiated session
        await until(lambda: len(sessions) == 3 and daemon.devices[phone.id].session, timeout=4)


async def test_art_referenced_by_live_activity_is_never_evicted(make_daemon, short_tmp, monkeypatch):
    monkeypatch.setattr(core, "ART_MAX_ENTRIES", 2)
    images = [bytes([i]) * 10 for i in range(4)]
    keys = [hashlib.sha256(img).hexdigest()[:16] for img in images]

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "art", "key": keys[0], "mime": "image/png", "data": base64.b64encode(images[0]).decode()})
        activity = {"id": "a", "kind": "media", "iconKey": keys[0], "actions": []}
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [activity]})
        for img, key in zip(images[1:], keys[1:], strict=True):
            await ch.send({"t": "art", "key": key, "mime": "image/png", "data": base64.b64encode(img).decode()})
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: keys[3] in daemon.devices[phone.id].art)
        assert list(daemon.devices[phone.id].art) == [keys[0], keys[3]]


async def test_crashed_connector_is_restarted(make_daemon, short_tmp, monkeypatch):
    monkeypatch.setattr(core, "CONNECTOR_RESTART_DELAY", 0.1)
    real_routes, calls = core.Daemon.routes, []

    def routes(self, dev):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return real_routes(self, dev)

    monkeypatch.setattr(core.Daemon, "routes", routes)

    async def script(ch):
        await ready(ch)
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: daemon.devices[phone.id].session is not None, timeout=3)
        assert len(calls) >= 2
