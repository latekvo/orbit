"""The daemon <-> frontend local socket itself: restarts, deleted socket, slow/hung clients,
many clients, malformed requests. Real orbitd (+ fakephone where activity traffic matters)."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import time

import pytest
from harness import Proc, bin_path, measure


async def raw(sock, payload: bytes, read_timeout=3.0, close_write=False) -> list:
    r, w = await asyncio.open_unix_connection(str(sock), limit=64 * 1024 * 1024)

    async def send():
        with contextlib.suppress(ConnectionError):
            w.write(payload)
            await w.drain()
            if close_write:
                w.write_eof()

    sender = asyncio.create_task(send())
    out = []
    try:
        while True:
            line = await asyncio.wait_for(r.readline(), read_timeout)
            if not line:
                out.append("EOF")
                break
            out.append(json.loads(line))
    except TimeoutError:
        out.append("TIMEOUT")
    sender.cancel()
    w.close()
    return out


async def test_orbit_watch_across_daemon_restart(paired):
    w = paired
    p = await asyncio.create_subprocess_exec(
        bin_path("orbit"), "--socket", str(w.sock), "watch", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await asyncio.sleep(1.5)
    await w.restart_daemon()
    try:
        code = await asyncio.wait_for(p.wait(), 5)
    except TimeoutError:
        code = "still running"
        p.kill()
        await p.wait()
    err = (await p.stderr.read()).decode()
    measure("watch_restart", exit_code=code, stderr=repr(err))
    # A script consuming `orbit watch` must be able to tell "daemon went away" from a normal end.
    assert code not in (0, "still running") and err


async def test_socket_deleted_under_running_daemon(paired):
    w = paired
    os.unlink(w.sock)
    code, out, err = await w.cli("status")
    # a second orbitd with the same config dir and socket path now starts
    second = await Proc("orbitd2", [bin_path("orbitd"), "--no-mdns", "--no-ble", "--name", "Test Mac",
                                    "--config-dir", str(w.dir / "cfg"), "--socket", str(w.sock)],
                        w.dir / "orbitd2.log").start()
    await asyncio.sleep(4)
    alive2 = second.proc.returncode is None
    sessions = w.phone.log().count("session s")
    phone_conns = w.proxy.accepted
    code2, out2, err2 = await w.cli("status")
    await second.stop()
    measure("socket_deleted", cli_exit=code, cli_err=err.strip(), second_daemon_running=alive2,
            proxy_connections=phone_conns, second_log=second.log()[-400:].replace("\n", " | "))
    # orbitd is running, but every new frontend is told it is not; a second orbitd then runs on
    # the same config dir and also connects to the phone.
    assert not alive2


def _flood(path, n_clients, requests):
    """Blocking sockets with a tiny receive buffer that send every request and never read."""
    import threading

    socks = []
    for _ in range(n_clients):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(str(path))
        socks.append(s)
    threads = [threading.Thread(target=s.sendall, args=(requests,), daemon=True) for s in socks]
    for t in threads:
        t.start()
    return socks


async def test_second_daemon_same_config_dir_other_socket(paired):
    """Single-instance protection is keyed on the socket path, not the config dir: a second orbitd
    started with another --socket (or $ORBIT_SOCKET) runs on the same identity and pins."""
    w = paired
    sock2 = w.dir / "d2.sock"
    second = await Proc("orbitd2", [bin_path("orbitd"), "--no-mdns", "--no-ble", "--name", "Test Mac",
                                    "--config-dir", str(w.dir / "cfg"), "--socket", str(sock2)],
                        w.dir / "orbitd2b.log").start()
    await asyncio.sleep(4)
    alive2 = second.proc.returncode is None
    import re as _re
    live = len(_re.findall(r"session (s\d+) (?:paired|connected)", w.phone.log()))
    closed = w.phone.log().count(" closed (")
    await second.stop()
    measure("second_daemon_other_socket", second_running=alive2, phone_sessions_opened=live, closed=closed)
    assert not alive2


async def test_hung_subscriber_memory(paired):
    """Clients that subscribe and ask for art but never read (a frozen applet asking for album art).
    Does orbitd bound its buffers, free them when it gives up on the client, and keep serving others?"""
    w = paired
    rss0 = w.daemon.rss_kb()
    acts = await w.api("activities.list")
    key = next(a["artKey"] for a in acts if a.get("artKey"))
    art_len = len((await w.api("art.get", {"device": w.phone_id, "key": key}))["data"])
    req = b'{"id":0,"method":"subscribe"}\n' + b"".join(
        json.dumps({"id": i, "method": "art.get", "params": {"device": w.phone_id, "key": key}}).encode() + b"\n"
        for i in range(1, 1 + (48 * 1024 * 1024) // art_len))
    rec = await w.recorder()
    hung = _flood(w.sock, 5, req)
    samples = []
    for _ in range(15):
        await asyncio.sleep(1)
        samples.append(w.daemon.rss_kb() // 1024)
    n_before = len(rec.events)
    await asyncio.sleep(2)
    other_ok = len(rec.events) > n_before
    t = time.monotonic()
    await w.api("status")
    status_latency = time.monotonic() - t
    rss_hung = w.daemon.rss_kb()
    drops = w.daemon.log().count("is not reading")
    await asyncio.sleep(10)  # orbitd "disconnected" them; the clients still hold their end open
    rss_dropped_but_open = w.daemon.rss_kb()
    for s in hung:
        s.close()
    await asyncio.sleep(3)
    rss_after = w.daemon.rss_kb()
    measure("hung_subscriber", art_b64_bytes=art_len, rss0_mb=rss0 // 1024, rss_samples_mb=samples,
            rss_hung_mb=rss_hung // 1024, rss_10s_after_drop_mb=rss_dropped_but_open // 1024,
            rss_after_client_close_mb=rss_after // 1024, other_client_events=other_ok,
            status_latency=round(status_latency, 3), log_drops=drops)
    assert other_ok and status_latency < 1
    # Dropping a client that is not reading must release what was buffered for it.
    assert (rss_dropped_but_open - rss0) // 1024 < 40


async def test_fifty_clients_subscribe_at_once(paired):
    w = paired
    fds0 = w.daemon.fds()

    async def one():
        r, wr = await asyncio.open_unix_connection(str(w.sock), limit=64 * 1024 * 1024)
        t = time.monotonic()
        wr.write(b'{"id":1,"method":"subscribe"}\n')
        await wr.drain()
        got = set()
        while len(got) < 4:
            msg = json.loads(await asyncio.wait_for(r.readline(), 10))
            got.add(msg.get("event") or "result")
        return time.monotonic() - t, (r, wr)

    res = await asyncio.gather(*(one() for _ in range(50)))
    lats = sorted(x for x, _ in res)
    fds_open = w.daemon.fds()
    for _, (r, wr) in res:
        wr.close()
    await asyncio.sleep(1)
    fds_after = w.daemon.fds()
    measure("fifty_clients", p50=round(lats[25], 3), max=round(lats[-1], 3), fds0=fds0, fds_open=fds_open,
            fds_after=fds_after)
    assert lats[-1] < 2 and fds_after <= fds0 + 2


MALFORMED = [
    ("invalid_json", b"{nope\n"),
    ("not_object", b"[1,2]\n"),
    ("invalid_utf8", b'{"id":1,"method":"st\xffatus"}\n'),
    ("nul", b'{"id":1,"method":"status\x00"}\n'),
    ("unknown_method", b'{"id":1,"method":"rm -rf"}\n'),
    ("params_list", b'{"id":1,"method":"status","params":[1]}\n'),
    ("id_object", b'{"id":{"a":1},"method":"status"}\n'),
    ("id_list", b'{"id":[1],"method":"status"}\n'),
    ("id_float", b'{"id":1.5,"method":"status"}\n'),
    ("id_bigint", b'{"id":123456789012345678901234567890,"method":"status"}\n'),
    ("no_method", b'{"id":1}\n'),
    ("action_bad_types", b'{"id":1,"method":"activities.action","params":{"device":1,"activity":[],"action":{}}}\n'),
    ("connect_bad_port", b'{"id":1,"method":"devices.connect","params":{"host":"x","port":99999}}\n'),
    ("connect_bool_port", b'{"id":1,"method":"devices.connect","params":{"host":"127.0.0.1","port":true}}\n'),
    ("connect_id_int", b'{"id":1,"method":"devices.connect","params":{"id":5}}\n'),
    ("pair_respond_bad", b'{"id":1,"method":"pairing.respond","params":{"session":"x","accept":"yes"}}\n'),
    ("deep_nesting", b'{"id":1,"method":"status","params":{"a":' + b"[" * 100000 + b"]" * 100000 + b"}}\n"),
    ("nan", b'{"id":NaN,"method":"status"}\n'),
    ("no_newline_then_eof", b'{"id":1,"method":"status"}'),
    ("newline_then_eof", b'{"id":1,"method":"status"}\n'),
]


@pytest.mark.parametrize("name,payload", MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_malformed_request(paired, name, payload):
    w = paired
    out = await raw(w.sock, payload, close_write=name.endswith("then_eof"))
    alive = w.daemon.proc.returncode is None
    st = await w.api("status")
    tb = w.daemon.log().count("Traceback")
    measure(f"malformed_{name}", reply=json.dumps(out[0])[:200] if out else None, daemon_alive=alive,
            tracebacks=tb)
    assert alive and st["name"] == "Test Mac"
    if name == "no_newline_then_eof":  # out of spec (no newline): dropping it is fine
        return
    if name == "connect_bool_port":
        assert out[0]["error"]["code"] == "bad_request", "port=true was accepted as port 1"
    assert out and isinstance(out[0], dict) and ("error" in out[0] or "result" in out[0])
    assert tb == 0


async def test_oversized_request(paired):
    """A request line over the 16 MiB limit: bad_request, then the connection is closed."""
    w = paired
    big = b'{"id":1,"method":"status","params":{"x":"' + b"a" * (17 * 1024 * 1024) + b'"}}\n'

    def run():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(w.sock))
        s.settimeout(10)
        with contextlib.suppress(OSError):
            s.sendall(big)
        data = b""
        with contextlib.suppress(OSError):
            while chunk := s.recv(65536):
                data += chunk
        s.close()
        return data

    t = time.monotonic()
    data = await asyncio.to_thread(run)
    lat = time.monotonic() - t
    st = await w.api("status")
    measure("oversized", reply=data[:200], latency=round(lat, 2), alive=st["name"] == "Test Mac")
    assert json.loads(data.splitlines()[0])["error"]["code"] == "bad_request"


async def test_huge_action_value(paired):
    """A 5 MB value is forwarded to the phone? (handshake frames are 4096 B; session frames bounded)"""
    w = paired
    acts = await w.api("activities.list")
    mid = next(a["id"] for a in acts if a["kind"] == "media")
    t = time.monotonic()
    try:
        res, err = await w.api("activities.action", {"device": w.phone_id, "activity": mid, "action": "media.seek",
                                                     "value": "x" * (5 * 1024 * 1024)}), None
    except Exception as e:
        res, err = None, (getattr(e, "code", None), getattr(e, "message", str(e)))
    lat = time.monotonic() - t
    await asyncio.sleep(1)
    st = (await w.device())["state"]
    measure("huge_action_value", result=res, error=err, latency=round(lat, 2), state_after=st)
    assert st == "connected"


async def test_cli_against_frozen_daemon(paired):
    w = paired
    w.daemon.send_signal(signal.SIGSTOP)
    try:
        t = time.monotonic()
        code, out, err = await w.cli("status", timeout=45)
        lat = time.monotonic() - t
    finally:
        w.daemon.send_signal(signal.SIGCONT)
    measure("cli_frozen_daemon", exit=code, latency=round(lat, 1), err=err.strip()[:200])
    assert code != -999, "orbit status hung for 45 s against a stopped orbitd"


async def test_cli_action_during_reconnecting_and_offline(paired):
    w = paired
    acts = await w.api("activities.list")
    mid = next(a["id"] for a in acts if a["kind"] == "media")
    await w.proxy.refuse()
    w.proxy.reset()
    await asyncio.sleep(0.2)
    c1 = await w.cli("action", w.phone_id[:8], mid, "media.next")
    c_act = await w.cli("activities")
    c_status = await w.cli("status")
    await asyncio.sleep(16)
    c2 = await w.cli("action", w.phone_id[:8], mid, "media.next")
    measure("cli_reconnecting", action=c1, activities_head=c_act[1][:300], status=c_status[1])
    measure("cli_offline", action=c2)
    # `orbit activities` during reconnecting must not present stale data as live
    assert "stale" in c_act[1].lower() or "reconnecting" in c_act[1].lower()
