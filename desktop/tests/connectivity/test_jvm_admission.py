# ruff: noqa: E501
"""Admission limits (16 unpaired sessions in total, 4 per address) versus a legitimate paired laptop.

Flood sources come from ORBIT_FLOOD_SOURCES (comma-separated local addresses to bind). macOS only
has 127.0.0.1; on Linux every 127.0.0.0/8 address works, e.g. in Docker:
ORBIT_FLOOD_SOURCES=127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5
"""

import asyncio
import contextlib
import os
import time

from orbit.proto import FrameStream, KeyPair, initiate

from .harness import log
from .test_jvm_basic import setup

SOURCES = os.environ.get("ORBIT_FLOOD_SOURCES", "127.0.0.1").split(",")


async def hold_pairing(port: int, source: str, stop: asyncio.Event) -> str:
    """A LAN peer that completes the handshake, asks to pair and then just waits (nobody confirms)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))
    try:
        ch = await asyncio.wait_for(initiate(FrameStream(reader, writer), KeyPair.generate(), "Flood", "linux"), 5)
        await ch.send({"t": "ready", "paired": False, "now": 0})
        await ch.recv()
        await ch.send({"t": "pair_request"})
        while not stop.is_set():  # answer pings so the phone keeps us
            with contextlib.suppress(TimeoutError):
                msg = await asyncio.wait_for(ch.recv(), 1)
                if msg["t"] == "ping":
                    await ch.send({"t": "pong", "req": msg.get("req")})
        return "held"
    except Exception as e:
        return f"{type(e).__name__}"
    finally:
        writer.close()


async def test_paired_laptop_gets_in_during_pairing_flood(rig):
    """Someone on the LAN keeps the unpaired slots busy (handshake + pair_request, which the phone
    holds for up to 120 s). The paired laptop's session drops meanwhile: can it reconnect?"""
    phone, proxy, laptop, dev = await setup(rig)
    stop = asyncio.Event()
    holders = []
    for src in SOURCES:
        for _ in range(4 if len(SOURCES) > 1 else 4):
            holders.append(asyncio.create_task(hold_pairing(phone.port, src, stop)))
            await asyncio.sleep(0.05)
    await asyncio.sleep(1)
    st = await phone.stats()
    log(f"flood from {SOURCES}: phone sessions {st['sessionStates']}, unauthenticated {st['unauthenticated']}")
    t0 = time.monotonic()
    proxy.rst_all()
    try:
        t_con = await laptop.wait_state(dev, "connected", 60, t0 + 0.001)
        got_in = t_con - t0
    except TimeoutError:
        got_in = None
    evicted = phone.events("evicted", t0)
    log(f"paired laptop during flood: reconnected after {got_in}; phone evicted {len(evicted)} unpaired sessions")
    log("states", [(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)])
    stop.set()
    await asyncio.gather(*holders)
    t_stop = time.monotonic()
    if got_in is None:
        t_con = await laptop.wait_state(dev, "connected", 90, t_stop)
        log(f"flood stopped: laptop reconnected {t_con - t_stop:.1f}s later")
    assert got_in is not None and got_in < 5, (
        "a paired laptop is locked out by unpaired sessions it has nothing to do with"
    )


async def test_new_connection_displaces_the_oldest_from_its_address(rig):
    """Four idle TCP connections (no commit) fill one address's slots: a fifth one gets in, and the
    oldest of the four is closed rather than holding its slot until the 10 s handshake timeout."""
    phone, proxy, laptop = await rig(laptop=False)
    conns = []
    for _ in range(4):
        conns.append(await asyncio.open_connection("127.0.0.1", phone.port, local_addr=(SOURCES[0], 0)))
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    r, w = await asyncio.open_connection("127.0.0.1", phone.port, local_addr=(SOURCES[0], 0))
    first = await asyncio.wait_for(conns[0][0].read(1), 5)
    t_closed = time.monotonic() - t0
    ev = await phone.wait_event("evicted", 5, t0)
    w.write(b"\x00")  # the fifth is being served: its handshake is still open
    await w.drain()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(r.read(1), 1)
    st = await phone.stats()
    log(
        f"5th connection from one address: oldest closed after {t_closed:.2f}s (read {first!r}), "
        f"evicted {ev}; phone sessions {st['sessionStates']}"
    )
    for _, w2 in conns + [(r, w)]:
        w2.close()
    assert first == b""
    assert st["sessionStates"] == {"HANDSHAKE": 4}
