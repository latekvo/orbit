"""Laptop suspend: SIGSTOP orbitd while the network path goes dark, then resume.

During the "sleep" the proxy black-holes the connection (the phone gives up after 30 s and closes;
that FIN never reaches the sleeping laptop); on wake the laptop's first byte draws an RST, as from a
phone that forgot the connection.

* plain SIGSTOP: time.monotonic keeps running (like CLOCK_BOOTTIME)
* ORBIT_FREEZE_MONO=1: monotonic does not advance while stopped, as CLOCK_MONOTONIC / mach time across
  a real suspend; the wall clock is additionally stepped forward by 3 h to emulate hours asleep.
"""

import asyncio
import os
import signal
import time

import pytest
from harness import Proxy, pair

HOUR = 3_600_000


@pytest.mark.parametrize("mono_frozen", [False, True])
async def test_laptop_suspend(lab, mono_frozen):
    skew = lab.root / "skew"
    skew.write_text("0")
    env = {"ORBIT_SKEW_FILE": str(skew)}
    if mono_frozen:
        env["ORBIT_FREEZE_MONO"] = "1"
    d = await lab.daemon(env=env)
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    await api.wait_fresh_activities(dev, 5)
    await asyncio.sleep(31)
    conn = pr.live[0]
    conn.freeze()
    d.send_signal(signal.SIGSTOP)
    await p.wait_line("peer silent", 45)
    await asyncio.sleep(60 - 31)
    conn.rst_on_up = True
    if mono_frozen:
        skew.write_text(str(3 * HOUR))
    t_wake = time.monotonic()
    d.send_signal(signal.SIGCONT)
    if mono_frozen:
        os.kill(d.pid, signal.SIGUSR2)
    # what a frontend reads right after wake, and an action pressed 1 s after wake
    await asyncio.sleep(1)
    x = await api.device(dev)
    acts = [a for a in await api.call("activities.list") if a["device"] == dev]
    view = (x["state"], any(a.get("stale") for a in acts))
    lat, res = await api.timed(
        "activities.action", {"device": dev, "activity": "msg:chat", "action": "notif.0", "value": "after-wake"}
    )
    t_conn = await api.wait_state(dev, "connected", 30, since=t_wake + 0.05)
    closes = d.grep("session_closed", t_wake)
    t_drop = closes[0][0] - t_wake if closes else None
    await asyncio.sleep(1)
    ran = sum(1 for a in p.actions() if a["value"] == "after-wake")
    print(
        f"\nsuspend 60 s (monotonic {'frozen' if mono_frozen else 'running'}): 1 s after wake the laptop shows "
        f"state={view[0]} stale={view[1]}; action -> {res!r} in {lat:.1f}s (executed {ran}x); old session "
        f"dropped at +{t_drop and round(t_drop, 1)}s ({closes[0][1].split('cause=')[1] if closes else '-'}); "
        f"connected again at +{t_conn - t_wake:.1f}s"
    )
    assert t_drop is not None and t_drop < 2, "pre-sleep session (and its activities) presented as live after wake"
    assert res == {"ok": True} and ran == 1
    await pr.close()
