# ruff: noqa: E501
"""Baseline, RST and phone-restart scenarios: real orbitd -> FaultProxy -> Kotlin ResponderSession."""

import asyncio
import time

from orbit.client import ApiError

from .harness import MEDIA, log, pair


async def setup(rig, **kw):
    phone, proxy, laptop = await rig(**kw)
    await phone.cmd("upsert", activity=MEDIA)
    dev = await pair(laptop, phone, proxy.port)
    await laptop.wait_activities(lambda a: any(x["id"] == MEDIA["id"] and not x["stale"] for x in a), 5)
    return phone, proxy, laptop, dev


async def action(laptop, dev, act="media.pause"):
    return await laptop.timed_call("activities.action", {"device": dev, "activity": MEDIA["id"], "action": act})


async def test_baseline_pair_and_action(rig):
    phone, proxy, laptop, dev = await setup(rig)
    dt, res = await action(laptop, dev)
    log("baseline action", f"{dt * 1000:.0f} ms", res)
    assert res == {"ok": True}
    assert dt < 1.0
    st = await phone.stats()
    assert st["actions"] == {f"{MEDIA['id']}/media.pause": 1}


async def test_rst_young_session_recovers(rig):
    """RST both ends (AP/NAT drops the flow) on a session younger than STABLE_SESSION (30 s)."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    assert proxy.rst_all() == 1
    t_rec = await laptop.wait_state(dev, "reconnecting", 5, t0)
    t_con = await laptop.wait_state(dev, "connected", 20, t_rec)
    t_fresh = await laptop.wait_activities(lambda a: any(not x["stale"] for x in a), 10, t_con)
    closed = await phone.wait_event("session_closed", 5, t0)
    log(
        f"rst young: reconnecting after {t_rec - t0:.2f}s, connected after {t_con - t0:.2f}s, fresh after {t_fresh - t0:.2f}s; phone reason {closed['reason']!r}"
    )
    dt, res = await action(laptop, dev)
    assert res == {"ok": True}
    assert t_con - t0 < 3.0


async def test_rst_stable_session_recovers_immediately(rig):
    phone, proxy, laptop, dev = await setup(rig)
    await asyncio.sleep(31)  # past STABLE_SESSION
    t0 = time.monotonic()
    proxy.rst_all()
    t_con = await laptop.wait_state(dev, "connected", 20, t0 + 0.001)
    log(f"rst stable: connected again after {t_con - t0:.2f}s")
    assert t_con - t0 < 1.0


async def test_action_right_after_rst_is_sent_once(rig):
    """An action issued while the device is reconnecting waits for the new session and runs once."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.rst_all()
    await laptop.wait_state(dev, "reconnecting", 5, t0)
    dt, res = await action(laptop, dev)
    st = await phone.stats()
    log(f"action during reconnect: {dt:.2f}s {res}; phone counts {st['actions']}")
    assert res == {"ok": True}
    assert st["actions"] == {f"{MEDIA['id']}/media.pause": 1}


async def test_rst_while_action_in_flight_is_not_resent(rig):
    """The phone executed the action but the result is lost: the laptop must say so and not resend."""
    phone, proxy, laptop, dev = await setup(rig)
    await phone.cmd("action_delay", ms=2000)
    task = asyncio.create_task(action(laptop, dev))
    await phone.wait_event("action", 5)
    proxy.rst_all()
    dt, res = await task
    await laptop.wait_state(dev, "connected", 20)
    await asyncio.sleep(2)
    st = await phone.stats()
    log(f"rst in flight: {dt:.2f}s {res!r}; phone counts {st['actions']}")
    assert isinstance(res, ApiError) and res.code == "failed" and "may have been applied" in res.message
    assert st["actions"] == {f"{MEDIA['id']}/media.pause": 1}


async def test_phone_process_restart(rig, workdir):
    """kill -9 the phone (app killed / crashed), restart on the same port after 8 s."""
    phone, proxy, laptop, dev = await setup(rig)
    from .harness import JvmPhone

    t0 = time.monotonic()
    phone.proc.kill()
    t_rec = await laptop.wait_state(dev, "reconnecting", 5, t0)
    await asyncio.sleep(8)
    phone2 = JvmPhone(workdir, name="phone2", port=phone.port)
    phone2.state = phone.state
    phone2.argv[phone2.argv.index("--state") + 1] = str(phone.state)
    await phone2.start()
    t_up = time.monotonic()
    await phone2.cmd("upsert", activity=MEDIA)
    try:
        t_con = await laptop.wait_state(dev, "connected", 90, t0)
        log(
            f"phone restart: reconnecting {t_rec - t0:.2f}s, phone back at {t_up - t0:.2f}s, connected at {t_con - t0:.2f}s ({t_con - t_up:.2f}s after phone was back)"
        )
        log("states", [(round(t - t0, 2), s) for t, s in laptop.state_log(dev, t0)])
    finally:
        await phone2.stop()
    assert t_con - t_up < 5.0


async def test_laptop_roams_phone_keeps_zombie(rig):
    """The laptop's end resets (it moved to another network) but the phone never hears about the old
    flow: the phone holds a zombie PAIRED session next to the new one until its idle timeout."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    proxy.rst_laptop_side()
    t_con = await laptop.wait_state(dev, "connected", 20, t0 + 0.001)
    st = await phone.stats()
    await phone.cmd("upsert", activity={**MEDIA, "title": "Next song"})
    t_up = await laptop.wait_activities(
        lambda a: any(x.get("title") == "Next song" and not x["stale"] for x in a), 5, t_con
    )
    dt, res = await action(laptop, dev)
    closed = await phone.wait_event("session_closed", 40, t0)
    t_z = phone.events("session_closed", t0)[0][0]
    log(
        f"laptop roam: reconnected {t_con - t0:.2f}s; phone sessions meanwhile {st['sessionStates']}; update arrived {t_up - t0:.2f}s; action {dt:.2f}s {res}; zombie closed at {t_z - t0:.1f}s ({closed['reason']})"
    )
    assert res == {"ok": True}
    assert (await phone.stats())["sessions"] == 1
