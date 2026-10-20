# ruff: noqa: E501
"""The same raw-initiator probes against the Kotlin responder and the Python reference responder
(orbit-fakephone), to surface behavioural differences at the connectivity edges."""

import asyncio
import contextlib
import signal
import struct
import sys
import time
from pathlib import Path

import pytest

from orbit.proto import FrameStream, KeyPair, initiate

from .harness import CLASSPATH_FILE, MEDIA, JvmPhone, Proc, free_port, log

IMPLS = ["kotlin", "python"]


class PyPhone(Proc):
    def __init__(self, workdir: Path) -> None:
        self.port = free_port()
        argv = [
            str(Path(sys.executable).parent / "orbit-fakephone"),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-mdns",
            "--auto-accept",
            "--config-dir",
            str(workdir / "pyphone"),
            "--tick",
            "1",
        ]
        super().__init__("pyphone", argv, workdir / "pyphone.log")

    async def start(self, stdin: bool = False) -> None:
        await super().start()
        await self.wait_line(lambda t: "listening on" in t, 20)

    def executed(self, since: float) -> list[float]:
        return [ts for ts, t in self.lines if ts >= since and " action " in t and " -> " in t and "(from " in t]


@pytest.fixture
async def responder(workdir):
    made = []

    async def make(impl):
        if impl == "kotlin":
            if not CLASSPATH_FILE.exists():
                pytest.skip("JVM harness not built")
            p = JvmPhone(workdir, auto_accept=True)
            await p.start()
            await p.cmd("upsert", activity=MEDIA)
            p.executed = lambda since: [ts for ts, _ in p.events("action", since)]
        else:
            p = PyPhone(workdir)
            await p.start()
        made.append(p)
        return p

    yield make
    for p in made:
        await p.stop()


async def closed_after(reader, limit: float) -> float | None:
    t0 = time.monotonic()
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), limit - (time.monotonic() - t0))
            if not data:
                return time.monotonic() - t0
    except (TimeoutError, ValueError):
        return None
    except ConnectionError:
        return time.monotonic() - t0


async def connect(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return reader, writer, FrameStream(reader, writer)


async def paired_channel(port, key):
    reader, writer, stream = await connect(port)
    ch = await initiate(stream, key, "Probe", "linux")
    await ch.send({"t": "ready", "paired": False, "now": int(time.time() * 1000)})
    await ch.send({"t": "pair_request"})
    await ch.send({"t": "pair_decision", "accepted": True})
    snapshot = None
    while snapshot is None:
        m = await asyncio.wait_for(ch.recv(), 10)
        if m["t"] == "snapshot":
            snapshot = m
        elif m["t"] == "ping":
            await ch.send({"t": "pong", "req": m.get("req")})
    return reader, writer, ch, snapshot


@pytest.mark.parametrize("impl", IMPLS)
async def test_probe_silent_tcp_connection(responder, impl):
    """TCP open, nothing sent: how long until the responder hangs up?"""
    p = await responder(impl)
    reader, writer, _ = await connect(p.port)
    dt = await closed_after(reader, 45)
    writer.close()
    log(f"[{impl}] silent connection closed after {dt if dt is None else round(dt, 1)} s")


@pytest.mark.parametrize("impl", IMPLS)
async def test_probe_unpaired_idle(responder, impl):
    """Handshake + ready, then only pongs: when and how does the responder end it?"""
    p = await responder(impl)
    reader, writer, stream = await connect(p.port)
    ch = await initiate(stream, KeyPair.generate(), "Probe", "linux")
    await ch.send({"t": "ready", "paired": False, "now": 0})
    t0 = time.monotonic()
    last = None
    with contextlib.suppress(Exception):
        while time.monotonic() - t0 < 45:
            m = await asyncio.wait_for(ch.recv(), 45)
            last = m
            if m["t"] == "ping":
                await ch.send({"t": "pong", "req": m.get("req")})
    log(f"[{impl}] unpaired session ended after {time.monotonic() - t0:.1f}s, last message {last}")
    writer.close()


@pytest.mark.parametrize("impl", IMPLS)
async def test_probe_pair_request_before_ready(responder, impl):
    p = await responder(impl)
    reader, writer, stream = await connect(p.port)
    ch = await initiate(stream, KeyPair.generate(), "Probe", "linux")
    await ch.send({"t": "pair_request"})
    got = []
    with contextlib.suppress(Exception):
        while len(got) < 3:
            got.append(await asyncio.wait_for(ch.recv(), 3))
    log(f"[{impl}] pair_request before ready -> {[m.get('t') + ('/' + m['code'] if 'code' in m else '') for m in got]}")
    writer.close()


@pytest.mark.parametrize("impl", IMPLS)
async def test_probe_any_byte_keeps_session_alive(responder, impl):
    """spec 5: 'close after 30 s without receiving anything (any byte counts, so a large frame still
    arriving keeps the session alive)'. Trickle one frame at 1 byte / 2 s for 50 s."""
    p = await responder(impl)
    reader, writer, ch, _ = await paired_channel(p.port, KeyPair.generate())
    writer.write(struct.pack(">I", 4000))
    await writer.drain()
    t0 = time.monotonic()
    alive = True
    while time.monotonic() - t0 < 50:
        try:
            writer.write(b"\x00")
            await writer.drain()
        except ConnectionError:
            alive = False
            break
        with contextlib.suppress(TimeoutError):
            data = await asyncio.wait_for(reader.read(65536), 2)
            if data == b"":
                alive = False
                break
    dt = time.monotonic() - t0
    log(f"[{impl}] trickled frame: session {'alive' if alive else 'closed'} after {dt:.1f}s")
    writer.close()
    assert alive, f"{impl} responder closed a session whose peer was still sending bytes"


@pytest.mark.parametrize("impl", IMPLS)
async def test_probe_frozen_responder_runs_stale_action(responder, impl):
    """Responder frozen (SIGSTOP), action sent, initiator gives up and closes at 30 s (like orbitd),
    responder thawed at 40 s: is the abandoned action executed?"""
    p = await responder(impl)
    reader, writer, ch, snap = await paired_channel(p.port, KeyPair.generate())
    acts = snap["activities"]
    target = next(a for a in acts if a.get("actions"))
    action = next(x["id"] for x in target["actions"] if x["id"] not in ("open", "dismiss"))
    t0 = time.monotonic()
    p.signal(signal.SIGSTOP)
    try:
        await ch.send({"t": "action", "req": 1, "activity": target["id"], "action": action})
        await asyncio.sleep(30)
        writer.close()
        await asyncio.sleep(10)
    finally:
        p.signal(signal.SIGCONT)
    await asyncio.sleep(3)
    ran = [round(ts - t0, 1) for ts in p.executed(t0)]
    log(
        f"[{impl}] abandoned action {target['id']}/{action}: executed at {ran} s (initiator closed at 30 s, thaw at 40 s)"
    )
    assert not ran
