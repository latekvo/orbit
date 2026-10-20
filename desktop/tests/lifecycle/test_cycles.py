"""1000 connect/disconnect cycles: resource growth in orbitd and the fake phone."""

import asyncio
import time

from harness import Proxy, pair

N = 1000
SAMPLES = {0, 100, 250, 500, 750, 1000}


async def sample(d, p, i):
    ds, ps = await d.stats(), await p.stats()
    return {
        "i": i,
        "d_rss": d.rss_kb(),
        "d_fds": d.open_fds(),
        "d_thr": d.os_threads(),
        "d_tasks": ds["tasks"],
        "d_sessions": ds["sessions"],
        "d_bg": ds["bg"],
        "d_api_tasks": ds["api_tasks"],
        "p_rss": p.rss_kb(),
        "p_fds": p.open_fds(),
        "p_thr": p.os_threads(),
        "p_tasks": ps["tasks"],
        "p_sessions": ps["sessions"],
        "p_conn_tasks": ps["conn_tasks"],
    }


def check(rows):
    first, last = rows[1], rows[-1]  # rows[0] is before warm-up
    for k in ("d_fds", "p_fds", "d_tasks", "p_tasks", "d_sessions", "p_sessions", "d_thr", "p_thr"):
        assert last[k] <= first[k] + 2, (k, [r[k] for r in rows])
    assert last["d_rss"] < first["d_rss"] * 1.3 + 8000, [r["d_rss"] for r in rows]
    assert last["p_rss"] < first["p_rss"] * 1.3 + 8000, [r["p_rss"] for r in rows]


async def wait_connected(api, dev, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        x = await api.device(dev)
        if x and x["connected"]:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(dev)


async def test_api_disconnect_connect_1000(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    dev = await pair(api, p)
    rows = [await sample(d, p, -1)]
    t0 = time.monotonic()
    for i in range(N + 1):
        if i in SAMPLES:
            await asyncio.sleep(0.5)
            rows.append(await sample(d, p, i))
        await api.call("devices.disconnect", {"id": dev})
        await api.call("devices.connect", {"id": dev})
        await wait_connected(api, dev)
    dt = time.monotonic() - t0
    print(f"\n{N} API disconnect/connect cycles in {dt:.0f}s")
    for r in rows:
        print(" ", r)
    check(rows)


async def test_link_reset_reconnect_1000(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    rows = [await sample(d, p, -1)]
    t0 = time.monotonic()
    for i in range(N + 1):
        if i in SAMPLES:
            await asyncio.sleep(0.5)
            rows.append(await sample(d, p, i))
        for c in pr.live:
            c.rst()
        await asyncio.sleep(0.01)
        await api.call("devices.connect", {"id": dev})  # explicit: skips the backoff
        await wait_connected(api, dev)
    dt = time.monotonic() - t0
    print(f"\n{N} RST + reconnect cycles in {dt:.0f}s")
    for r in rows:
        print(" ", r)
    check(rows)
    await pr.close()


async def test_unpaired_connect_reject_200(lab):
    d = await lab.daemon()
    p = await lab.phone(accept="reject")
    api = await lab.api(d)
    rows = [await sample(d, p, -1)]
    for i in range(201):
        if i in (0, 100, 200):
            await asyncio.sleep(0.5)
            rows.append(await sample(d, p, i))
        res = await api.call("devices.connect", {"host": "127.0.0.1", "port": p.port})
        dev = res["id"]
        for _ in range(200):
            if not await api.call("pairing.list"):
                break
            await asyncio.sleep(0.01)
    print("\n200 rejected pairings:")
    for r in rows:
        print(" ", r)
    assert dev is not None
    check(rows)


async def test_local_api_client_churn_1000(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    await pair(api, p)
    before = await d.stats()
    rss0 = d.rss_kb()
    for _ in range(N):
        c = await d.api()
        await c.call("devices.list")
        await c.close()
    await asyncio.sleep(0.5)
    after = await d.stats()
    print(f"\n{N} local API clients: before {before} rss {rss0}; after {after} rss {d.rss_kb()}")
    assert after["api_clients"] == before["api_clients"] and after["fds"] <= before["fds"] + 1
    assert after["tasks"] <= before["tasks"] + 1
