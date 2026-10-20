"""Multi-device: two daemons / one phone, one daemon / three flapping phones, one phone / three laptops,
duplicate sessions, and flapping links."""

import asyncio
import signal
import time

from harness import Phone, Proxy, pair


def reply(dev, text):
    return {"device": dev, "activity": "msg:chat", "action": "notif.0", "value": text}


def executed(p, text):
    return sum(1 for a in p.actions() if a["value"] == text)


async def test_two_daemons_one_phone(lab):
    d1 = await lab.daemon(name="Lap1", tag="d1")
    d2 = await lab.daemon(name="Lap2", tag="d2")
    p = await lab.phone()
    a1, a2 = await lab.api(d1), await lab.api(d2)
    dev = await pair(a1, p)
    assert await pair(a2, p) == dev
    await a1.wait_fresh_activities(dev, 5)
    await a2.wait_fresh_activities(dev, 5)
    r1, r2 = await asyncio.gather(
        a1.call("activities.action", reply(dev, "from-1")), a2.call("activities.action", reply(dev, "from-2"))
    )
    await asyncio.sleep(0.5)
    # an action from laptop 1 must show up on laptop 2 (phone upserts to every session)
    t0 = time.monotonic()
    await a1.call("activities.action", reply(dev, "seen-by-2"))
    seen = None
    for _ in range(100):
        acts = [a for a in await a2.call("activities.list") if a["id"] == "msg:chat"]
        if acts and "seen-by-2" in (acts[0].get("text") or ""):
            seen = time.monotonic() - t0
            break
        await asyncio.sleep(0.02)
    t_kill = time.monotonic()
    await d1.stop(signal.SIGKILL)
    await asyncio.sleep(3)
    d2_states = a2.state_log(dev, t_kill)
    print(
        f"\ntwo daemons: results {r1} {r2}; executions {executed(p, 'from-1')}/{executed(p, 'from-2')}; "
        f"laptop2 saw laptop1's reply after {seen and round(seen, 3)}s; phone sessions after killing d1: "
        f"{(await p.stats())['sessions']}; d2 state changes {d2_states}"
    )
    assert r1 == r2 == {"ok": True}
    assert executed(p, "from-1") == 1 and executed(p, "from-2") == 1
    assert seen is not None and d2_states == []


async def test_one_phone_three_laptops(lab):
    ds = [await lab.daemon(name=f"Lap{i}", tag=f"d{i}") for i in range(3)]
    p = await lab.phone()
    apis = [await lab.api(d) for d in ds]
    dev = None
    for a in apis:
        dev = await pair(a, p)
    res = await asyncio.gather(*(a.call("activities.action", reply(dev, f"lap-{i}")) for i, a in enumerate(apis)))
    await asyncio.sleep(0.5)
    counts = [executed(p, f"lap-{i}") for i in range(3)]
    st = await p.stats()
    print(f"\none phone, three laptops: results {res}; executions {counts}; phone sessions {st['sessions']}")
    assert all(r == {"ok": True} for r in res) and counts == [1, 1, 1] and st["sessions"] == 3


async def test_three_phones_flapping_independently(lab):
    d = await lab.daemon()
    api = await lab.api(d)
    phones = [await lab.phone(tag=f"p{i}", name=f"Phone {i}") for i in range(3)]
    proxies = []
    devs = []
    for ph in phones:
        pr = Proxy(ph.port)
        await pr.__aenter__()
        proxies.append(pr)
        devs.append(await pair(api, ph, port=pr.port))
        await api.wait_fresh_activities(devs[-1], 5)
    proxies[0].max_life = 4.0  # every new connection dies after 4 s
    proxies[1].max_life = 9.0
    for pr in proxies[:2]:
        for c in pr.live:
            asyncio.get_running_loop().call_later(pr.max_life, c.rst)
    t0 = time.monotonic()
    lat = []
    while time.monotonic() - t0 < 90:
        lt, r = await api.timed("activities.action", reply(devs[2], f"stable-{len(lat)}"))
        lat.append((lt, r))
        await asyncio.sleep(1)
    logs = [api.state_log(dv, t0) for dv in devs]
    for i, lg in enumerate(logs):
        offline = 0.0
        prev = None
        for t, s in lg + [(time.monotonic(), "end")]:
            if prev and prev[1] == "offline":
                offline += t - prev[0]
            prev = (t, s)
        print(
            f"\nphone {i}: {len(proxies[i].conns)} connections in 90 s, offline {offline:.1f}s, "
            f"states {[(round(t - t0, 1), s) for t, s in lg]}"
        )
    bad = [r for _, r in lat if r != {"ok": True}]
    print(f"stable phone: {len(lat)} actions, max latency {max(x for x, _ in lat):.3f}s, failures {bad}")
    assert all(s == "connected" for _, s in logs[2]) and not bad and max(x for x, _ in lat) < 0.5
    for pr in proxies:
        await pr.close()


async def test_link_that_drops_every_20s_stays_mostly_connected(lab):
    """A reachable phone whose link drops every 20 s (flaky Wi-Fi / OS killing the socket)."""
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    pr.max_life = 20.0
    asyncio.get_running_loop().call_later(20.0, pr.live[0].rst)
    t0 = time.monotonic()
    await asyncio.sleep(240)
    lg = api.state_log(dev, t0)
    down = {"reconnecting": 0.0, "offline": 0.0}
    prev = None
    for t, s in lg + [(time.monotonic(), "end")]:
        if prev and prev[1] in down:
            down[prev[1]] += t - prev[0]
        prev = (t, s)
    gaps = [round(lg[i + 1][0] - lg[i][0], 1) for i in range(len(lg) - 1) if lg[i][1] != "connected"]
    print(
        f"\nlink dropping every 20s for 240s: {len(pr.conns)} sessions; reconnect gaps {gaps}; "
        f"time reconnecting {down['reconnecting']:.0f}s, offline {down['offline']:.0f}s"
    )
    assert down["offline"] < 10, f"phone reachable all along but shown offline {down['offline']:.0f}s of 240s"
    await pr.close()


async def test_duplicate_routes_at_once(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pa, pb = Proxy(p.port), Proxy(p.port)
    await pa.__aenter__()
    await pb.__aenter__()
    dev = await pair(api, p, port=pa.port)
    await api.call("devices.disconnect", {"id": dev})
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    ra, rb = await asyncio.gather(
        api.call("devices.connect", {"host": "127.0.0.1", "port": pa.port}),
        api.call("devices.connect", {"host": "127.0.0.1", "port": pb.port}),
    )
    await asyncio.sleep(1)
    st = await p.stats()
    acts = [a for a in await api.call("activities.list") if a["device"] == dev]
    r = await api.call("activities.action", reply(dev, "dup"))
    await asyncio.sleep(0.5)
    closed = [e["reason"] for e in p.events("session_closed", t0)]
    print(
        f"\ntwo routes at once: {ra} {rb}; phone sessions {st['sessions']} (closed {closed}); "
        f"activities {len(acts)} ids {sorted(a['id'] for a in acts)}; action {r} executed {executed(p, 'dup')}x; "
        f"live proxies {len(pa.live)}/{len(pb.live)}"
    )
    assert st["sessions"] == 1 and len(acts) == len({a["id"] for a in acts}) and executed(p, "dup") == 1
    for x in (pa, pb):
        await x.close()


async def test_phone_restarts_while_old_session_half_open(lab):
    """Wi-Fi blip: the old TCP path silently dies (no FIN/RST), the phone app restarts and is reachable again."""
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    await asyncio.sleep(31)
    pr.live[0].freeze()  # half-open: laptop still thinks it is connected
    t0 = time.monotonic()
    await p.stop(signal.SIGKILL)
    p2 = Phone(lab.root, tag="p2", port=p.port, config=p.config)
    lab.procs.append(p2)
    await p2.start()
    t_up = time.monotonic()
    lc, rc = await api.timed("devices.connect", {"id": dev})
    la, ra = await api.timed("activities.action", reply(dev, "half-open"))
    t_rec = await api.wait_state(dev, "reconnecting", 45, since=t0)
    t_conn = await api.wait_state(dev, "connected", 30, since=t_rec)
    await asyncio.sleep(2)
    print(
        f"\nhalf-open + phone restart: phone reachable at {t_up - t0:.1f}s; devices.connect -> {rc} in {lc:.2f}s; "
        f"action -> {ra!r} in {la:.1f}s (executed {executed(p2, 'half-open')}); reconnecting at "
        f"{t_rec - t0:.1f}s, connected at {t_conn - t0:.1f}s"
    )
    assert t_conn - t_up < 5, f"phone reachable but laptop stuck on a dead session for {t_conn - t_up:.0f}s"
    await pr.close()


async def test_laptop_vanishes_phone_keeps_half_open_session(lab):
    """Laptop drops off (no FIN reaches the phone) and comes back: the phone holds two sessions meanwhile."""
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    await asyncio.sleep(31)
    t0 = time.monotonic()
    pr.live[0].rst_laptop_side_only()
    t_conn = await api.wait_state(dev, "connected", 10, since=t0 + 0.01)
    peak = (await p.stats())["sessions"]
    r = await api.call("activities.action", reply(dev, "after-vanish"))
    _, line = await p.wait_line(r"peer silent", 40, t0)
    await asyncio.sleep(0.5)
    print(
        f"\nlaptop vanished: laptop reconnected after {t_conn - t0:.2f}s; phone sessions peak {peak}; "
        f"stale one closed at +{p.grep('peer silent', t0)[0][0] - t0:.1f}s; action {r} executed "
        f"{executed(p, 'after-vanish')}x; phone sessions now {(await p.stats())['sessions']}"
    )
    assert executed(p, "after-vanish") == 1 and (await p.stats())["sessions"] == 1
    await pr.close()
