"""Device state transitions and actions under link faults, observed through the local API.

Real orbitd + orbit-fakephone, FaultProxy in between. Timings are printed as `MEASURE ...`.
"""

import asyncio
import time

import pytest
from harness import media_id, measure


async def test_blip_reconnects_quickly_and_marks_activities_stale(paired):
    w = paired
    t0 = time.monotonic()
    rec = await w.recorder(t0)
    await rec.until(lambda r: r.last("devices.changed") is not None)
    w.proxy.reset()
    await rec.until(lambda r: [s for _, s in r.states(w.phone_id)][-1:] == ["connected"]
                    and any(s == "reconnecting" for _, s in r.states(w.phone_id)), timeout=20)
    states = rec.states(w.phone_id)
    stale_seen = any(e == "activities.changed" and d["activities"] and all(a["stale"] for a in d["activities"])
                     for _, e, d in rec.events)
    measure("blip", states=states, stale_seen=stale_seen)
    assert [s for _, s in states] == ["connected", "reconnecting", "connected"]
    assert stale_seen
    assert states[-1][0] < 3.0  # back within the 1 s backoff + handshake


async def test_unreachable_goes_offline_after_grace_and_clears_activities(paired):
    w = paired
    t0 = time.monotonic()
    rec = await w.recorder(t0)
    await rec.until(lambda r: r.last("devices.changed") is not None)
    await w.proxy.refuse()
    w.proxy.reset()
    t_off = await rec.until(lambda r: r.states(w.phone_id)[-1][1] == "offline", timeout=30)
    acts = rec.last("activities.changed")["activities"]
    measure("grace", states=rec.states(w.phone_id), offline_at=round(t_off, 2), activities_left=len(acts))
    assert 14.5 <= t_off <= 16.5
    assert acts == []


@pytest.mark.parametrize("outage", [20, 70])
async def test_recovery_time_after_outage(paired, outage):
    """Phone at a manually added address unreachable for ``outage`` s, then reachable again:
    how long until connected? (No discovery event wakes the connector for a manual address.)"""
    w = paired
    t0 = time.monotonic()
    rec = await w.recorder(t0)
    await rec.until(lambda r: r.last("devices.changed") is not None)
    await w.proxy.refuse()
    w.proxy.reset()
    await asyncio.sleep(outage)
    t_back = time.monotonic() - t0
    await w.proxy.listen()
    t_conn = await rec.until(lambda r: r.states(w.phone_id)[-1][1] == "connected", timeout=150)
    log = w.daemon.log()
    retries = log.count("retrying in")
    measure(f"outage{outage}", phone_back_at=round(t_back, 1), connected_at=round(t_conn, 1),
            recovery=round(t_conn - t_back, 1), retries=retries, states=rec.states(w.phone_id))
    # A user would expect a phone that is reachable again to be back within a few seconds.
    assert t_conn - t_back < 10


async def test_action_while_reconnecting_is_held_and_applied_once(paired):
    w = paired
    mid = media_id(await w.api("activities.list"))
    before = w.phone_actions("media.next")
    w.proxy.reset()
    await asyncio.sleep(0.05)
    t = time.monotonic()
    res = await w.api("activities.action", {"device": w.phone_id, "activity": mid, "action": "media.next"})
    lat = time.monotonic() - t
    await asyncio.sleep(1)
    n = w.phone_actions("media.next") - before
    measure("action_reconnecting", result=res, latency=round(lat, 2), applied=n)
    assert res == {"ok": True} and n == 1


async def test_action_while_offline_fails_fast(paired):
    w = paired
    mid = media_id(await w.api("activities.list"))
    await w.proxy.refuse()
    w.proxy.reset()
    await asyncio.sleep(16)
    t = time.monotonic()
    try:
        res = await w.api("activities.action", {"device": w.phone_id, "activity": mid, "action": "media.next"})
        err = None
    except Exception as e:  # ApiError
        res, err = None, (getattr(e, "code", None), getattr(e, "message", str(e)))
    lat = time.monotonic() - t
    measure("action_offline", result=res, error=err, latency=round(lat, 3))
    assert err == ("not_connected", "phone offline") and lat < 0.5


async def test_action_into_blackhole_reports_timeout_and_is_not_replayed(paired):
    w = paired
    mid = media_id(await w.api("activities.list"))
    before = w.phone_actions("media.next")
    w.proxy.blackhole()
    t = time.monotonic()
    try:
        res, err = await w.api("activities.action", {"device": w.phone_id, "activity": mid, "action": "media.next"}), None
    except Exception as e:
        res, err = None, (getattr(e, "code", None), getattr(e, "message", str(e)))
    lat = time.monotonic() - t
    w.proxy.heal()  # the link comes back: the queued action frame is delivered late
    await asyncio.sleep(3)
    n = w.phone_actions("media.next") - before
    st = (await w.device())["state"]
    measure("action_blackhole", result=res, error=err, latency=round(lat, 2), applied_after_heal=n, state=st)
    assert err == ("timeout", "phone did not respond") and 9 <= lat <= 12.5
    assert n <= 1


async def test_blackhole_detection_time(paired):
    """Silent link: how long does the API keep saying "connected"?"""
    w = paired
    t0 = time.monotonic()
    rec = await w.recorder(t0)
    await rec.until(lambda r: r.last("devices.changed") is not None)
    w.proxy.blackhole()
    t_rec = await rec.until(lambda r: r.states(w.phone_id)[-1][1] != "connected", timeout=45)
    w.proxy.reset()
    w.proxy.heal()
    measure("blackhole_detect", detected_at=round(t_rec, 1), states=rec.states(w.phone_id))
    assert t_rec <= 31.5


async def test_action_sent_then_link_reset_says_may_have_applied(paired):
    w = paired
    mid = media_id(await w.api("activities.list"))
    before = w.phone_actions("media.next")
    w.proxy.blackhole()
    task = asyncio.create_task(w.api("activities.action", {"device": w.phone_id, "activity": mid, "action": "media.next"}))
    await asyncio.sleep(1.0)
    w.proxy.reset()
    w.proxy.heal()
    t = time.monotonic()
    try:
        res, err = await task, None
    except Exception as e:
        res, err = None, (getattr(e, "code", None), getattr(e, "message", str(e)))
    await w.wait_connected()
    await asyncio.sleep(1)
    n = w.phone_actions("media.next") - before
    measure("action_sent_then_reset", result=res, error=err, after_reset=round(time.monotonic() - t, 2), applied=n)
    assert err == ("failed", "connection lost after the action was sent; it may have been applied")
    assert n == 0  # the frame died in the proxy; never replayed on the new session
