# ruff: noqa: E501
"""BLE-like slow link (about 5 KB/s phone -> laptop in ATT-sized chunks) with album-art bursts."""

import asyncio
import time

from .harness import log, pair

BURST = "media:burst"


def ble_like(proxy, rate=5000):
    proxy.down.set(chunk=244, rate=rate)
    proxy.up.set(chunk=244, rate=rate)


async def act(laptop, dev, activity=BURST):
    return await laptop.timed_call("activities.action", {"device": dev, "activity": activity, "action": "media.pause"})


async def test_action_result_stuck_behind_art_burst(rig):
    """A track change burst (5 new 30 KB covers) is queued; the user presses pause right after. The
    phone executes it at once, but its action_result waits behind ~200 KB of art on one FIFO queue."""
    phone, proxy, laptop = await rig()
    ble_like(proxy)
    await phone.cmd("art_burst", count=1, bytes=2000)
    dev = await pair(laptop, phone, proxy.port)
    await laptop.wait_activities(lambda a: any(x["id"] == BURST and not x["stale"] for x in a), 20)
    t0 = time.monotonic()
    await phone.cmd("art_burst", count=5, bytes=30000)
    await asyncio.sleep(0.5)
    dt, r = await act(laptop, dev)
    executed = [round(ts - t0, 1) for ts, _ in phone.events("action", t0)]
    t_drain = await laptop.wait_activities(lambda a: any(x.get("title") == "Track 4" for x in a), 120, t0)
    dt2, r2 = await act(laptop, dev)
    states = [(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)]
    log(
        f"art burst @5KB/s: action answered {dt:.1f}s {r!r}, phone executed at {executed}s; burst drained {t_drain - t0:.1f}s; next action {dt2:.2f}s {r2}; laptop states {states}"
    )
    assert r == {"ok": True}, "the phone did apply it; the user was told it did not respond"


async def test_reconnect_snapshot_with_art_on_slow_link(rig):
    """After a drop, the snapshot follows every art it references: on a slow link the laptop shows
    the old, stale activities (and actions on them time out) until all art has crawled through."""
    phone, proxy, laptop = await rig()
    ble_like(proxy)
    for i in range(4):
        await phone.cmd("art_burst", count=1, bytes=30000, activity=f"media:app{i}")
    t_pair0 = time.monotonic()
    dev = await pair(laptop, phone, proxy.port)
    t_fresh0 = await laptop.wait_activities(lambda a: len(a) == 4 and all(not x["stale"] for x in a), 120, t_pair0)
    log(f"initial pairing on slow link: activities shown {t_fresh0 - t_pair0:.1f}s after pairing started")
    await asyncio.sleep(31)
    t0 = time.monotonic()
    proxy.rst_all()
    t_con = await laptop.wait_state(dev, "connected", 30, t0 + 0.001)
    dt, r = await act(laptop, dev, "media:app0")
    t_fresh = await laptop.wait_activities(lambda a: len(a) == 4 and all(not x["stale"] for x in a), 120, t_con)
    log(
        f"slow-link reconnect: connected {t_con - t0:.1f}s, activities fresh {t_fresh - t0:.1f}s; action meanwhile {dt:.1f}s {r!r}"
    )
    assert t_fresh - t_con < 5, "activities stay stale long after the session is back"
