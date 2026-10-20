"""orbitd against scripted raw responders: protocol edge cases, timeouts and time conversion."""

import asyncio
import json
import os
import stat

import pytest
from daemon_helpers import RawPhone, pin_in, until

import orbit.daemon.core as core_mod
import orbit.daemon.session as session_mod
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
        await ch.send({"t": "ping", "req": 77})  # allowed before pairing
        assert await ch.recv() == {"t": "pong", "req": 77}
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


async def test_ping_deadline_allows_for_the_round_trip_time(make_daemon, short_tmp, monkeypatch):
    """A link whose round trip (0.4 s) is longer than the ping deadline (0.3 s) is slow, not dead."""
    monkeypatch.setattr(session_mod, "PROBE_FLOWING", 0.05)  # the pong gap would otherwise pass as "flowing"
    rtt = 0.4
    pongs = []

    async def slow_link(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})

        async def pong(req):
            await asyncio.sleep(rtt)
            await ch.send({"t": "pong", "req": req})
            pongs.append(req)

        while True:
            msg = await ch.recv()
            if msg["t"] == "ping":
                asyncio.ensure_future(pong(msg["req"]))

    real_open = core_mod.open_tcp

    async def slow_open(host, port):
        await asyncio.sleep(rtt)
        return await real_open(host, port)

    monkeypatch.setattr(core_mod, "open_tcp", slow_open)
    async with RawPhone(slow_link, handshake_delay=rtt) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone, keepalive=0.6, stall_after=0.3, dead_after=10)
        session = daemon.devices[phone.id].session
        await asyncio.sleep(3)
        assert not session.closed.is_set(), session.close_reason
        assert len(pongs) >= 3


async def test_devices_ping_answers_with_the_round_trip(make_daemon, short_tmp, api_for, monkeypatch):
    monkeypatch.setattr(core_mod, "PING_TIMEOUT", 0.5)
    delays = {1: 0.2, 2: 0.0}  # by ping req; later pings are never answered

    async def phone(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        while True:
            msg = await ch.recv()
            if msg["t"] == "ping" and msg["req"] in delays:
                await ch.send({"t": "pong", "req": msg["req"] + 100})  # not this ping's
                await asyncio.sleep(delays[msg["req"]])
                await ch.send({"t": "pong", "req": msg["req"]})

    async with RawPhone(phone) as raw:
        daemon = await connect_pinned(make_daemon, short_tmp, raw, keepalive=60)
        api = await api_for(daemon)
        slow, fast = [(await api.call("devices.ping", {"id": raw.id}))["rttMs"] for _ in range(2)]
        assert 200 <= slow < 400 and fast < 100
        with pytest.raises(ApiError) as e:
            await api.call("devices.ping", {"id": raw.id})
        assert (e.value.code, e.value.message) == ("timeout", "no pong within 0.5 s")
        session = daemon.devices[raw.id].session
        ping = asyncio.ensure_future(api.call("devices.ping", {"id": raw.id}))
        await until(lambda: session._pongs)
        await session.close("test: link lost")
        with pytest.raises(ApiError) as e:
            await asyncio.wait_for(ping, 0.3)
        assert e.value.code == "not_connected" and "link lost" in e.value.message
        assert session._pongs == {}


async def test_probe_deadline_follows_a_link_that_turned_slow_at_once(make_daemon, short_tmp, api_for, monkeypatch):
    """The handshake saw a fast link; one pong taking 0.25 s is enough to stretch a 0.1 s action probe."""
    monkeypatch.setattr(session_mod, "PROBE_FLOWING", 0.05)
    delay = 0.25
    pongs = []

    async def turned_slow(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        act = {"id": "x", "kind": "generic", "title": "X", "actions": [{"id": "go", "label": "Go"}]}
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [act]})

        async def answer(msg):
            if msg["t"] == "ping":
                await asyncio.sleep(delay)
                await ch.send({"t": "pong", "req": msg["req"]})
                pongs.append(msg["req"])
            elif msg["t"] == "action":
                await asyncio.sleep(2 * delay)
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

        while True:
            asyncio.ensure_future(answer(await ch.recv()))

    async with RawPhone(turned_slow) as phone:
        daemon = await connect_pinned(
            make_daemon, short_tmp, phone, keepalive=0.5, stall_after=10, dead_after=10, action_probe=0.1
        )
        api = await api_for(daemon)
        await until(lambda: pongs)
        assert await api.call("activities.action", {"device": phone.id, "activity": "x", "action": "go"}) == {
            "ok": True
        }


async def test_late_handshake_answer_does_not_stretch_probe_deadlines(make_daemon, short_tmp, api_for, monkeypatch):
    """The phone's app answered the handshake 1 s late over a fast TCP connect (it was frozen, or a NAT or
    adb forward accepted for it): an action it never answers still drops the session after the action
    probe, not after 4 handshake times."""
    monkeypatch.setattr(session_mod, "PROBE_FLOWING", 0.05)  # the snapshot just before would pass as "flowing"

    async def goes_silent(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        act = {"id": "x", "kind": "generic", "title": "X", "actions": [{"id": "go", "label": "Go"}]}
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [act]})
        while True:
            await ch.recv()

    async with RawPhone(goes_silent, handshake_delay=1.0) as phone:
        daemon = await connect_pinned(
            make_daemon, short_tmp, phone, keepalive=10, stall_after=10, dead_after=10, action_probe=0.2
        )
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].activities)
        session = daemon.devices[phone.id].session
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {"device": phone.id, "activity": "x", "action": "go"})
        assert e.value.code == "failed"
        assert session.closed.is_set() and loop.time() - started < 1.5, session.close_reason


async def test_handshake_deadline_allows_for_a_slow_tcp_connect(make_daemon, api_for, monkeypatch):
    """A TCP connect that took 0.2 s (a round trip) stretches the 0.3 s handshake deadline to 1.1 s."""
    real_open = core_mod.open_tcp

    async def slow_open(host, port):
        await asyncio.sleep(0.2)
        return await real_open(host, port)

    async def unpaired(ch):
        await ch.send({"t": "ready", "paired": False, "now": now_ms()})
        while True:
            await ch.recv()

    monkeypatch.setattr(core_mod, "open_tcp", slow_open)
    async with RawPhone(unpaired, handshake_delay=0.5) as phone:
        daemon = await make_daemon(connect_timeout=0.3)
        api = await api_for(daemon)
        assert (await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port}))["id"] == phone.id


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
        "device": "spoofed",
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


async def test_action_expires_on_the_phone_clock_when_orbitd_stops_waiting(make_daemon, short_tmp, api_for):
    skew = 3_600_000  # phone clock one hour ahead
    seen = []

    async def after(ch):
        await ch.send({"t": "snapshot", "now": now_ms() + skew, "activities": [{"id": "a", "kind": "generic"}]})
        while True:
            msg = await ch.recv()
            if msg["t"] == "action":
                seen.append((msg.get("expiresAt"), now_ms() + skew))
                error = {"error": "expired"} if msg["action"] == "late" else {}
                await ch.send({"t": "action_result", "req": msg["req"], "ok": not error} | error)

    async with RawPhone(lambda ch: paired_script(ch, after)) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone, action_probe=0.8, action_timeout=3.0)
        api = await api_for(daemon)
        await until(lambda: api.call("activities.list"))
        base = {"device": phone.id, "activity": "a"}
        assert await api.call("activities.action", {**base, "action": "go"}) == {"ok": True}
        rtt_ms = daemon.devices[phone.id].session.rtt * 1000
        daemon.action_timeout = 0.4  # below the probe window
        assert await api.call("activities.action", {**base, "action": "late"}) == {
            "ok": False,
            "error": session_mod.EXPIRED,
        }
        daemon.action_timeout, daemon.action_deadline = 3.0, 0.5  # the API call's own bound comes first
        assert await api.call("activities.action", {**base, "action": "go"}) == {"ok": True}
    (probe_at, read1), (timeout_at, read2), (deadline_at, read3) = seen
    assert 700 <= probe_at - read1 <= 800 + 4 * rtt_ms + 50
    assert 300 <= timeout_at - read2 <= 400
    assert 400 <= deadline_at - read3 <= 500


async def test_no_action_expiry_without_the_phone_clock(make_daemon, short_tmp, api_for):
    seen = []

    async def script(ch):
        await ch.send({"t": "ready", "paired": True})
        await ch.recv()
        await ch.send({"t": "snapshot", "activities": [{"id": "a", "kind": "generic"}]})
        while True:
            msg = await ch.recv()
            if msg["t"] == "action":
                seen.append(msg)
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    async with RawPhone(script) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone)
        api = await api_for(daemon)
        await until(lambda: api.call("activities.list"))
        assert await api.call("activities.action", {"device": phone.id, "activity": "a", "action": "go"}) == {
            "ok": True
        }
    assert "expiresAt" not in seen[0]


async def test_action_lost_with_the_session_is_answered_once_it_expired(make_daemon, short_tmp, api_for):
    """The phone could still read the action from a buffer and apply it until it expires."""
    got = asyncio.get_running_loop().create_future()

    async def after(ch):
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "a", "kind": "generic"}]})
        while (await ch.recv())["t"] != "action":
            pass
        if not got.done():
            got.set_result(asyncio.get_running_loop().time())
        # Returning closes the connection with the action unanswered.

    async with RawPhone(lambda ch: paired_script(ch, after)) as phone:
        daemon = await connect_pinned(make_daemon, short_tmp, phone, action_probe=1.5, action_timeout=5.0)
        api = await api_for(daemon)
        await until(lambda: api.call("activities.list"))
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {"device": phone.id, "activity": "a", "action": "go"})
        answered = asyncio.get_running_loop().time()
    assert (e.value.code, e.value.message) == ("failed", session_mod.SENT_THEN_LOST)
    assert answered - await got >= 1.4


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
        ("devices.ping", {"id": dev["id"]}),
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


async def test_unbindable_socket_is_a_startup_error(short_tmp):
    from orbit.daemon.api import StartupError
    from orbit.daemon.core import Daemon

    too_long = short_tmp / ("s" * 200) / "orbitd.sock"
    with pytest.raises(StartupError, match="cannot listen on"):
        await Daemon(short_tmp / "cfg", "D", too_long, mdns=False, ble=False).start()


def test_orbitd_reports_startup_error_cleanly(short_tmp, capsys):
    from orbit.daemon.main import main

    with pytest.raises(SystemExit) as e:
        main(
            [
                "--config-dir",
                str(short_tmp / "cfg"),
                "--socket",
                str(short_tmp / ("s" * 200) / "d.sock"),
                "--no-mdns",
                "--no-ble",
            ]
        )
    assert e.value.code == 1
    assert capsys.readouterr().err.startswith("orbitd: cannot listen on")


async def test_socket_path_that_is_not_a_socket_is_left_alone(short_tmp):
    from orbit.daemon.api import StartupError
    from orbit.daemon.core import Daemon

    notes = short_tmp / "notes.txt"
    notes.write_text("precious")
    with pytest.raises(StartupError, match="is not a socket"):
        await Daemon(short_tmp / "cfg", "D", notes, mdns=False, ble=False).start()
    assert notes.read_text() == "precious"


async def test_unpreparable_socket_directory_is_a_startup_error(short_tmp):
    from orbit.daemon.api import StartupError
    from orbit.daemon.core import Daemon

    (short_tmp / "file").write_text("")
    with pytest.raises(StartupError, match="cannot prepare"):
        await Daemon(short_tmp / "cfg", "D", short_tmp / "file" / "d.sock", mdns=False, ble=False).start()
