# ruff: noqa: E501
"""Blackhole, half-open, slow-reader, fragmentation and latency scenarios against the Kotlin responder."""

import asyncio
import signal
import time

from orbit.client import ApiError

from .harness import MEDIA, log, pair
from .test_jvm_basic import action, setup

PAUSE = f"{MEDIA['id']}/media.pause"


async def test_blackhole_both_ways_detected_and_recovered(rig):
    """Every byte stops both ways, no FIN/RST (Wi-Fi gone). Both sides must notice within 30 s and
    reconnect once the path is back."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.up.set(mode="pause")
    proxy.down.set(mode="pause")
    proxy.accepting = "hang"
    t_rec = await laptop.wait_state(dev, "reconnecting", 40, t0)
    closed = await phone.wait_event("session_closed", 40, t0)
    t_phone = time.monotonic()
    log(
        f"blackhole: laptop reconnecting after {t_rec - t0:.1f}s; phone closed ({closed['reason']}) by {t_phone - t0:.1f}s"
    )
    proxy.forget_all()
    proxy.up.set(mode="pass")
    proxy.down.set(mode="pass")
    proxy.accepting = "pass"
    t_back = time.monotonic()
    t_con = await laptop.wait_state(dev, "connected", 90, t_back)
    log(f"blackhole: connected {t_con - t_back:.1f}s after the path came back")
    log("states", [(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)])
    assert 28 <= t_rec - t0 <= 33
    assert t_phone - t0 <= 33


async def test_action_in_short_blackhole_is_applied_late_after_timeout(rig):
    """A 20 s outage (both ways, no FIN/RST): the laptop answers "timeout" after ~10-12 s, then the
    outage ends and TCP delivers the queued action anyway. The user's retry runs it a second time."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.up.set(mode="pause")
    proxy.down.set(mode="pause")
    dt1, r1 = await action(laptop, dev)
    log(f"short blackhole: first action answered after {dt1:.1f}s: {r1!r}")
    dt2_task = asyncio.create_task(action(laptop, dev))  # the user's retry, still inside the outage
    await asyncio.sleep(max(0.0, 20 - (time.monotonic() - t0)))
    proxy.up.set(mode="pass")
    proxy.down.set(mode="pass")
    t_heal = time.monotonic()
    dt2, r2 = await dt2_task
    await asyncio.sleep(2)
    acts = phone.events("action", t0)
    log(f"short blackhole: retry answered after {dt2:.1f}s: {r2!r}")
    log("phone executed", [round(ts - t0, 1) for ts, _ in acts], "outage ended at", round(t_heal - t0, 1))
    log("laptop states", [(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)])
    # The user was told the first press did not get an answer; it must not be applied later
    # (and a retry must not make it two).
    assert isinstance(r1, ApiError) and r1.code == "timeout"
    assert len(acts) <= 1, f"phone executed the pause {len(acts)} times, at {[round(ts - t0, 1) for ts, _ in acts]} s"


async def test_frozen_phone_executes_stale_action_after_thaw(rig):
    """The phone process is frozen (Android cached-app freezer / Doze; here SIGSTOP) for 60 s. The
    laptop's action times out and the laptop drops the session at 30 s. On thaw the phone still reads
    the old action from its socket buffer and executes it, a minute after the user gave up."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    phone.signal(signal.SIGSTOP)
    try:
        dt, r = await action(laptop, dev)
        log(f"frozen phone: action answered after {dt:.1f}s: {r!r}")
        t_rec = await laptop.wait_state(dev, "reconnecting", 40, t0)
        log(f"frozen phone: laptop reconnecting after {t_rec - t0:.1f}s")
        await asyncio.sleep(max(0.0, 60 - (time.monotonic() - t0)))
    finally:
        phone.signal(signal.SIGCONT)
    t_thaw = time.monotonic()
    t_con = await laptop.wait_state(dev, "connected", 90, t_thaw)
    await asyncio.sleep(2)
    acts = phone.events("action", t0)
    log(
        f"frozen phone: connected {t_con - t_thaw:.1f}s after thaw; phone executed at {[round(ts - t0, 1) for ts, _ in acts]} s (thaw at {t_thaw - t0:.1f})"
    )
    log(
        "phone closes", [(round(ts - t0, 1), e["session"], e["reason"]) for ts, e in phone.events("session_closed", t0)]
    )
    assert isinstance(r, ApiError) and r.code == "timeout"
    assert not acts, (
        "the phone executed an action the laptop had already reported as timed out, after the laptop dropped the session"
    )


async def test_half_open_phone_to_laptop_lost(rig):
    """Phone -> laptop bytes are silently dropped; laptop -> phone still flows."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.down.set(mode="drop")
    dt, r = await action(laptop, dev)
    t_rec = await laptop.wait_state(dev, "reconnecting", 40, t0)
    closed = await phone.wait_event("session_closed", 40, t0)
    t_pc = time.monotonic()
    proxy.down.set(mode="pass")
    t_con = await laptop.wait_state(dev, "connected", 60, t_rec)
    log(
        f"half-open p->l: action {dt:.1f}s {r!r}; laptop reconnecting {t_rec - t0:.1f}s; phone closed by {t_pc - t0:.1f}s ({closed['reason']}); connected {t_con - t0:.1f}s"
    )
    assert 28 <= t_rec - t0 <= 33


async def test_half_open_laptop_to_phone_lost(rig):
    """Laptop -> phone bytes are silently dropped; phone -> laptop still flows (the phone's pings keep
    the laptop's dead timer happy, so only the phone can notice)."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.up.set(mode="drop")
    dt, r = await action(laptop, dev)
    closed = await phone.wait_event("session_closed", 40, t0)
    t_pc = time.monotonic()
    t_rec = await laptop.wait_state(dev, "reconnecting", 40, t0)
    proxy.up.set(mode="pass")
    t_con = await laptop.wait_state(dev, "connected", 60, t_rec)
    log(
        f"half-open l->p: action {dt:.1f}s {r!r}; phone closed {t_pc - t0:.1f}s ({closed['reason']}); laptop reconnecting {t_rec - t0:.1f}s; connected {t_con - t0:.1f}s"
    )
    assert t_rec - t0 <= 33


async def test_slow_reader_laptop_stops_reading(rig):
    """Phone -> laptop stalls (kernel buffers fill, nothing read) while a download updates 100x/s;
    laptop -> phone still flows. Who notices, how, and does everything recover and clean up?"""
    phone, proxy, laptop, dev = await setup(rig)
    base = await phone.stats()
    t0 = time.monotonic()
    proxy.down.set(mode="pause")
    await phone.cmd("churn", count=4000, rate=100)
    t_rec = await laptop.wait_state(dev, "reconnecting", 60, t0)
    closed = await phone.wait_event("session_closed", 60, t0)
    t_pc = time.monotonic()
    proxy.forget_all()
    proxy.down.set(mode="pass")
    t_con = await laptop.wait_state(dev, "connected", 60, t_rec)
    await phone.wait_event("churn_done", 60, t0)
    await asyncio.sleep(1)
    st = await phone.stats()
    log(
        f"slow reader: laptop reconnecting {t_rec - t0:.1f}s; phone closed by {t_pc - t0:.1f}s ({closed['reason']}); connected {t_con - t0:.1f}s"
    )
    log("phone threads before", base["threadNames"], "after", st["threadNames"], "sessions", st["sessionStates"])
    assert t_rec - t0 <= 33
    assert st["threadNames"].get("orbit-send-N", 0) == base["threadNames"].get("orbit-send-N", 0)
    assert st["sessions"] == 1


async def test_laptop_frozen_45s(rig):
    """orbitd is SIGSTOPped for 45 s (laptop lid closed / suspended) then resumed."""
    phone, proxy, laptop, dev = await setup(rig)
    await asyncio.sleep(31)  # a stable session
    t0 = time.monotonic()
    laptop.signal(signal.SIGSTOP)
    try:
        closed = await phone.wait_event("session_closed", 45, t0)
        t_pc = time.monotonic()
        await asyncio.sleep(max(0.0, 45 - (time.monotonic() - t0)))
    finally:
        laptop.signal(signal.SIGCONT)
    t_wake = time.monotonic()
    t_con = await laptop.wait_state(dev, "connected", 60, t_wake + 0.001)
    t_fresh = await laptop.wait_activities(lambda a: a and all(not x["stale"] for x in a), 20, t_con)
    dt, r = await action(laptop, dev)
    log(
        f"laptop frozen: phone closed at {t_pc - t0:.1f}s ({closed['reason']}); after wake: connected {t_con - t_wake:.2f}s, fresh {t_fresh - t_wake:.2f}s; action {dt:.2f}s {r}"
    )
    log("states", [(round(t - t0, 2), s) for t, s in laptop.state_log(dev, t0)])
    assert t_con - t_wake < 3
    assert r == {"ok": True}


async def test_one_byte_fragmentation(rig):
    phone, proxy, laptop = await rig()
    for d in (proxy.up, proxy.down):
        d.set(chunk=1, chunk_delay=0.0005)
    await phone.cmd("upsert", activity=MEDIA)
    t0 = time.monotonic()
    dev = await pair(laptop, phone, proxy.port)
    t_pair = time.monotonic() - t0
    await laptop.wait_activities(lambda a: any(not x["stale"] for x in a), 20)
    dt, r = await action(laptop, dev)
    log(f"1-byte fragments: pairing {t_pair:.2f}s, action {dt * 1000:.0f} ms {r}")
    assert r == {"ok": True}


async def test_latency_and_jitter(rig):
    """300 ms +- 300 ms each way (congested Wi-Fi): no spurious drops over 40 s, actions succeed."""
    phone, proxy, laptop = await rig()
    for d in (proxy.up, proxy.down):
        d.set(latency=0.3, jitter=0.3)
    await phone.cmd("upsert", activity=MEDIA)
    t0 = time.monotonic()
    dev = await pair(laptop, phone, proxy.port)
    t_pair = time.monotonic() - t0
    await laptop.wait_activities(lambda a: any(not x["stale"] for x in a), 20)
    t_paired = time.monotonic()
    lat = []
    for _ in range(8):
        dt, r = await action(laptop, dev)
        assert r == {"ok": True}
        lat.append(round(dt, 2))
        await asyncio.sleep(4)
    log(f"latency 300+-300ms: pairing {t_pair:.1f}s, action latencies {lat}")
    assert [s for _, s in laptop.state_log(dev, t_paired) if s != "connected"] == []


async def test_high_latency_handshake_limit(rig):
    """Find the one-way latency at which connecting stops working (laptop connect bound 10 s per step,
    Kotlin handshake bound 10 s)."""
    phone, proxy, laptop = await rig()
    await phone.cmd("auto_accept", value=True)
    results = {}
    for lat in (1.0, 2.0, 3.0, 4.0):
        for d in (proxy.up, proxy.down):
            d.set(latency=lat)
        dt, r = await laptop.timed_call("devices.connect", {"host": "127.0.0.1", "port": proxy.port})
        results[lat] = (round(dt, 1), r if isinstance(r, ApiError) else "ok")
        if not isinstance(r, ApiError):
            await laptop.call("devices.disconnect", {"id": r["id"]})
        await asyncio.sleep(1)
    log("one-way latency -> connect", results)
    closes = [(e["reason"]) for _, e in phone.events("session_closed")]
    log("phone close reasons", closes)
