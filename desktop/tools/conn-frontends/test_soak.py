"""Repeated blips and orbitd restarts: leaks in orbitd, the applet and the API socket."""

import asyncio
import time

from harness import measure


async def test_repeated_blips_leak_nothing(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["cards"], 10)
    await asyncio.sleep(1)
    fds0, rss0 = w.daemon.fds(), w.daemon.rss_kb()
    afds0 = a.proc.fds()
    lat = []
    for _ in range(7):
        w.proxy.reset()
        lat.append(round(await w.wait_connected(70), 1))
        await asyncio.sleep(3)  # a flapping link: each session lives ~3 s
    measure("flapping_link_reconnect_latencies", latencies=lat)
    await asyncio.sleep(2)
    s = a.now()
    fds1, rss1 = w.daemon.fds(), w.daemon.rss_kb()
    log = w.daemon.log()
    measure("soak_blips", fds=(fds0, fds1), rss_mb=(rss0 // 1024, rss1 // 1024), applet_fds=(afds0, a.proc.fds()),
            reconnects=log.count("reconnected device="), applet_cards=len(s["cards"]),
            stale=[c["stale"] for c in s["cards"]], header=s["header"], tracebacks=log.count("Traceback"))
    assert fds1 <= fds0 + 2 and not any(c["stale"] for c in s["cards"])
    # the phone was reachable again within milliseconds every time; minutes of "reconnecting"
    # (then "offline") per drop is what a flaky BLE link turns into
    assert max(lat) < 10, lat


async def test_repeated_daemon_restarts_applet_follows(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["cards"], 10)
    afds0 = a.proc.fds()
    times = []
    for _ in range(8):
        await w.daemon.stop()
        await a.until(lambda s: not s["connected"], 5)
        await w.start_daemon()
        t = time.monotonic()
        s = await a.until(lambda s: s["connected"] and s["cards"] and not s["cards"][0]["stale"], 20)
        times.append(round(s["m"] - t, 2))
    measure("soak_daemon_restarts", back_after=times, applet_fds=(afds0, a.proc.fds()))
    assert a.proc.fds() <= afds0 + 2
