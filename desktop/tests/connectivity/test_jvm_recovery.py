# ruff: noqa: E501
"""How fast the laptop gets back once the fault is over (manual host:port route, no mDNS wake-ups)."""

import asyncio
import time

from orbit.client import ApiError

from .harness import MEDIA, JvmPhone, log
from .test_jvm_basic import setup


async def test_recovery_after_a_few_flaps(rig):
    """Five drops in a row, each a moment after the session came back (walking at the edge of Wi-Fi
    coverage). Then the link is fine: how long until the laptop is back, and what do actions say?"""
    phone, proxy, laptop, dev = await setup(rig)
    for _ in range(5):
        t = time.monotonic()
        proxy.rst_all()
        await laptop.wait_state(dev, "connected", 90, t + 0.001)
        await asyncio.sleep(0.5)
    t0 = time.monotonic()
    proxy.rst_all()  # the last drop; the link is healthy from here on
    probes = []
    while True:
        dt, r = await laptop.timed_call(
            "activities.action", {"device": dev, "activity": MEDIA["id"], "action": "media.pause"}
        )
        probes.append(
            (
                round(time.monotonic() - t0, 1),
                "ok" if r == {"ok": True} else r.message if isinstance(r, ApiError) else r,
            )
        )
        if r == {"ok": True}:
            break
        await asyncio.sleep(2)
    t_back = time.monotonic() - t0
    log(
        f"after 6 flaps: back {t_back:.1f}s after the last drop (link healthy all along); states {[(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)]}"
    )
    log("action probes", probes)
    assert t_back < 5, "the link was healthy, but the laptop stayed away (backoff) and reported the phone offline"


async def test_phone_down_65s(rig, workdir):
    """The phone app is gone for 65 s (reboot, OEM task killer + sticky restart) and comes back on the same port."""
    phone, proxy, laptop, dev = await setup(rig)
    t0 = time.monotonic()
    phone.proc.kill()
    await asyncio.sleep(65)
    phone2 = JvmPhone(workdir, name="phone2", port=phone.port)
    phone2.argv[phone2.argv.index("--state") + 1] = str(phone.state)
    await phone2.start()
    t_up = time.monotonic()
    try:
        t_con = await laptop.wait_state(dev, "connected", 120, t_up)
    finally:
        await phone2.stop()
    log(
        f"phone down 65 s: connected {t_con - t_up:.1f}s after the phone was back; states {[(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)]}"
    )
    assert t_con - t_up < 5 + 1.5  # retried at least every 5 s within the first 5 minutes (LOCAL_API.md)
