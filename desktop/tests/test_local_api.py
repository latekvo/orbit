"""The local API socket and its clients under misuse: other orbitds, stuck daemons, hostile clients."""

import asyncio
import json
import os
import signal
import socket
import sys
import time

import pytest
from daemon_helpers import until
from test_cli import orbit

from orbit.client import ApiError, LocalClient
from orbit.daemon import api as api_mod
from orbit.daemon.api import AlreadyRunning
from orbit.daemon.core import Daemon


async def test_second_daemon_on_the_same_config_dir_is_refused(make_daemon, short_tmp):
    """Another socket path (e.g. $ORBIT_SOCKET) must not get a second orbitd on the same identity."""
    await make_daemon()
    other = Daemon(short_tmp / "daemon", "Other", short_tmp / "other.sock", mdns=False, ble=False)
    with pytest.raises(AlreadyRunning):
        await other.start()
    assert not (short_tmp / "other.sock").exists()


async def test_deleted_socket_is_recreated(make_daemon, monkeypatch):
    monkeypatch.setattr(api_mod, "SOCKET_CHECK", 0.1, raising=False)
    daemon = await make_daemon()
    os.unlink(daemon.api.path)
    await until(daemon.api.path.exists, timeout=3)
    client = await LocalClient.connect(daemon.api.path)
    try:
        assert (await client.call("status"))["name"] == "Test Laptop"
    finally:
        await client.close()


async def test_bool_port_and_deep_nesting_are_bad_requests(make_daemon, api_for):
    daemon = await make_daemon()
    api = await api_for(daemon)
    with pytest.raises(ApiError) as e:
        await api.call("devices.connect", {"host": "127.0.0.1", "port": True})
    assert e.value.code == "bad_request"
    reader, writer = await asyncio.open_unix_connection(str(daemon.api.path), limit=1 << 24)
    writer.write(b"[" * 100_000 + b"]" * 100_000 + b"\n")
    reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
    writer.close()
    assert reply["error"]["code"] == "bad_request"
    assert (await api.call("status"))["name"] == "Test Laptop"  # still serving


async def test_client_that_never_reads_does_not_starve_others(make_daemon, api_for):
    daemon = await make_daemon()
    api = await api_for(daemon)
    flooders = []
    request = (json.dumps({"id": 1, "method": "devices.list"}) + "\n").encode() * 200_000
    for _ in range(3):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(str(daemon.api.path))
        s.setblocking(False)
        flooders.append(s)
    loop = asyncio.get_running_loop()
    senders = [loop.create_task(loop.sock_sendall(s, request)) for s in flooders]
    await asyncio.sleep(1.0)
    t0 = time.monotonic()
    assert (await asyncio.wait_for(api.call("status"), 5))["name"] == "Test Laptop"
    latency = time.monotonic() - t0
    in_flight = len(daemon.api._tasks)
    for t in senders:
        t.cancel()
    for s in flooders:
        s.close()
    print(f"status answered in {latency:.2f} s with {in_flight} requests in flight")
    assert latency < 0.5 and in_flight <= 200  # requests are not read faster than they are answered


async def test_cli_times_out_on_a_stopped_daemon(make_daemon, short_tmp, monkeypatch):
    """A SIGSTOPped orbitd accepts connections (the kernel does) but never answers."""
    sock = short_tmp / "stopped.sock"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from orbit.daemon.main import main; main()",
        "--no-mdns",
        "--no-ble",
        "--config-dir",
        str(short_tmp / "stopped"),
        "--socket",
        str(sock),
    )
    try:
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.05)
        os.kill(proc.pid, signal.SIGSTOP)
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import orbit.client as c; c.CALL_TIMEOUT = 1.0; from orbit.cli import main; main()",
            "--socket",
            str(sock),
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(cli.communicate(), 10)
    finally:
        os.kill(proc.pid, signal.SIGCONT)
        proc.terminate()
        await proc.wait()
    assert cli.returncode == 2 and "did not answer" in out.decode()


async def test_watch_fails_when_orbitd_goes_away(make_daemon):
    daemon = await make_daemon()
    watch = asyncio.ensure_future(orbit(daemon, "watch", "--json"))
    await asyncio.sleep(1.0)
    await daemon.api.stop()
    code, out = await watch
    assert code == 2 and "orbitd closed the connection" in out


def test_stale_activities_are_marked():
    from orbit.cli import describe_activity

    act = {"id": "media:x", "device": "d" * 32, "kind": "media", "title": "Song"}
    assert "stale" not in describe_activity(act, 0, "Pixel")[0]
    assert "stale" in describe_activity({**act, "stale": True}, 0, "Pixel")[0]
