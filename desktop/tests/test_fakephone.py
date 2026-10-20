import asyncio
import base64
import errno
import hashlib
import socket
import struct
import time

import pytest

from orbit.fakephone import FakePhone
from orbit.fakephone import phone as phone_module
from orbit.fakephone.art import cover
from orbit.proto import FrameStream, KeyPair, device_id, initiate
from orbit.store import PeerStore

ALL_IDS = {"media:com.example.music", "progress:download", "timer:countdown", "msg:chat"}


class Laptop:
    """Minimal Orbit initiator over real localhost TCP."""

    def __init__(self, identity: KeyPair | None = None, name: str = "Laptop") -> None:
        self.identity = identity or KeyPair.generate()
        self.name = name
        self.id = device_id(self.identity.public)
        self.req = 0

    async def connect(self, phone: FakePhone, paired: bool = False, **ready) -> "Laptop":
        reader, self.writer = await asyncio.open_connection("127.0.0.1", phone.port)
        self.ch = await initiate(FrameStream(reader, self.writer), self.identity, self.name, "macos")
        self.ready = await self.recv()
        assert self.ready["t"] == "ready"
        await self.send({"t": "ready", "paired": paired, "now": int(time.time() * 1000)} | ready)
        return self

    async def send(self, msg: dict) -> None:
        await self.ch.send(msg)

    async def recv(self, within: float = 2.0) -> dict:
        return await asyncio.wait_for(self.ch.recv(), within)

    async def recv_until(self, pred, within: float = 2.0) -> tuple[dict, list[dict]]:
        """Read until pred(msg); return that message and everything read before it but pings, which are answered."""
        seen: list[dict] = []
        async with asyncio.timeout(within):
            while True:
                msg = await self.ch.recv()
                if pred(msg):
                    return msg, seen
                if msg["t"] == "ping":
                    await self.send({"t": "pong", "req": msg["req"]})
                else:
                    seen.append(msg)

    async def art(self, keys) -> dict[str, dict]:
        """The `art` messages for ``keys``, which may still be on their way, by key."""
        arts: dict[str, dict] = {}
        while set(keys) - set(arts):
            art, _ = await self.recv_until(lambda m: m["t"] == "art")
            arts[art["key"]] = art
        return arts

    async def action(self, activity: str, action: str, value=None) -> tuple[dict, list[dict]]:
        self.req += 1
        msg = {"t": "action", "req": self.req, "activity": activity, "action": action}
        if value is not None:
            msg["value"] = value
        await self.send(msg)
        return await self.recv_until(lambda m, r=self.req: m["t"] == "action_result" and m["req"] == r)

    async def expect_closed(self, within: float = 2.0) -> list[dict]:
        seen: list[dict] = []
        async with asyncio.timeout(within):
            while True:
                try:
                    seen.append(await self.ch.recv())
                except asyncio.IncompleteReadError:
                    return seen

    async def assert_silent(self, seconds: float) -> None:
        with pytest.raises(TimeoutError):
            msg = await self.recv(seconds)
            raise AssertionError(f"unexpected message {msg}")

    def close(self) -> None:
        self.writer.close()


async def pair(phone: FakePhone, laptop: Laptop) -> dict:
    """First-time pairing against an auto-accepting phone; returns the snapshot."""
    await laptop.connect(phone)
    await laptop.send({"t": "pair_request"})
    decision, _ = await laptop.recv_until(lambda m: m["t"] == "pair_decision")
    assert decision["accepted"] is True
    await laptop.send({"t": "pair_decision", "accepted": True})
    snapshot, _ = await laptop.recv_until(lambda m: m["t"] == "snapshot")
    return snapshot


@pytest.fixture
async def make_phone(tmp_path):
    phones: list[FakePhone] = []

    async def make(**kw) -> FakePhone:
        opts = {"port": 0, "mdns": False, "auto_accept": True, "tick": 0.05} | kw
        phone = FakePhone(tmp_path / "phone", **opts)
        phones.append(phone)
        await phone.start()
        return phone

    yield make
    for phone in phones:
        await phone.stop()


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


async def test_first_pairing_pins_peer_and_sends_art_after_snapshot(make_phone, tmp_path):
    phone = await make_phone(auto_accept=None)
    laptop = await Laptop().connect(phone)
    assert laptop.ready["paired"] is False and abs(laptop.ready["now"] - time.time() * 1000) < 5000
    await laptop.send({"t": "pair_request"})
    ev = await phone.wait_event("pairing")
    assert ev["sas"] == laptop.ch.sas == phone.sessions[0].sas and len(ev["sas"]) == 6
    assert ev["peer"] == laptop.id

    await phone.respond_pairing(True)
    assert (await laptop.recv()) == {"t": "pair_decision", "accepted": True}
    assert not phone.sessions[0].paired and PeerStore(tmp_path / "phone").get(laptop.id) is None

    await laptop.send({"t": "pair_decision", "accepted": True})
    assert (await phone.wait_event("paired"))["peer"] == laptop.id
    pinned = PeerStore(tmp_path / "phone").get(laptop.id)
    assert pinned is not None and pinned.static == laptop.identity.public and pinned.name == "Laptop"
    assert pinned.platform == "macos"

    snapshot, before = await laptop.recv_until(lambda m: m["t"] == "snapshot")
    assert before == []
    assert {a["id"] for a in snapshot["activities"]} == ALL_IDS
    referenced = {a[k] for a in snapshot["activities"] for k in ("iconKey", "artKey") if k in a}
    arts = {}
    for key, msg in (await laptop.art(referenced)).items():
        data = base64.b64decode(msg["data"])
        assert key == hashlib.sha256(data).hexdigest()[:16] and msg["mime"] == "image/png"
        assert max(png_size(data)) <= 256
        arts[key] = data
    assert referenced == set(arts)
    media = next(a for a in snapshot["activities"] if a["kind"] == "media")
    assert png_size(arts[media["artKey"]]) == (128, 128)
    assert abs(snapshot["now"] - time.time() * 1000) < 5000

    # Art already sent in this session is not repeated with later upserts.
    upsert, between = await laptop.recv_until(lambda m: m["t"] == "upsert")
    assert upsert["activity"]["id"] == "progress:download" and not [m for m in between if m["t"] == "art"]


async def test_reconnect_paired_gets_snapshot_without_pairing(make_phone, tmp_path):
    phone = await make_phone()
    identity = KeyPair.generate()
    first = Laptop(identity)
    await pair(phone, first)
    first.close()
    await phone.wait_event("session_closed")
    await phone.stop()

    phone = await make_phone(auto_accept=False)  # restarted phone, same config dir
    laptop = await Laptop(identity).connect(phone, paired=True)
    assert laptop.ready["paired"] is True
    snapshot, before = await laptop.recv_until(lambda m: m["t"] == "snapshot")
    assert before == [] and {a["id"] for a in snapshot["activities"]} == ALL_IDS
    assert (await phone.wait_event("ready"))["paired"] is True
    assert not [e for k, e in phone.history if k == "pairing"]

    # Pinned on the phone but the laptop forgot us: the session is not paired.
    other = await Laptop(identity).connect(phone, paired=False)
    assert other.ready["paired"] is True
    assert (await phone.wait_event("ready", predicate=lambda d: not d["paired"]))["remote_paired"] is False
    await other.send({"t": "action", "req": 1, "activity": "msg:chat", "action": "open"})
    assert (await other.recv_until(lambda m: m["t"] != "art"))[0]["code"] == "not_paired"


async def test_unpaired_session_rejects_actions_but_stays_open(make_phone):
    phone = await make_phone()
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "action", "req": 1, "activity": "msg:chat", "action": "open"})
    error = await laptop.recv()
    assert error["t"] == "error" and error["code"] == "not_paired"
    await laptop.send({"t": "ping", "req": 9})
    assert await laptop.recv() == {"t": "pong", "req": 9}
    assert not [e for k, e in phone.history if k == "action"]


async def test_phone_rejection_closes(make_phone, tmp_path):
    phone = await make_phone(auto_accept=False)
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "pair_request"})
    rest = await laptop.expect_closed()
    assert rest == [{"t": "pair_decision", "accepted": False}]
    assert (await phone.wait_event("pair_rejected"))["by"] == "local"
    assert PeerStore(tmp_path / "phone").get(laptop.id) is None and phone.sessions == []


async def test_peer_rejection_closes(make_phone, tmp_path):
    phone = await make_phone(auto_accept=None)
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "pair_request"})
    await phone.wait_event("pairing")
    await laptop.send({"t": "pair_decision", "accepted": False})
    assert await laptop.expect_closed() == []
    assert (await phone.wait_event("pair_rejected"))["by"] == "peer"
    assert (await phone.wait_event("session_closed"))["peer"] == laptop.id
    with pytest.raises(LookupError):
        await phone.respond_pairing(True)


async def test_prompt_callback_decides(make_phone):
    asked = []

    async def prompt(session):
        asked.append((session.peer.name, session.sas))
        await asyncio.sleep(0.05)
        return True

    phone = await make_phone(auto_accept=None, prompt=prompt)
    laptop = Laptop()
    snapshot = await pair(phone, laptop)
    assert asked == [("Laptop", laptop.ch.sas)] and {a["id"] for a in snapshot["activities"]} == ALL_IDS


async def test_pairing_timeout_closes(make_phone):
    phone = await make_phone(auto_accept=None, pairing_timeout=0.2)
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "pair_request"})
    assert await laptop.expect_closed() == [{"t": "bye", "reason": "pairing timeout"}]
    assert (await phone.wait_event("pair_rejected"))["by"] == "timeout"


async def test_media_actions(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    is_media = lambda m: m["t"] == "upsert" and m["activity"]["id"] == "media:com.example.music"  # noqa: E731

    result, seen = await laptop.action("media:com.example.music", "media.pause")
    assert result["ok"] is True and phone.media.state == "paused"
    media = [m for m in seen if is_media(m)][-1]["activity"]
    assert media["media"]["state"] == "paused" and "media.play" in [a["id"] for a in media["actions"]]
    assert media["media"]["rate"] == 0.0
    paused_at = phone.media.position_ms()
    await asyncio.sleep(0.1)
    assert phone.media.position_ms() == paused_at == media["media"]["positionMs"]

    result, seen = await laptop.action("media:com.example.music", "media.next")
    assert result["ok"] and phone.media.track_index == 1
    upsert = next(m for m in seen if is_media(m))
    assert upsert["activity"]["title"] == phone.media.tracks[1].title and upsert["activity"]["media"]["positionMs"] == 0
    new_art = upsert["activity"]["artKey"]
    assert new_art in await laptop.art([new_art])  # art follows the activity that references it

    result, seen = await laptop.action("media:com.example.music", "media.seek", 30_000)
    assert result["ok"] and phone.media.position_ms() == 30_000
    assert [m for m in seen if is_media(m)][-1]["activity"]["media"]["positionMs"] == 30_000
    result, _ = await laptop.action("media:com.example.music", "media.seek", 10**9)
    assert result["ok"] and phone.media.position_ms() == phone.media.tracks[1].duration_ms
    for bad in ("12", 1.5, True):
        result, _ = await laptop.action("media:com.example.music", "media.seek", bad)
        assert result["ok"] is False and "integer" in result["error"]

    await laptop.action("media:com.example.music", "media.previous")  # past 3 s: restart track 2
    assert phone.media.track_index == 1 and phone.media.position_ms() == 0
    result, seen = await laptop.action("media:com.example.music", "media.play")
    assert result["ok"] and phone.media.state == "playing"
    playing = [m for m in seen if is_media(m)][-1]
    assert playing["activity"]["media"]["rate"] == 1.0 and "media.pause" in [
        a["id"] for a in playing["activity"]["actions"]
    ]
    await asyncio.sleep(0.15)
    assert phone.media.position_ms() >= 100

    # Playing media is not re-sent every tick (receivers extrapolate), while the download is.
    quiet = []
    try:
        async with asyncio.timeout(0.4):
            while True:
                quiet.append(await laptop.ch.recv())
    except TimeoutError:
        pass
    assert any(m["t"] == "upsert" and m["activity"]["id"] == "progress:download" for m in quiet)
    assert not [m for m in quiet if is_media(m)]


async def test_media_auto_advances_at_track_end(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    duration = phone.media.track.duration_ms
    await laptop.action("media:com.example.music", "media.seek", duration - 100)
    upsert, _ = await laptop.recv_until(
        lambda m: m["t"] == "upsert" and m["activity"].get("title") == phone.media.tracks[1].title
    )
    assert phone.media.track_index == 1 and upsert["activity"]["media"]["state"] == "playing"


async def test_chat_reply_and_mark_read(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    result, _ = await laptop.action("msg:chat", "notif.0")
    assert result == {"t": "action_result", "req": 1, "ok": False, "error": "text required"}
    result, seen = await laptop.action("msg:chat", "notif.0", "On my way")
    assert result["ok"] and phone.chat.replies == ["On my way"]
    chat = next(m for m in seen if m["t"] == "upsert" and m["activity"]["id"] == "msg:chat")
    assert chat["activity"]["text"] == "You: On my way"
    ev = await phone.wait_event("action", predicate=lambda d: d["ok"])
    assert ev == {
        "session": "s1",
        "peer": laptop.id,
        "activity": "msg:chat",
        "action": "notif.0",
        "value": "On my way",
        "ok": True,
        "error": None,
    }
    result, seen = await laptop.action("msg:chat", "dismiss")
    assert result["ok"] is False and "not dismissable" in result["error"]
    result, seen = await laptop.action("msg:chat", "notif.1")
    assert result["ok"] and {"t": "remove", "id": "msg:chat"} in seen
    result, _ = await laptop.action("msg:chat", "open")
    assert result["ok"] is False and "unknown activity" in result["error"]


async def test_unknown_activity_and_action(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    result, _ = await laptop.action("nope", "open")
    assert result["ok"] is False and "unknown activity" in result["error"]
    result, _ = await laptop.action("timer:countdown", "media.pause")
    assert result["ok"] is False and "unknown action" in result["error"]
    result, _ = await laptop.action("timer:countdown", "open")
    assert result["ok"] is True


async def test_download_cancel_removes(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    result, seen = await laptop.action("progress:download", "notif.0")
    assert result["ok"] and phone.download.cancelled
    assert {"t": "remove", "id": "progress:download"} in seen
    assert "progress:download" not in phone.activities


async def test_download_completes_lingers_and_restarts(make_phone):
    phone = await make_phone()
    phone.download.step = 40
    phone.download.linger_ticks = 2
    phone.download.restart_ticks = 2
    laptop = Laptop()
    await pair(phone, laptop)
    is_dl = lambda m: m.get("activity", m).get("id") == "progress:download"  # noqa: E731
    done, before = await laptop.recv_until(
        lambda m: m["t"] == "upsert" and m["activity"]["title"] == "Download complete"
    )
    values = [m["activity"]["progress"]["value"] for m in before if m["t"] == "upsert" and is_dl(m)]
    assert values == sorted(values) and done["activity"]["progress"]["value"] == 100
    _, between = await laptop.recv_until(lambda m: m["t"] == "remove" and m["id"] == "progress:download")
    assert not [m for m in between if m["t"] == "upsert" and is_dl(m)]
    restart, _ = await laptop.recv_until(lambda m: m["t"] == "upsert" and is_dl(m))
    assert restart["activity"]["title"].startswith("Downloading") and phone.download.completed == 1


async def test_timer_pause_add_and_done(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    is_timer = lambda m: m["t"] == "upsert" and m["activity"]["id"] == "timer:countdown"  # noqa: E731

    result, seen = await laptop.action("timer:countdown", "notif.0")
    assert result["ok"] and not phone.timer.running
    chrono = [m for m in seen if is_timer(m)][-1]["activity"]["chrono"]
    assert chrono["running"] is False and chrono["countDown"] is True
    assert chrono["valueMs"] == phone.timer.remaining_ms() and 290_000 < chrono["valueMs"] <= 300_000
    remaining = phone.timer.remaining_ms()
    await laptop.action("timer:countdown", "notif.1")
    assert phone.timer.remaining_ms() == remaining + 60_000

    phone.timer._paused_remaining = 150  # resume with 150 ms left
    result, seen = await laptop.action("timer:countdown", "notif.0")
    resumed = [m for m in seen if is_timer(m)][-1]["activity"]
    assert phone.timer.running and resumed["chrono"]["running"] is True
    assert abs(resumed["chrono"]["baseMs"] - (time.time() * 1000 + 150)) < 1000
    done, _ = await laptop.recv_until(lambda m: is_timer(m) and m["activity"]["title"] == "Timer done")
    assert done["activity"]["chrono"]["valueMs"] == 0 and phone.timer.state == "done"
    result, seen = await laptop.action("timer:countdown", "dismiss")
    assert result["ok"] and {"t": "remove", "id": "timer:countdown"} in seen


async def test_ping_pong_and_bye_on_stop(make_phone):
    phone = await make_phone()
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "ping", "req": 42})
    assert await laptop.recv() == {"t": "pong", "req": 42}
    await phone.stop()
    assert await laptop.expect_closed() == [{"t": "bye", "reason": "shutdown"}]


async def test_keepalive_ping_and_dead_peer(make_phone):
    phone = await make_phone(keepalive=0.15, dead_after=0.5)
    laptop = await Laptop().connect(phone)
    start = time.monotonic()
    ping = await laptop.recv(1)
    assert ping["t"] == "ping" and time.monotonic() - start >= 0.1

    # A peer that keeps talking stays alive past dead_after.
    for _ in range(8):
        await laptop.send({"t": "pong", "req": 0})
        await asyncio.sleep(0.1)
    assert phone.sessions and not phone.sessions[0].closed

    seen = await laptop.expect_closed(2)
    assert seen and {m["t"] for m in seen} == {"ping"}
    ev = await phone.wait_event("session_closed")
    assert "silent" in ev["reason"] and phone.sessions == []


async def test_a_frame_still_arriving_keeps_the_session_alive(make_phone):
    phone = await make_phone(keepalive=10, dead_after=0.5)
    laptop = await Laptop().connect(phone)
    laptop.writer.write(struct.pack(">I", 4000))  # a frame trickling in at a byte per 0.2 s
    for _ in range(8):
        await asyncio.sleep(0.2)
        laptop.writer.write(b"\x00")
    assert phone.sessions and not phone.sessions[0].closed


async def test_action_past_its_expiry_is_refused_unapplied(make_phone):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    pause = {"activity": "media:com.example.music", "action": "media.pause"}
    now = phone_module.now_ms()
    for req, expires, error in ((1, now - 1, "expired"), (2, "soon", "bad_request"), (3, None, "bad_request")):
        await laptop.send({"t": "action", "req": req, "expiresAt": expires} | pause)
        result, _ = await laptop.recv_until(lambda m: m["t"] == "action_result")
        assert result == {"t": "action_result", "req": req, "ok": False, "error": error}
    assert phone.media.state == "playing"
    await laptop.send({"t": "action", "req": 4, "expiresAt": now + 60_000} | pause)
    result, _ = await laptop.recv_until(lambda m: m["t"] == "action_result")
    assert result == {"t": "action_result", "req": 4, "ok": True}
    assert phone.media.state == "paused"
    result, _ = await laptop.action("media:com.example.music", "media.play")  # no expiresAt
    assert result["ok"] is True and phone.media.state == "playing"


async def test_action_expiry_boundary_is_refused(make_phone, monkeypatch):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    monkeypatch.setattr(phone_module, "now_ms", lambda: 1_000_000)
    msg = {"t": "action", "req": 1, "activity": "media:com.example.music", "action": "media.pause"}
    await laptop.send(msg | {"expiresAt": 1_000_000})
    result, _ = await laptop.recv_until(lambda m: m["t"] == "action_result")
    assert result["error"] == "expired" and phone.media.state == "playing"
    await laptop.send(msg | {"req": 2, "expiresAt": 1_000_001})
    result, _ = await laptop.recv_until(lambda m: m["t"] == "action_result")
    assert result["ok"] is True and phone.media.state == "paused"


async def test_action_read_after_the_silence_deadline_is_not_applied(make_phone):
    """Bytes that waited past dead_after (the process was stopped) are not handled: the peer gave up."""
    phone = await make_phone(keepalive=10, dead_after=0.5)
    laptop = Laptop()
    await pair(phone, laptop)
    await laptop.send({"t": "action", "req": 1, "activity": "media:com.example.music", "action": "media.pause"})
    time.sleep(0.7)  # noqa: ASYNC251 - the phone's event loop stalls with the action unread
    ev = await phone.wait_event("session_closed")
    assert "silent" in ev["reason"]
    assert phone.media.state == "playing"


async def test_freeze_stops_all_traffic(make_phone):
    phone = await make_phone(keepalive=0.1, dead_after=0.3)
    laptop = Laptop()
    await pair(phone, laptop)
    phone.freeze()
    await asyncio.sleep(0.05)
    while True:  # drain anything already in flight
        try:
            await laptop.recv(0.05)
        except TimeoutError:
            break
    await laptop.send({"t": "ping", "req": 1})
    await laptop.send({"t": "action", "req": 1, "activity": "media:com.example.music", "action": "media.pause"})
    phone.upsert({"id": "x", "kind": "generic", "app": {"id": "a", "name": "A"}, "title": "X"})
    await laptop.assert_silent(0.6)  # no pong, no keepalive ping, no upsert, and not closed (> dead_after)
    assert len(phone.sessions) == 1 and not phone.sessions[0].closed
    assert phone.media.state == "playing"  # the action was never read


async def test_custom_upsert_and_remove_reach_all_paired_sessions(make_phone):
    phone = await make_phone()
    a, b = Laptop(name="A"), Laptop(name="B")
    await pair(phone, a)
    await pair(phone, b)
    unpaired = await Laptop().connect(phone)
    icon = phone.add_art(cover("#000000", "#ffffff", 32))
    activity = {
        "id": "call:1",
        "kind": "call",
        "app": {"id": "dialer", "name": "Phone"},
        "title": "Bob",
        "iconKey": icon,
        "actions": [{"id": "notif.0", "label": "Hang up", "icon": "call_end"}],
    }
    phone.upsert(activity)
    for laptop in (a, b):
        msg, _ = await laptop.recv_until(lambda m: m["t"] == "upsert" and m["activity"]["id"] == "call:1")
        assert msg["activity"]["title"] == "Bob" and "updatedMs" in msg["activity"]
        assert icon in await laptop.art([icon])
    result, _ = await a.action("call:1", "notif.0")
    assert result["ok"] is True
    result, _ = await a.action("call:1", "dismiss")
    assert result["ok"] is False
    assert phone.remove("call:1") is True
    for laptop in (a, b):
        await laptop.recv_until(lambda m: m == {"t": "remove", "id": "call:1"})
    await unpaired.assert_silent(0.2)


async def test_large_art_goes_in_parts_to_a_laptop_that_reassembles_them(make_phone):
    phone = await make_phone()
    image = b"\xff\xd8\xff" + bytes(range(256)) * 40  # a 10 KB "JPEG"
    key = phone.add_art(image)
    activity = {"id": "m", "kind": "media", "app": {"id": "p", "name": "P"}, "title": "T", "artKey": key, "actions": []}
    parts_laptop, whole_laptop = Laptop(), Laptop()
    for laptop, ready in ((parts_laptop, {"artParts": True}), (whole_laptop, {})):
        await laptop.connect(phone, **ready)
        await laptop.send({"t": "pair_request"})
        await laptop.recv_until(lambda m: m["t"] == "pair_decision")
        await laptop.send({"t": "pair_decision", "accepted": True})
        await laptop.recv_until(lambda m: m["t"] == "snapshot")
    phone.upsert(activity)
    await parts_laptop.recv_until(lambda m: m["t"] == "upsert")
    parts = [(await parts_laptop.recv_until(lambda m: m["t"] == "art" and m["key"] == key))[0] for _ in range(3)]
    assert [(m["part"], m["parts"], m["mime"]) for m in parts] == [(i, 3, "image/jpeg") for i in range(3)]
    assert [len(base64.b64decode(m["data"])) for m in parts] == [4096, 4096, len(image) - 8192]
    assert b"".join(base64.b64decode(m["data"]) for m in parts) == image
    await whole_laptop.recv_until(lambda m: m["t"] == "upsert")
    art = (await whole_laptop.art([key]))[key]
    assert "part" not in art and base64.b64decode(art["data"]) == image


async def test_unpair_removes_pin_and_closes(make_phone, tmp_path):
    phone = await make_phone()
    laptop = Laptop()
    await pair(phone, laptop)
    await laptop.send({"t": "unpair"})
    await laptop.expect_closed()
    assert (await phone.wait_event("unpaired"))["peer"] == laptop.id
    assert PeerStore(tmp_path / "phone").get(laptop.id) is None


async def test_default_port_falls_back_when_taken(make_phone, monkeypatch):
    holder = socket.socket()
    holder.bind(("0.0.0.0", 0))
    holder.listen()
    taken = holder.getsockname()[1]
    try:
        with pytest.raises(OSError) as e:
            await make_phone(port=taken)  # only the well-known port falls back
        assert e.value.errno == errno.EADDRINUSE
        monkeypatch.setattr(phone_module, "DEFAULT_PORT", taken)
        phone = await make_phone(port=taken)
        assert phone.port not in (0, taken)
        await Laptop().connect(phone)
    finally:
        holder.close()


async def test_removed_scripted_activity_stays_removed(make_phone):
    phone = await make_phone()  # tick 0.05: the download would republish on every tick
    assert "progress:download" in phone.activities
    assert phone.remove("progress:download") is True
    await asyncio.sleep(0.5)
    assert "progress:download" not in phone.activities


async def test_frozen_phone_does_not_time_out_pending_pairing(make_phone):
    phone = await make_phone(auto_accept=None, pairing_timeout=0.2)
    laptop = await Laptop().connect(phone)
    await laptop.send({"t": "pair_request"})
    await phone.wait_event("pairing")
    phone.freeze()
    await asyncio.sleep(0.5)
    assert len(phone.sessions) == 1 and not phone.sessions[0].closed
    assert not [k for k, _ in phone.history if k == "pair_rejected"]


async def test_art_mime_follows_image_type(make_phone):
    phone = await make_phone()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    key = phone.add_art(jpeg)
    with pytest.raises(ValueError):
        phone.add_art(b"not an image")
    laptop = Laptop()
    await pair(phone, laptop)
    phone.upsert({"id": "j", "kind": "generic", "title": "J", "artKey": key})
    art, _ = await laptop.recv_until(lambda m: m["t"] == "art" and m["key"] == key)
    assert art["mime"] == "image/jpeg" and base64.b64decode(art["data"]) == jpeg


def test_advertised_addresses_for_ipv6_hosts():
    from orbit.fakephone.mdns import advertised_addresses

    assert advertised_addresses("::1") == ["::1"]
    assert advertised_addresses("127.0.0.1") == ["127.0.0.1"]
    assert all(":" in a for a in advertised_addresses("::"))


async def test_session_neither_paired_nor_pairing_is_closed_after_the_deadline(make_phone):
    phone = await make_phone(unpaired_timeout=0.3)
    idle = await Laptop().connect(phone)
    assert await idle.expect_closed(1.0) == [{"t": "bye", "reason": "not paired"}]

    pending = await Laptop().connect(phone)
    await pending.send({"t": "pair_request"})
    await pending.recv_until(lambda m: m["t"] == "pair_decision")  # phone accepted; laptop undecided
    await pending.assert_silent(0.6)

    laptop = Laptop()
    await pair(phone, laptop)
    await asyncio.sleep(0.5)
    result, _ = await laptop.action("media:com.example.music", "media.pause")
    assert result["ok"] is True
