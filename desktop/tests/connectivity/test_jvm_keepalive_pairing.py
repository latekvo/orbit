# ruff: noqa: E501
"""Keepalive interop (Kotlin 10/30 vs Python 10/30, and either side on the older 20/60) and pairing
interrupted by link faults."""

import asyncio
import contextlib
import time

import pytest

from .harness import MEDIA, log, pair
from .test_jvm_basic import setup

COMBOS = {
    "kotlin10-30_python10-30": ({}, {}),
    "kotlin20-60_python10-30": ({"ping_after_ms": 20000, "idle_ms": 60000}, {}),
    "kotlin10-30_python20-60": ({}, {"keepalive": 20.0, "dead_after": 60.0}),
}


@pytest.mark.parametrize("combo", list(COMBOS))
async def test_keepalive_interop(rig, combo):
    phone_opts, laptop_kwargs = COMBOS[combo]
    phone, proxy, laptop, dev = await setup(rig, phone_opts=phone_opts, laptop_kwargs=laptop_kwargs)
    t0 = time.monotonic()
    await asyncio.sleep(130)  # idle: only keepalives flow
    drops = [s for _, s in laptop.state_log(dev, t0) if s != "connected"]
    closes = phone.events("session_closed", t0)
    bytes_up, bytes_down = proxy.up.bytes, proxy.down.bytes
    t1 = time.monotonic()
    proxy.up.set(mode="pause")
    proxy.down.set(mode="pause")
    proxy.accepting = "hang"
    t_lap = await laptop.wait_state(dev, "reconnecting", 75, t1)
    ev = await phone.wait_event("session_closed", 75, t1)
    t_ph = phone.events("session_closed", t1)[0][0]
    log(
        f"{combo}: 130 s idle -> laptop drops {drops}, phone closes {len(closes)}, bytes up/down {bytes_up}/{bytes_down}; blackhole detected by laptop {t_lap - t1:.1f}s, phone {t_ph - t1:.1f}s ({ev['reason']})"
    )
    assert not drops and not closes


async def test_drop_during_sas_prompt_then_repair(rig):
    phone, proxy, laptop = await rig()
    await phone.cmd("upsert", activity=MEDIA)
    t0 = time.monotonic()
    res = await laptop.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port})
    await laptop.wait_event("pairing.changed", lambda d: bool(d["pairings"]), 10, t0)
    old = next(
        p for _, e, d in reversed(laptop.events) if e == "pairing.changed" and d["pairings"] for p in d["pairings"]
    )
    await phone.wait_event("session_state", 5, t0, lambda e: e["state"] == "PAIRING")
    t1 = time.monotonic()
    proxy.rst_all()
    t_gone = await laptop.wait_event("pairing.changed", lambda d: not d["pairings"], 5, t1)
    closed = await phone.wait_event("session_closed", 5, t1)
    late = await laptop.timed_call("pairing.respond", {"session": old["session"], "accept": True})
    await phone.cmd("decide", accept=True)  # the phone user taps accept on the (now gone) prompt
    await asyncio.sleep(3)
    attempts = len(phone.events("session_open", t1))
    dev = await pair(laptop, phone, proxy.port)
    log(
        f"drop during SAS: laptop prompt gone {t_gone - t1:.2f}s, phone closed ({closed['reason']}); late accept -> {late[1]!r}; reconnect attempts while unpaired {attempts}; re-pair ok, pins {phone.events('pinned')[-1][1]['peer'][:8]}"
    )
    assert attempts == 0
    assert dev == res["id"]


async def test_phone_accepts_while_laptop_cannot_hear(rig):
    """Laptop accepts first; then phone -> laptop stalls; the phone user accepts (the phone pins and
    sends pair_decision + snapshot into the void). What do the two sides believe afterwards?"""
    phone, proxy, laptop = await rig()
    await phone.cmd("upsert", activity=MEDIA)
    t0 = time.monotonic()
    dev = (await laptop.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port}))["id"]
    await laptop.wait_event("pairing.changed", lambda d: bool(d["pairings"]), 10, t0)
    p = next(
        p for _, e, d in reversed(laptop.events) if e == "pairing.changed" and d["pairings"] for p in d["pairings"]
    )
    await laptop.call("pairing.respond", {"session": p["session"], "accept": True})
    await asyncio.sleep(0.5)
    proxy.down.set(mode="pause")
    t1 = time.monotonic()
    await phone.cmd("decide", accept=True)
    await phone.wait_event("pinned", 5, t0)
    t_end = await laptop.wait_event("pairing.changed", lambda d: not d["pairings"], 150, t1)
    await asyncio.sleep(1)
    d = laptop.device(dev)
    proxy.forget_all()
    proxy.down.set(mode="pass")
    await asyncio.sleep(5)
    reconnects = len(phone.events("session_open", t_end))
    log(
        f"phone accepted, laptop deaf: phone PAIRED+pinned at once; laptop pairing ended {t_end - t1:.1f}s later; laptop device {d and {k: d[k] for k in ('paired', 'state')}}; laptop reconnect attempts {reconnects}"
    )
    # Re-pairing from the laptop must still work (the phone keeps its stale pin).
    t2 = time.monotonic()
    await laptop.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port})
    await laptop.wait_event("pairing.changed", lambda d: bool(d["pairings"]), 10, t2)
    with contextlib.suppress(TimeoutError):
        await phone.wait_event("session_state", 5, t2, lambda e: e["state"] == "PAIRING")
    st = [e["state"] for _, e in phone.events("session_state", t2)]
    log("phone states on re-pair", st)
    assert "PAIRING" in st


async def test_laptop_accepts_while_phone_cannot_hear(rig):
    """Phone accepts first; then laptop -> phone stalls; the laptop user accepts (the laptop pins at
    once). The phone never hears it. What does the user see on the laptop?"""
    phone, proxy, laptop = await rig()
    await phone.cmd("upsert", activity=MEDIA)
    t0 = time.monotonic()
    dev = (await laptop.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port}))["id"]
    await laptop.wait_event("pairing.changed", lambda d: bool(d["pairings"]), 10, t0)
    p = next(
        p for _, e, d in reversed(laptop.events) if e == "pairing.changed" and d["pairings"] for p in d["pairings"]
    )
    await phone.cmd("decide", accept=True)
    await laptop.wait_event("pairing.changed", lambda d: any(x["remoteAccepted"] for x in d["pairings"]), 5, t0)
    proxy.up.set(mode="pause")
    t1 = time.monotonic()
    await laptop.call("pairing.respond", {"session": p["session"], "accept": True})
    t_paired = await laptop.wait_event(
        "devices.changed", lambda d: any(x["id"] == dev and x["paired"] for x in d["devices"]), 5, t1
    )
    closed = await phone.wait_event("session_closed", 60, t1)
    t_pc = phone.events("session_closed", t1)[0][0]
    proxy.forget_all()
    proxy.up.set(mode="pass")
    t_unpaired = await laptop.wait_event(
        "devices.changed", lambda d: not any(x["id"] == dev and x["paired"] for x in d["devices"]), 120, t_paired
    )
    await asyncio.sleep(1)
    log(
        f"laptop accepted, phone deaf: laptop showed paired after {t_paired - t1:.2f}s (activities {len(laptop.activities())}); phone closed at {t_pc - t1:.1f}s ({closed['reason']}); laptop unpaired itself at {t_unpaired - t1:.1f}s; phone pinned: {bool(phone.events('pinned', t0))}"
    )
    log("laptop states", [(round(t - t1, 1), s) for t, s in laptop.state_log(dev, t1)])
    log("orbitd log", [t for _, t in laptop.lines if "trust" in t or "unpin" in t][-2:])
