"""orbitd <-> orbit-fakephone over real TCP, driven through the local API."""

import asyncio
import base64
import hashlib
import os
import stat

import pytest
from conftest import until

from orbit.fakephone import FakePhone
from orbit.store import PeerStore


@pytest.fixture
async def make_phone(short_tmp):
    phones = []

    async def make(config="phone", **kw):
        kw.setdefault("mdns", False)
        kw.setdefault("host", "127.0.0.1")
        kw.setdefault("port", 0)
        phone = FakePhone(short_tmp / config, **kw)
        await phone.start()
        phones.append(phone)
        return phone

    yield make
    for p in phones:
        await p.stop()


async def pair(api, phone):
    await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port})
    (pairing,) = await until(lambda: api.call("pairing.list"))
    await api.call("pairing.respond", {"session": pairing["session"], "accept": True})
    await until(lambda: _device(api, phone.device_id, paired=True, connected=True))
    return pairing


async def _device(api, dev_id, **expect):
    for d in await api.call("devices.list"):
        if d["id"] == dev_id and all(d[k] == v for k, v in expect.items()):
            return d
    return None


async def _activity(api, act_id):
    return next((a for a in await api.call("activities.list") if a["id"] == act_id), None)


async def test_first_pairing_shows_same_sas_on_both_ends(make_daemon, make_phone, api_for, short_tmp):
    phone = await make_phone(auto_accept=None)
    daemon = await make_daemon()
    api = await api_for(daemon)
    assert (await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port}))["id"] == phone.device_id

    (pairing,) = await until(lambda: api.call("pairing.list"))
    phone_side = await phone.wait_event("pairing")
    assert pairing["deviceId"] == phone.device_id and phone_side["peer"] == daemon.device_id
    assert pairing["sas"] == phone_side["sas"] and len(pairing["sas"]) == 6 and pairing["sas"].isdigit()
    assert (pairing["localAccepted"], pairing["remoteAccepted"]) == (False, False)
    assert await api.call("activities.list") == []
    assert (await _device(api, phone.device_id))["paired"] is False

    await api.call("pairing.respond", {"session": pairing["session"], "accept": True})
    (pairing,) = await api.call("pairing.list")
    assert (pairing["localAccepted"], pairing["remoteAccepted"]) == (True, False)
    assert await api.call("activities.list") == []  # not paired until the phone accepts too

    await phone.respond_pairing(True)
    await until(lambda: _device(api, phone.device_id, paired=True, connected=True, transport="lan"))
    assert await api.call("pairing.list") == []
    acts = await until(lambda: api.call("activities.list"))
    assert {"media:com.example.music", "timer:countdown", "msg:chat"} <= {a["id"] for a in acts}

    # Both sides pinned each other, persisted with owner-only permissions.
    assert phone.pins.get(daemon.device_id) is not None
    pinned = PeerStore(short_tmp / "daemon").get(phone.device_id)
    assert pinned.static == phone.identity.public and pinned.address == f"127.0.0.1:{phone.port}"
    for name in ("identity.pem", "peers.json"):
        assert stat.S_IMODE(os.stat(short_tmp / "daemon" / name).st_mode) == 0o600


async def test_rejected_pairing_pins_nothing(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=False)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port})
    await phone.wait_event("pair_rejected")
    await until(lambda: not daemon.devices)  # unpaired, undiscovered device is dropped once the session ends
    assert await api.call("pairing.list") == []
    assert daemon.store.get(phone.device_id) is None and phone.pins.get(daemon.device_id) is None


async def test_restarted_daemon_reconnects_without_pairing(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    first = await make_daemon()
    await pair(await api_for(first), phone)
    await first.stop()
    await phone.wait_event("session_closed")
    pairings_before = sum(1 for kind, _ in phone.history if kind == "pairing")

    second = await make_daemon()
    api = await api_for(second)
    await until(lambda: _device(api, phone.device_id, paired=True, connected=True))
    assert await until(lambda: _activity(api, "media:com.example.music"))
    assert await api.call("pairing.list") == []
    assert sum(1 for kind, _ in phone.history if kind == "pairing") == pairings_before


async def test_snapshot_upsert_remove_reach_subscribers(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await pair(api, phone)
    watcher = await api_for(daemon)
    assert await watcher.call("subscribe") is True
    events = watcher.events()
    initial = [await anext(events) for _ in range(3)]
    assert [e for e, _ in initial] == ["devices.changed", "pairing.changed", "activities.changed"]

    async def next_activities(predicate):
        async for event, data in events:
            if event == "activities.changed" and predicate(data["activities"]):
                return data["activities"]

    phone.upsert({"id": "generic:test", "kind": "generic", "title": "Hello", "actions": [], "updatedMs": 1})
    acts = await asyncio.wait_for(next_activities(lambda a: any(x["id"] == "generic:test" for x in a)), 5)
    test_act = next(a for a in acts if a["id"] == "generic:test")
    assert test_act["title"] == "Hello" and test_act["device"] == phone.device_id

    phone.remove("generic:test")
    await asyncio.wait_for(next_activities(lambda a: all(x["id"] != "generic:test" for x in a)), 5)


async def test_actions_round_trip_and_change_phone_state(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await pair(api, phone)
    await until(lambda: _activity(api, "media:com.example.music"))
    dev = phone.device_id

    async def act(activity, action, **kw):
        return await api.call("activities.action", {"device": dev, "activity": activity, "action": action, **kw})

    media = "media:com.example.music"
    assert phone.media.state == "playing"
    assert await act(media, "media.pause") == {"ok": True}
    assert phone.media.state == "paused"
    assert (await until(lambda: _paused(api, media)))["media"]["state"] == "paused"

    track = phone.media.track_index
    assert await act(media, "media.next") == {"ok": True}
    assert phone.media.track_index == track + 1

    assert await act(media, "media.seek", value=30_000) == {"ok": True}
    assert abs(phone.media.position_ms() - 30_000) < 1000
    assert (await act(media, "media.seek", value="soon"))["ok"] is False

    assert await act("msg:chat", "notif.0", value="On my way") == {"ok": True}
    assert phone.chat.replies == ["On my way"]
    refused = await act("msg:chat", "notif.0")
    assert refused["ok"] is False and refused["error"]

    assert await until(lambda: _activity(api, "progress:download"))
    assert await act("progress:download", "notif.0") == {"ok": True}
    assert phone.download.cancelled
    await until(lambda: _gone(api, "progress:download"))


async def _paused(api, act_id):
    a = await _activity(api, act_id)
    return a if a and a["media"]["state"] == "paused" else None


async def _gone(api, act_id):
    return await _activity(api, act_id) is None


async def test_art_is_cached_and_served(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await pair(api, phone)
    media = await until(lambda: _activity(api, "media:com.example.music"))
    for key in (media["artKey"], media["iconKey"]):
        art = await api.call("art.get", {"device": phone.device_id, "key": key})
        raw = base64.b64decode(art["data"])
        assert art["mime"] == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(raw).hexdigest()[:16] == key


async def test_forget_unpairs_both_sides(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await pair(api, phone)
    assert await api.call("devices.forget", {"id": phone.device_id}) is True
    await phone.wait_event("unpaired")
    assert phone.pins.get(daemon.device_id) is None
    assert daemon.store.get(phone.device_id) is None
    assert await api.call("activities.list") == []
    assert await _device(api, phone.device_id) is None


async def test_disconnect_suppresses_reconnect_until_connect(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon()
    api = await api_for(daemon)
    await pair(api, phone)
    assert await api.call("devices.disconnect", {"id": phone.device_id}) is True
    await asyncio.sleep(1.5)  # longer than the first reconnect backoff
    assert await _device(api, phone.device_id, paired=True, connected=False)
    await api.call("devices.connect", {"id": phone.device_id})
    assert await _device(api, phone.device_id, paired=True, connected=True)


async def test_frozen_phone_is_detected_dead(make_daemon, make_phone, api_for):
    phone = await make_phone(auto_accept=True)
    daemon = await make_daemon(keepalive=0.2, dead_after=0.8)
    api = await api_for(daemon)
    await pair(api, phone)
    session = daemon.devices[phone.device_id].session
    phone.freeze()
    await asyncio.wait_for(session.closed.wait(), 3)
    assert "no data" in session.close_reason
    assert await api.call("activities.list") == []
