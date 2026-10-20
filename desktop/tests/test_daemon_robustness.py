"""orbitd failure paths: reconnect backoff, transport errors, oversize frames, yielding transports, art bounds."""

import asyncio
import base64
import hashlib

import pytest
from daemon_helpers import RawPhone, pin_in, until

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


async def test_art_keys_are_listed_once_the_art_arrived(make_daemon, short_tmp, api_for):
    icon, cover = b"icon", b"cover"
    icon_key, cover_key = (hashlib.sha256(img).hexdigest()[:16] for img in (icon, cover))
    send_cover = asyncio.Event()

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "art", "key": icon_key, "mime": "image/png", "data": base64.b64encode(icon).decode()})
        activity = {"id": "a", "kind": "media", "iconKey": icon_key, "artKey": cover_key, "actions": []}
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [activity]})
        await send_cover.wait()
        await ch.send({"t": "art", "key": cover_key, "mime": "image/jpeg", "data": base64.b64encode(cover).decode()})
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        [listed] = await until(lambda: api.call("activities.list"))
        assert listed["iconKey"] == icon_key and "artKey" not in listed
        await api.call("subscribe")
        events = api.events()

        async def next_activities():
            async for event, data in events:
                if event == "activities.changed":
                    return data["activities"]

        assert "artKey" not in (await asyncio.wait_for(next_activities(), 2))[0]
        send_cover.set()
        [late] = await asyncio.wait_for(next_activities(), 2)
        assert late["artKey"] == cover_key
        art = await api.call("art.get", {"device": phone.id, "key": cover_key})
        assert art == {"mime": "image/jpeg", "data": base64.b64encode(cover).decode()}


async def test_previous_art_stands_in_until_a_variant_of_the_same_track_arrives(make_daemon, short_tmp):
    small, full = b"small cover", b"full cover"
    small_key, full_key = (hashlib.sha256(img).hexdigest()[:16] for img in (small, full))
    track = {"id": "a", "kind": "media", "title": "Song", "subtitle": "Artist", "actions": []}
    step = asyncio.Queue()

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "art", "key": small_key, "mime": "image/jpeg", "data": base64.b64encode(small).decode()})
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{**track, "artKey": small_key}]})
        await step.get()
        await ch.send({"t": "upsert", "now": now_ms(), "activity": {**track, "artKey": full_key}})
        await step.get()
        await ch.send({"t": "upsert", "now": now_ms(), "activity": {**track, "title": "Next", "artKey": "f" * 16}})
        await asyncio.sleep(30)

    def listed(**match):
        return [a for a in daemon.activities_json() if all(a.get(k) == v for k, v in match.items())]

    def received(key):
        return any(a.get("artKey") == key for d in daemon.devices.values() for a in d.activities.values())

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: listed(artKey=small_key))
        step.put_nowait(1)
        await until(lambda: received(full_key))
        assert listed(title="Song", artKey=small_key)  # same track: the small variant stays until the full one arrives
        step.put_nowait(1)
        [next_track] = await until(lambda: listed(title="Next"))
        assert "artKey" not in next_track  # a different track never shows the previous cover


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


async def test_unreachable_advertised_addresses_fail_fast_then_next_is_tried(make_daemon, api_for):
    async def script(ch):
        await ready(ch, paired=False)
        await asyncio.sleep(30)

    blackholes = [("192.0.2.1", 47820), ("192.0.2.2", 47820)]  # TEST-NET: never answers
    async with RawPhone(script) as phone:
        daemon = await make_daemon(tcp_timeout=0.3, connect_timeout=5)
        api = await api_for(daemon)
        info = {"id": phone.id, "name": "Emu", "platform": "android", "addresses": blackholes}
        daemon.on_lan("Emu._orbit._tcp.local.", info)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ApiError) as e:
            await api.call("devices.connect", {"id": phone.id})
        assert loop.time() - started < 1.5
        assert "all 2 addresses failed" in e.value.message and "192.0.2.2:47820" in e.value.message

        daemon.on_lan("Emu._orbit._tcp.local.", {**info, "addresses": [*blackholes, ("127.0.0.1", phone.port)]})
        assert await api.call("devices.connect", {"id": phone.id}) == {"id": phone.id}


def art_parts(image: bytes, size: int, key: str | None = None) -> list[dict]:
    key = key or hashlib.sha256(image).hexdigest()[:16]
    chunks = [image[i : i + size] for i in range(0, len(image), size)]
    return [
        {"t": "art", "key": key, "mime": "image/jpeg", "data": base64.b64encode(c).decode(), "part": i, "parts": n}
        for n in [len(chunks)]
        for i, c in enumerate(chunks)
    ]


async def test_art_in_parts_is_reassembled_around_other_messages(make_daemon, short_tmp):
    image = bytes(range(10))
    key = hashlib.sha256(image).hexdigest()[:16]
    laptop_ready = {}

    async def script(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        laptop_ready.update(await ch.recv())
        p = art_parts(image, 4)
        await ch.send(p[0])
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "a", "kind": "media", "artKey": key}]})
        await ch.send(p[1])
        await ch.send({"t": "ping", "req": 1})
        await ch.send(p[2])
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: key in daemon.devices[phone.id].art)
        assert daemon.devices[phone.id].art[key] == ("image/jpeg", image)
        assert laptop_ready["artParts"] is True


IMAGE = bytes(range(10))
P = art_parts(IMAGE, 4)
OTHER = art_parts(b"another image", 4)
ACTIVITY = {"id": "a", "kind": "media", "artKey": P[0]["key"]}


@pytest.mark.parametrize(
    ("frames", "stored"),
    [
        ([P[0], P[2]], False),  # a gap
        ([P[0], OTHER[0], P[1], P[2]], False),  # another image started
        ([P[0], {**P[1], "parts": 4}, P[2]], False),
        ([P[0], {**P[1], "mime": "image/png"}, P[2]], False),
        ([{**P[0], "part": False}, P[1], P[2]], False),
        ([P[0], {**P[1], "part": True}, P[2]], False),
        ([P[0], {**P[1], "part": 1.0}, P[2]], False),
        ([P[0], P[1], P[1], P[2]], False),  # a repeated part
        ([*art_parts(IMAGE, 4, key="0" * 16)], False),  # the key is checked on the whole image
        ([{**p, "parts": 257, "part": p["part"]} for p in art_parts(IMAGE, 4)], False),
        ([P[0], P[1], P[0], P[1], P[2]], True),  # started over
        ([P[0], {"t": "art", "key": "1" * 16, "mime": "image/png", "data": ""}, P[1], P[2]], True),
        ([*art_parts(IMAGE, 10)], True),  # one part
    ],
)
async def test_incomplete_or_inconsistent_art_parts_are_discarded(make_daemon, short_tmp, frames, stored):
    done = b"done"
    done_key = hashlib.sha256(done).hexdigest()[:16]
    key = P[0]["key"]

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [ACTIVITY]})
        for f in frames:
            await ch.send(f)
        await ch.send({"t": "art", "key": done_key, "mime": "image/png", "data": base64.b64encode(done).decode()})
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: done_key in daemon.devices[phone.id].art)
        assert (key in daemon.devices[phone.id].art) is stored


async def test_reassembled_art_is_bounded(make_daemon, short_tmp, monkeypatch):
    from orbit.daemon import session

    monkeypatch.setattr(session, "ART_MAX_ASSEMBLED", 8)
    small = bytes(8)
    small_key = hashlib.sha256(small).hexdigest()[:16]

    async def script(ch):
        await ready(ch)
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [ACTIVITY]})
        for f in P + art_parts(small, 4):
            await ch.send(f)
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: small_key in daemon.devices[phone.id].art)
        assert P[0]["key"] not in daemon.devices[phone.id].art


async def test_art_parts_do_not_continue_across_sessions(make_daemon, short_tmp):
    sessions = []
    done = b"done"
    done_key = hashlib.sha256(done).hexdigest()[:16]

    async def script(ch):
        sessions.append(ch)
        await ready(ch)
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [ACTIVITY]})
        if len(sessions) == 1:
            await ch.send(P[0])
            await ch.send(P[1])
            ch.close()
            return
        await ch.send(P[2])
        await ch.send({"t": "art", "key": done_key, "mime": "image/png", "data": base64.b64encode(done).decode()})
        await asyncio.sleep(30)

    async with RawPhone(script) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: done_key in daemon.devices[phone.id].art, timeout=10)
        assert len(sessions) == 2 and P[0]["key"] not in daemon.devices[phone.id].art
