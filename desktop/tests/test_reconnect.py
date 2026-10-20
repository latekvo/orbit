"""Reconnection policy and route choice against real TCP phones (spec/LOCAL_API.md, Connecting)."""

import asyncio
import socket
import time

import pytest
from daemon_helpers import RawPhone, pin_in, until

from orbit.client import ApiError
from orbit.daemon import core
from orbit.daemon.timeconv import now_ms
from orbit.proto import KeyPair, device_id
from orbit.store import PeerStore

ACT = {
    "id": "media:x",
    "kind": "media",
    "title": "Song",
    "updatedMs": 1_700_000_000_000,
    "actions": [{"id": "media.pause", "label": "Pause"}],
}


class Phone(RawPhone):
    """A paired phone that sends its snapshot, answers pings and actions, and can be told to drop."""

    def __init__(self, identity=None, **kw):
        self.drops = asyncio.Event()
        self.actions = []
        self.silent: bool | str = False  # reads, but sends nothing (frozen app, kernel still acknowledging)
        super().__init__(self._script, identity=identity, **kw)

    async def _script(self, ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": now_ms(), "activities": [ACT]})
        reader = asyncio.ensure_future(self._serve(ch))
        dropped = asyncio.ensure_future(self.drops.wait())
        await asyncio.wait([reader, dropped], return_when=asyncio.FIRST_COMPLETED)
        reader.cancel()
        dropped.cancel()

    async def _serve(self, ch):
        while True:
            msg = await ch.recv()
            if self.silent == "after one frame":  # e.g. an update it sent as the link died
                self.silent = True
                await ch.send({"t": "activity", "activity": ACT})
            if self.silent:
                continue
            if msg["t"] == "ping":
                await ch.send({"t": "pong", "req": msg["req"]})
            elif msg["t"] == "action":
                self.actions.append(msg["action"])
                await ch.send({"t": "action_result", "req": msg["req"], "ok": True})

    def drop(self):
        """Close every current connection (as if the Wi-Fi blipped); new ones are accepted."""
        self.drops.set()
        self.drops = asyncio.Event()


def session_of(daemon, dev_id):
    dev = daemon.devices.get(dev_id)
    return dev.session if dev else None


async def test_sessions_that_carried_data_reconnect_at_once_every_time(make_daemon, short_tmp):
    """A link that drops every few seconds: each reconnect succeeds on its first try, so none may wait."""
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: session_of(daemon, phone.id))
        gaps = []
        for _ in range(6):
            old = session_of(daemon, phone.id)
            await until(lambda old=old: daemon.devices[phone.id].activities and not daemon.devices[phone.id].stale)
            t0 = time.monotonic()
            phone.drop()
            await until(lambda old=old: session_of(daemon, phone.id) not in (None, old), timeout=20)
            gaps.append(time.monotonic() - t0)
        assert max(gaps) < 0.5, gaps


async def test_manual_address_phone_back_after_an_outage_is_reconnected_quickly(make_daemon, short_tmp):
    """No discovery for a manual address: the retry cadence alone must find the phone soon."""
    identity = KeyPair.generate()
    async with Phone(identity) as phone:
        port = phone.port
        pin_in(short_tmp / "daemon", identity, address=f"127.0.0.1:{port}")
        daemon = await make_daemon()
        await until(lambda: session_of(daemon, phone.id))
        phone._server.close()
        phone.drop()
        await asyncio.sleep(9)  # several failed attempts
        assert session_of(daemon, phone.id) is None
        assert daemon.devices[phone.id].grace is not None  # still within the stale grace period
        server = await asyncio.start_server(phone._on_conn, "127.0.0.1", port)
        t0 = time.monotonic()
        try:
            await until(lambda: session_of(daemon, phone.id), timeout=20)
            latency = time.monotonic() - t0
        finally:
            server.close()
    assert latency < 2.5, latency


async def test_a_blackholed_address_does_not_hold_up_a_working_one(make_daemon, short_tmp, api_for):
    async with Phone() as phone:
        daemon = await make_daemon()
        api = await api_for(daemon)
        pin_in(short_tmp / "daemon", phone.identity)
        daemon.store.peers = PeerStore(short_tmp / "daemon").peers
        dev = daemon._device(phone.id)
        dev.auto = False  # only the explicit connect below
        # 10.255.255.1 does not answer (no RST): only a timeout ends that attempt. It sorts first.
        addrs = [("10.255.255.1", 47820), ("10.255.255.2", 47820), ("127.0.0.1", phone.port)]
        daemon.on_lan("P._orbit._tcp.local.", {"id": phone.id, "name": "P", "platform": "android", "addresses": addrs})
        t0 = time.monotonic()
        assert await api.call("devices.connect", {"id": phone.id}) == {"id": phone.id}
        assert time.monotonic() - t0 < 1.5
        assert session_of(daemon, phone.id).address == f"127.0.0.1:{phone.port}"


def test_advertised_addresses_are_ordered_numerically_ipv4_first():
    addrs = ["fe80::1%en0", "10.0.0.200", "2001:db8::5", "10.0.0.30", "169.254.1.1"]
    assert sorted(addrs, key=core.address_order) == [
        "10.0.0.30",
        "10.0.0.200",
        "2001:db8::5",
        "169.254.1.1",
        "fe80::1%en0",
    ]


def test_tcp_connect_timeout_allows_slow_links():
    import inspect

    assert inspect.signature(core.Daemon).parameters["tcp_timeout"].default >= 8.0


async def test_last_working_lan_address_is_used_without_mdns(make_daemon, short_tmp):
    """Paired over a discovered address; after a restart with mDNS silent, that address still works."""
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity)
        daemon = await make_daemon()
        daemon.on_lan(
            "P._orbit._tcp.local.",
            {"id": phone.id, "name": "P", "platform": "android", "addresses": [("127.0.0.1", phone.port)]},
        )
        await until(lambda: session_of(daemon, phone.id))
        await daemon.stop()
        assert PeerStore(short_tmp / "daemon").get(phone.id).last_lan == f"127.0.0.1:{phone.port}"
        again = await make_daemon()  # nothing announced: only the remembered address
        await until(lambda: session_of(again, phone.id), timeout=5)


async def test_disconnect_while_an_attempt_is_under_way_wins(make_daemon, short_tmp, api_for):
    gate = asyncio.Event()

    async def slow_ready(ch):
        await gate.wait()
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await ch.recv()
        await asyncio.sleep(30)

    async with RawPhone(slow_ready) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: phone._writers)  # the handshake is under way, waiting for our ready
        assert await api.call("devices.disconnect", {"id": phone.id}) is True
        gate.set()
        await asyncio.sleep(0.5)
        assert session_of(daemon, phone.id) is None
        assert (await api.call("devices.list"))[0]["state"] == "offline"


async def test_reset_phone_at_the_remembered_address_is_reported(make_daemon, short_tmp, api_for):
    """The pinned phone was reset: its address now answers with a new identity."""

    async def other(ch):
        await ch.send({"t": "ready", "paired": False, "now": now_ms()})
        await asyncio.sleep(30)

    async with RawPhone(other, name="Pixel") as impostor:
        old = KeyPair.generate()
        pin_in(short_tmp / "daemon", old, name="Pixel", address=f"127.0.0.1:{impostor.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        (dev,) = await until(lambda: _with_problem(api))
        assert "answers as a different device" in dev["problem"] and dev["paired"]


async def _with_problem(api):
    devices = await api.call("devices.list")
    return [d for d in devices if d.get("problem")]


async def test_phone_that_forgot_this_computer_stays_listed_with_the_reason(make_daemon, short_tmp, api_for):
    async def forgot(ch):
        await ch.send({"t": "ready", "paired": False, "now": now_ms()})
        await asyncio.sleep(30)

    async with RawPhone(forgot) as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        (dev,) = await until(lambda: _with_problem(api))
        assert not dev["paired"] and "no longer trusts this computer" in dev["problem"]
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {"device": phone.id, "activity": "a", "action": "b"})
        assert e.value.code == "not_connected"


async def test_pairing_timeout_says_bye(make_daemon, api_for):
    got = []

    async def waits(ch):
        await ch.send({"t": "ready", "paired": False, "now": now_ms()})
        while True:
            got.append(await ch.recv())

    async with RawPhone(waits) as phone:
        daemon = await make_daemon(pairing_timeout=0.3)
        api = await api_for(daemon)
        await api.call("devices.connect", {"host": "127.0.0.1", "port": phone.port})
        await until(lambda: any(m["t"] == "bye" for m in got))
        assert {"t": "bye", "reason": "pairing timed out"} in got


async def test_not_an_orbit_responder_is_explained(make_daemon, api_for):
    async def http(reader, writer):
        await reader.read(100)
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    daemon = await make_daemon()
    api = await api_for(daemon)
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"host": "127.0.0.1", "port": port})
    server.close()
    assert "not an Orbit phone" in e.value.message or "closed the connection during the handshake" in e.value.message


async def test_action_right_after_wake_goes_to_the_fresh_session(make_daemon, short_tmp, api_for, monkeypatch):
    """The computer slept; the phone dropped the session meanwhile without the laptop noticing.

    The first click after wake must not go out on the dead session and fail as 'may have been applied'.
    """
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: session_of(daemon, phone.id) and daemon.devices[phone.id].activities)
        old = session_of(daemon, phone.id)
        # The phone gave up on the session during the sleep, but its FIN was lost: it no longer reads.
        for w in phone._writers:
            w.transport.pause_reading()
        phone._server.close()
        server = await asyncio.start_server(phone._on_conn, "127.0.0.1", phone.port)
        slept = time.clock_gettime(time.CLOCK_MONOTONIC)
        sleep_clock = lambda: slept + 120 + (time.monotonic() - t_mono)  # noqa: E731
        monkeypatch.setattr(core, "_sleep_clock", sleep_clock, raising=False)
        t_mono = time.monotonic()
        try:
            result = await api.call(
                "activities.action", {"device": phone.id, "activity": "media:x", "action": "media.pause"}
            )
            fresh = session_of(daemon, phone.id)
        finally:
            server.close()
    assert result == {"ok": True}
    assert old.closed.is_set() and fresh is not old and phone.actions == ["media.pause"]


async def test_wake_marks_activities_stale_until_the_session_answers(make_daemon, short_tmp, api_for, monkeypatch):
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].activities)
        session = session_of(daemon, phone.id)
        daemon._woke(60)
        assert [a["stale"] for a in await api.call("activities.list")] == [True]
        await until(lambda: not daemon.devices[phone.id].stale, timeout=3)
        assert session_of(daemon, phone.id) is session  # it answered: kept


def test_clock_step_moves_converted_times(monkeypatch):
    from orbit.daemon.timeconv import shift_local

    a = {"updatedMs": 1000, "chrono": {"baseMs": 5000}, "media": {"positionAtMs": 7000, "positionMs": 3}}
    shift_local(a, 250)
    assert (a["updatedMs"], a["chrono"]["baseMs"], a["media"]["positionAtMs"]) == (1250, 5250, 7250)
    assert a["media"]["positionMs"] == 3


async def test_clock_step_is_detected_and_applied(make_daemon, short_tmp, monkeypatch):
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        await until(lambda: daemon.devices[phone.id].activities)
        before = daemon.devices[phone.id].activities["media:x"]["updatedMs"]
        offset = session_of(daemon, phone.id).offset
        daemon.check_clocks()
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 3600)
        daemon.check_clocks()
        after = daemon.devices[phone.id].activities["media:x"]["updatedMs"]
        assert abs(after - before - 3_600_000) < 1000
        assert abs(session_of(daemon, phone.id).offset - offset - 3_600_000) < 1000


def test_txt_version_lists_are_understood():
    from orbit.daemon.discovery import supported_version

    assert supported_version("1") and supported_version("1,2") and supported_version(" 2, 1")
    assert not supported_version("2") and not supported_version(None) and not supported_version("")


async def test_old_pin_of_a_phone_paired_again_is_dropped(make_daemon, short_tmp, api_for):
    """Reset phone, paired again under its new identity: its old address answers as the new pin."""

    async def again(ch):
        await ch.send({"t": "ready", "paired": True, "now": now_ms()})
        await asyncio.sleep(30)

    async with RawPhone(again, name="Pixel") as phone:
        old = KeyPair.generate()
        pin_in(short_tmp / "daemon", old, name="Pixel", address=f"127.0.0.1:{phone.port}")
        pin_in(short_tmp / "daemon", phone.identity, name="Pixel")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: _ids(api, {phone.id}))
        assert PeerStore(short_tmp / "daemon").get(device_id(old.public)) is None


async def _ids(api, expected):
    return {d["id"] for d in await api.call("devices.list")} == expected


async def test_bytes_the_kernel_holds_may_have_reached_the_phone():
    """Unacknowledged is not undelivered: the phone may have the bytes and only its ACKs were lost."""
    from orbit.daemon.session import WatchedWriter

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    held = []
    server = await asyncio.start_server(lambda r, w: held.append(w), sock=listener)  # never reads
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    sock.connect(listener.getsockname())
    _, writer = await asyncio.open_connection(sock=sock)
    try:
        tx = WatchedWriter(writer)
        tx.write(b"x" * 1_000_000)
        await asyncio.sleep(0.2)
        buffered = writer.transport.get_write_buffer_size()
        assert 0 < buffered < tx.written  # some bytes in the kernel, unacknowledged once its window filled
        assert tx.delivered() == tx.written - buffered
    finally:
        writer.transport.abort()
        server.close()


@pytest.mark.parametrize("silent", [True, "after one frame"])
async def test_link_that_does_not_answer_an_action_is_dropped_within_5_s(make_daemon, short_tmp, api_for, silent):
    """A frame that was on its way when the link died does not count as an answer."""
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{phone.port}")
        daemon = await make_daemon()
        api = await api_for(daemon)
        await until(lambda: daemon.devices[phone.id].activities)
        old = session_of(daemon, phone.id)
        phone.silent = silent
        t0 = time.monotonic()
        with pytest.raises(ApiError) as e:
            await api.call("activities.action", {"device": phone.id, "activity": "media:x", "action": "media.pause"})
        assert time.monotonic() - t0 < 6.5 and "may" in e.value.message
        assert old.closed.is_set()


async def test_new_address_announced_checks_the_session_on_the_old_one(make_daemon, short_tmp):
    """DHCP renew / roam: the record now lists the new address while the cache still holds the old one."""
    async with Phone() as phone:
        pin_in(short_tmp / "daemon", phone.identity)
        daemon = await make_daemon()
        rec = {"id": phone.id, "name": "Pixel", "platform": "android", "addresses": [("127.0.0.1", phone.port)]}
        daemon.on_lan("Pixel._orbit._tcp.local.", rec)
        await until(lambda: daemon.devices[phone.id].activities)
        old = session_of(daemon, phone.id)
        phone.silent = True  # its old address is gone
        moved = {**rec, "addresses": [("127.0.0.1", phone.port), ("127.0.0.2", phone.port)]}
        daemon.on_lan("Pixel._orbit._tcp.local.", moved)
        await until(lambda: old.closed.is_set(), timeout=core.PROBE_TIMEOUT + 1.5)


def test_link_local_addresses_get_an_interface_to_try():
    from orbit.daemon.discovery import LanDiscovery

    lan = LanDiscovery(lambda *_: None)
    lan._interfaces = frozenset({("eth0", "fe80::1"), ("wlan0", "fe80::2"), ("lo", "fe80::1"), ("eth0", "10.0.0.2")})
    scoped = lan._scoped(["10.0.0.9", "fe80::9", "fe80::8%3"])
    assert scoped == ["10.0.0.9", "fe80::9%eth0", "fe80::9%wlan0", "fe80::8%3"]


async def test_silent_manual_address_is_reported_until_the_phone_answers(make_daemon, short_tmp, api_for, monkeypatch):
    """A manually added phone that got a new address (no mDNS to find it) must not just sit offline."""
    monkeypatch.setattr(core, "MANUAL_SILENT_FOR", 0.5)
    async with Phone() as phone:
        port = phone.port
    pin_in(short_tmp / "daemon", phone.identity, address=f"127.0.0.1:{port}")
    daemon = await make_daemon()
    api = await api_for(daemon)
    (dev,) = await until(lambda: _with_problem(api), timeout=10)
    assert dev["problem"].startswith(f"nothing answers at 127.0.0.1:{port}")
    back = Phone(identity=phone.identity)
    back._server = await asyncio.start_server(back._on_conn, "127.0.0.1", port)
    try:
        await until(lambda: session_of(daemon, phone.id), timeout=10)
        assert [d["problem"] for d in await api.call("devices.list")] == [None]
    finally:
        await back.__aexit__(None, None, None)


async def _with_problem(api):
    return [d for d in await api.call("devices.list") if d["problem"]]
