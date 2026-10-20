"""Keepalive boundaries (spec: ping after 10 s without sending, close after 30 s without receiving)."""

import asyncio
import signal
import time

import pytest
from harness import Orbitd, pair

from orbit.proto import FrameStream, KeyPair, device_id, respond
from orbit.store import PeerStore, PinnedPeer


async def test_peer_on_old_20_60_stays_connected(lab):
    d = await lab.daemon()
    p = await lab.phone(extra=["--keepalive", "20", "--dead-after", "60"])
    api = await lab.api(d)
    dev = await pair(api, p)
    t0 = time.monotonic()
    await asyncio.sleep(150)
    drops = d.grep("session_closed", t0)
    pings_from_laptop = len(p.grep(r"<- s\d+ ping", t0))
    pongs_from_phone = len(p.grep(r"-> s\d+ pong", t0))
    print(
        f"\nold 20/60 peer, 150 s: drops {len(drops)}; laptop pings {pings_from_laptop}, phone pongs {pongs_from_phone}"
    )
    assert not drops and (await api.device(dev))["connected"]


class SilentPinger:
    """Pinned responder that never answers pings and sends its own ping every ``every`` seconds."""

    def __init__(self, every: float, laptop_static: bytes) -> None:
        self.every = every
        self.identity = KeyPair.generate()
        self.laptop_static = laptop_static
        self.received: list[tuple[float, str]] = []
        self.closed_at: float | None = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _conn(self, r, w):
        stream = FrameStream(r, w)
        ch = await respond(stream, self.identity, "Silent", "android")
        await ch.send({"t": "ready", "paired": True, "now": int(time.time() * 1000)})
        await ch.recv()
        await ch.send({"t": "snapshot", "now": int(time.time() * 1000), "activities": []})

        async def pinger():
            n = 0
            while True:
                await asyncio.sleep(self.every)
                n += 1
                await ch.send({"t": "ping", "req": n})

        task = asyncio.create_task(pinger())
        try:
            while True:
                msg = await ch.recv()
                self.received.append((time.monotonic(), msg["t"]))
        except Exception:
            self.closed_at = time.monotonic()
        finally:
            task.cancel()
            w.close()


@pytest.mark.parametrize("every,survives", [(5, True), (25, False), (31, False)])
async def test_peer_that_never_answers_our_pings(lab, every, survives):
    """Our ping must draw some byte within 10 s (spec 2, ping); the peer's own pings count as bytes."""
    d = Orbitd(lab.root)
    lab.procs.append(d)
    async with SilentPinger(every, b"") as peer:
        pid = device_id(peer.identity.public)
        PeerStore(d.config).pin(PinnedPeer(pid, "Silent", "android", peer.identity.public, f"127.0.0.1:{peer.port}"))
        await d.start()
        api = await lab.api(d)
        t0 = await api.wait_state(pid, "connected", 10)
        try:
            t_drop = await api.wait_state(pid, "reconnecting", 100, since=t0)
        except TimeoutError:
            t_drop = None
        cause = d.grep("session_closed")
        print(
            f"\npeer pinging every {every}s: dropped after {t_drop and round(t_drop - t0, 1)}s "
            f"({cause[0][1].split('cause=')[1] if cause else '-'})"
        )
        assert (t_drop is None) == survives
        if not survives:  # our ping 10 s after the snapshot exchange, then 10 s for an answer
            assert 18 <= t_drop - t0 <= 22


async def test_frozen_phone_detected_in_20s_and_frozen_laptop_detected_by_phone(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    dev = await pair(api, p)
    await asyncio.sleep(2)
    t0 = time.monotonic()
    p.send_signal(signal.SIGSTOP)
    t_rec = await api.wait_state(dev, "reconnecting", 45, since=t0)
    p.send_signal(signal.SIGCONT)
    t_conn = await api.wait_state(dev, "connected", 30, since=t_rec)
    await asyncio.sleep(31)
    t1 = time.monotonic()
    d.send_signal(signal.SIGSTOP)
    _, line = await p.wait_line(r"peer silent", 45, t1)
    t_phone = p.grep("peer silent", t1)[0][0]
    d.send_signal(signal.SIGCONT)
    t_back = await api.wait_state(dev, "connected", 30, since=time.monotonic())
    print(
        f"\nfrozen phone: laptop noticed after {t_rec - t0:.1f}s, reconnected {t_conn - t_rec:.1f}s after resume; "
        f"frozen laptop: phone noticed after {t_phone - t1:.1f}s; after SIGCONT connected again in "
        f"{t_back - t_phone:.1f}s"
    )
    assert 10 <= t_rec - t0 <= 22 and 29 <= t_phone - t1 <= 32


@pytest.mark.parametrize("pause", [15, 22])
async def test_short_laptop_freeze_survives(lab, pause):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    await pair(api, p)
    await asyncio.sleep(12)
    t0 = time.monotonic()
    d.send_signal(signal.SIGSTOP)
    await asyncio.sleep(pause)
    d.send_signal(signal.SIGCONT)
    await asyncio.sleep(3)
    drops = d.grep("session_closed", t0)
    print(
        f"\nlaptop frozen {pause}s: drops {[x[1].split('cause=')[1] for x in drops]}; phone closes "
        f"{[e['reason'] for e in p.events('session_closed', t0)]}"
    )
    assert not drops
