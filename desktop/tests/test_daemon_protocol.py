"""orbitd against scripted raw responders: protocol edge cases, timeouts and time conversion."""

import asyncio
import json
import os
import stat

import pytest
from conftest import RawPhone, pin_in, until

from orbit.client import ApiError
from orbit.daemon.timeconv import now_ms, to_local
from orbit.proto import KeyPair, ProtocolError, device_id


async def paired_script(channel, after=None):
    """Responder side of an already-paired session: ready exchange, then ``after(channel)``."""
    await channel.send({"t": "ready", "paired": True, "now": now_ms()})
    ready = await channel.recv()
    assert ready["t"] == "ready" and ready["paired"] is True
    if after:
        return await after(channel)
    while True:
        msg = await channel.recv()
        if msg["t"] == "ping":
            await channel.send({"t": "pong", "req": msg["req"]})


async def connect_pinned(make_daemon, short_tmp, phone, **kw):
    pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
    daemon = await make_daemon(**kw)
    await until(lambda: daemon.devices[phone.id].session is not None)
    return daemon


async def test_unpaired_application_message_gets_not_paired(make_daemon, api_for):
    replies = asyncio.get_running_loop().create_future()

    async def script(ch):
        await ch.send({"t": "ready", "paired": False, "now": now_ms()})
        assert (await ch.recv())["paired"] is False
        assert (await ch.recv())["t"] == "pair_request"
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "x", "kind": "generic", "title": "T"}]})
        replies.set_result(await ch.recv())
        await asyncio.sleep(10)

    async with RawPhone(script) as phone:
        daemon = await make_daemon()
        api = await api_for(daemon)
        assert (await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port}))["id"] == phone.id
        reply = await asyncio.wait_for(replies, 5)
        assert reply["t"] == "error" and reply["code"] == "not_paired"
        assert await api.call("activities.list") == []
        pairing = (await api.call("pairing.list"))[0]
        assert pairing["deviceId"] == phone.id and len(pairing["sas"]) == 6
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {"device": phone.id, "activity": "x", "action": "open"})
        assert e.value.code == "not_paired"


async def test_dead_phone_is_dropped_but_responsive_one_kept(make_daemon, short_tmp):
    pings = []

    async def silent(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        while True:  # reads (and counts pings) but never answers
            msg = await ch.recv()
            pings.append(msg["t"])

    async with RawPhone(paired_script) as alive, RawPhone(silent) as dead:
        pin_in(short_tmp / "daemon", alive.identity, address=f"127.0.0.1:{alive.port}")
        pin_in(short_tmp / "daemon", dead.identity, address=f"127.0.0.1:{dead.port}")
        daemon = await make_daemon(keepalive=0.2, dead_after=0.8)
        await until(lambda: daemon.devices[alive.id].session and daemon.devices[dead.id].session)
        dead_session = daemon.devices[dead.id].session
        await asyncio.wait_for(dead_session.closed.wait(), 3)
        assert "no data" in dead_session.close_reason
        assert pings.count("ping") >= 2
        await asyncio.sleep(1.0)
        assert daemon.devices[alive.id].session is not None
        assert not daemon.devices[alive.id].session.closed.is_set()


async def test_impostor_with_wrong_id_at_remembered_address_is_rejected(make_daemon, short_tmp, api_for):
    real = KeyPair.generate()
    got_ready = []

    async def impostor_script(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        got_ready.append(await ch.recv())

    async with RawPhone(impostor_script) as impostor:
        pin_in(short_tmp / "daemon", real, address=f"127.0.0.1:{impostor.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: impostor.results)
        assert isinstance(impostor.results[0], asyncio.IncompleteReadError) and not got_ready
        devices = await api.call("devices.list")
        assert [d["id"] for d in devices] == [device_id(real.public)]
        assert not any(d["connected"] for d in devices)
        with pytest.raises(ApiError) as e:
            await api.call("devices.connect", {"id": next(iter(daemon.devices))})
        assert e.value.code == "failed" and impostor.id in e.value.message


async def test_impostor_claiming_pinned_key_cannot_decrypt(make_daemon, short_tmp):
    real, fake = KeyPair.generate(), KeyPair.generate()

    class Stolen(KeyPair):
        @property
        def public(self):  # advertises the real phone's static key, but only has its own private key
            return real.public

    async def script(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        return await ch.recv()

    async with RawPhone(script, identity=Stolen(fake.private)) as impostor:
        assert impostor.id == RawPhone(None, identity=real).id
        pin_in(short_tmp / "daemon", real, address=f"127.0.0.1:{impostor.port}")
        daemon = await make_daemon()
        await until(lambda: impostor.results)
        # The daemon's ready is encrypted under keys the impostor cannot derive.
        assert isinstance(impostor.results[0], ProtocolError) and impostor.results[0].code == "decrypt_failed"
        assert daemon.devices[impostor.id].session is None


async def test_snapshot_timestamps_converted_to_local_clock(make_daemon, short_tmp, api_for):
    skew = 3_600_000  # phone clock one hour ahead

    async def after(ch):
        phone_now = now_ms() + skew
        await ch.send(
            {
                "t": "snapshot",
                "now": phone_now,
                "activities": [
                    {
                        "id": "media:x",
                        "kind": "media",
                        "title": "Song",
                        "media": {"state": "playing", "positionMs": 5000, "durationMs": 100000, "rate": 1.0},
                        "chrono": {"baseMs": phone_now - 60_000, "countDown": False, "running": True},
                        "actions": [],
                        "updatedMs": phone_now - 1000,
                    }
                ],
            }
        )
        await asyncio.sleep(10)

    async with RawPhone(lambda ch: paired_script(ch, after)) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone)
        api = await api_for(daemon)
        acts = await until(lambda: api.call("activities.list"))
        local = now_ms()
        a = acts[0]
        assert a["device"] == phone.id and a["media"]["positionMs"] == 5000
        assert abs(a["media"]["positionAtMs"] - local) < 500
        assert abs(a["chrono"]["baseMs"] - (local - 60_000)) < 500
        assert abs(a["updatedMs"] - (local - 1000)) < 500


def test_to_local_shifts_only_timestamps():
    act = {
        "id": "t",
        "media": {"positionMs": 7, "durationMs": 9},
        "chrono": {"baseMs": 1000, "valueMs": 50, "running": False},
        "updatedMs": 2000,
    }
    out = to_local(act, "dev", sender_now=5000, offset=-300)
    assert out["device"] == "dev"
    assert out["media"] == {"positionMs": 7, "durationMs": 9, "positionAtMs": 4700}
    assert out["chrono"] == {"baseMs": 700, "valueMs": 50, "running": False}
    assert out["updatedMs"] == 1700
    assert act["updatedMs"] == 2000 and "positionAtMs" not in act["media"]


async def test_action_timeout_and_result_passthrough(make_daemon, short_tmp, api_for):
    async def after(ch):
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "a", "kind": "generic", "actions": []}]})
        while True:
            msg = await ch.recv()
            if msg["t"] == "action" and msg["action"] == "refuse":
                await ch.send({"t": "action_result", "req": msg["req"], "ok": False, "error": "nope"})
            elif msg["t"] == "action" and msg["action"] == "fine":
                assert msg["value"] == 42
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    async with RawPhone(lambda ch: paired_script(ch, after)) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone, action_timeout=0.3)
        api = await api_for(daemon)
        await until(lambda: api.call("activities.list"))
        base = {"device": phone.id, "activity": "a"}
        assert await api.call("activities.action", {**base, "action": "fine", "value": 42}) == {"ok": True}
        assert await api.call("activities.action", {**base, "action": "refuse"}) == {"ok": False, "error": "nope"}
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {**base, "action": "ignored"})
        assert e.value.code == "timeout"


async def test_local_api_errors_and_socket_mode(make_daemon, api_for):
    daemon = await make_daemon()
    assert stat.S_IMODE(os.stat(daemon.api.path).st_mode) == 0o600
    api = await api_for(daemon)

    async def code(method, params=None):
        with pytest.raises(ApiError) as e:
            await api.call(method, params)
        return e.value.code

    assert await code("no.such.method") == "bad_request"
    assert await code("devices.connect", {"host": "127.0.0.1"}) == "bad_request"
    assert await code("devices.connect", {"id": "f" * 32}) == "not_found"
    assert await code("devices.disconnect", {"id": "f" * 32}) == "not_found"
    assert await code("devices.forget", {"id": "f" * 32}) == "not_found"
    assert await code("pairing.respond", {"session": "nope", "accept": True}) == "not_found"
    assert await code("pairing.respond", {"session": "nope", "accept": "yes"}) == "bad_request"
    assert await code("activities.action", {"device": "f" * 32, "activity": "a", "action": "b"}) == "not_found"
    assert await code("art.get", {"device": "f" * 32, "key": "0" * 16}) == "not_found"
    # Connection refused on a free port.
    assert await code("devices.connect", {"host": "127.0.0.1", "port": 9}) == "failed"

    reader, writer = await asyncio.open_unix_connection(str(daemon.api.path))
    writer.write(b"{not json\n")
    reply = json.loads(await reader.readline())
    assert reply["id"] is None and reply["error"]["code"] == "bad_request"
    writer.close()

    status = await api.call("status")
    assert status["deviceId"] == daemon.device_id and status["name"] == "Test Laptop"


async def test_pinned_but_offline_device_errors(make_daemon, short_tmp, api_for):
    pin_in(short_tmp / "daemon", KeyPair.generate())
    daemon = await make_daemon()
    api = await api_for(daemon)
    (dev,) = await api.call("devices.list")
    assert dev["paired"] and not dev["connected"] and dev["transport"] is None
    for method, params in (
        ("activities.action", {"device": dev["id"], "activity": "a", "action": "b"}),
        ("devices.disconnect", {"id": dev["id"]}),
        ("devices.connect", {"id": dev["id"]}),
    ):
        with pytest.raises(ApiError) as e:
            await api.call(method, params)
        assert e.value.code == "not_connected", method


async def test_second_daemon_refuses_live_socket(make_daemon, short_tmp):
    from orbit.daemon.api import AlreadyRunning
    from orbit.daemon.core import Daemon

    daemon = await make_daemon()
    other = Daemon(short_tmp / "other", "Other", daemon.api.path, mdns=False, ble=False)
    with pytest.raises(AlreadyRunning):
        await other.start()
