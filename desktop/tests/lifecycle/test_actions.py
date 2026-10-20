"""Action idempotency across every kind of drop: count executions on the phone exactly.

The chat reply action (``msg:chat`` / ``notif.0`` with a unique text) is non-idempotent and each
execution is visible as one ``action`` EVENT on the phone, so counts are exact.
"""

import asyncio
import time

from harness import Proxy, pair

from orbit.client import ApiError

SENT_THEN_LOST = "connection lost after the action was sent; it may have been applied"


async def setup(lab, settle=31.0):
    d = await lab.daemon()
    p = await lab.phone()
    proxy = Proxy(p.port)
    await proxy.__aenter__()
    api = await lab.api(d)
    dev = await pair(api, p, port=proxy.port)  # the pinned (manual) address is the proxy
    await api.wait_fresh_activities(dev, 5)
    if settle:
        await asyncio.sleep(settle)  # stable session: reconnect without backoff
    return d, p, api, dev, proxy


def reply(dev, text):
    return {"device": dev, "activity": "msg:chat", "action": "notif.0", "value": text}


def executed(p, text):
    return sum(1 for a in p.actions() if a["value"] == text)


def describe(res):
    return f"{res.code}: {res.message}" if isinstance(res, ApiError) else res


async def test_action_lost_before_reaching_phone(lab):
    d, p, api, dev, proxy = await setup(lab)
    conn = proxy.live[0]
    conn.freeze(up=True, down=False)
    task = asyncio.create_task(api.timed("activities.action", reply(dev, "lost-up")))
    await asyncio.sleep(0.3)
    conn.rst()
    lat, res = await task
    await api.wait_state(dev, "connected", 15, since=time.monotonic() - 0.2)
    await asyncio.sleep(5)
    print(f"\nlost before phone: {describe(res)} after {lat:.2f}s; executions {executed(p, 'lost-up')}")
    assert executed(p, "lost-up") == 0
    assert isinstance(res, ApiError) and res.message == SENT_THEN_LOST
    await proxy.close()


async def test_action_result_lost_with_rst(lab):
    d, p, api, dev, proxy = await setup(lab)
    proxy.live[0].lose_reply_after = 0.3
    lat, res = await api.timed("activities.action", reply(dev, "lost-down"))
    await api.wait_state(dev, "connected", 15, since=time.monotonic() - 0.5)
    await asyncio.sleep(5)
    n = executed(p, "lost-down")
    print(f"\nresult lost (RST): {describe(res)} after {lat:.2f}s; executions {n}")
    assert n == 1
    assert isinstance(res, ApiError) and res.message == SENT_THEN_LOST
    await proxy.close()


async def test_action_result_lost_half_open(lab):
    d, p, api, dev, proxy = await setup(lab)
    proxy.live[0].lose_reply_after = 3600  # phone executes, reply vanishes, no RST: half-open
    t0 = time.monotonic()
    lat, res = await api.timed("activities.action", reply(dev, "lost-halfopen"))
    t_rec = await api.wait_state(dev, "reconnecting", 40, since=t0)
    t_conn = await api.wait_state(dev, "connected", 20, since=t_rec)
    await asyncio.sleep(3)
    n = executed(p, "lost-halfopen")
    print(
        f"\nresult lost (half-open): {describe(res)} after {lat:.2f}s; executions {n}; "
        f"reconnecting at {t_rec - t0:.1f}s, connected at {t_conn - t0:.1f}s"
    )
    assert n == 1 and lat < 12.5
    assert isinstance(res, ApiError) and res.code == "timeout"
    await proxy.close()


async def test_action_during_fast_reconnect_is_sent_once(lab):
    d, p, api, dev, proxy = await setup(lab)
    proxy.live[0].rst()
    await asyncio.sleep(0.05)
    lat, res = await api.timed("activities.action", reply(dev, "during-reconnect"))
    await asyncio.sleep(3)
    n = executed(p, "during-reconnect")
    print(f"\naction during reconnect: {describe(res)} after {lat:.2f}s; executions {n}")
    assert res == {"ok": True} and n == 1
    await proxy.close()


async def test_action_during_slow_reconnect_is_never_sent_later(lab):
    d, p, api, dev, proxy = await setup(lab)
    proxy.mode = "refuse"
    proxy.live[0].rst()
    await asyncio.sleep(0.05)
    lat, res = await api.timed("activities.action", reply(dev, "slow-reconnect"))
    proxy.mode = "pass"
    await api.wait_state(dev, "connected", 70, since=time.monotonic())
    await asyncio.sleep(5)
    n = executed(p, "slow-reconnect")
    print(f"\naction during slow reconnect: {describe(res)} after {lat:.2f}s; executions after reconnect {n}")
    assert isinstance(res, ApiError) and res.code == "not_connected" and res.message == "reconnecting"
    assert 4.5 < lat < 6
    assert n == 0
    await proxy.close()


async def test_burst_of_actions_across_a_drop_is_reported_honestly(lab):
    """Every action answered ok ran exactly once; every 'not sent' one never ran; nothing ran twice."""
    d, p, api, dev, proxy = await setup(lab)
    texts = [f"burst-{i}" for i in range(30)]

    async def fire(i, text):
        await asyncio.sleep(i * 0.01)
        return await api.timed("activities.action", reply(dev, text))

    tasks = [asyncio.create_task(fire(i, t)) for i, t in enumerate(texts)]
    await asyncio.sleep(0.12)
    proxy.live[0].rst()
    results = await asyncio.gather(*tasks)
    await asyncio.sleep(5)
    table = []
    bad = []
    for text, (_, res) in zip(texts, results, strict=True):
        n = executed(p, text)
        table.append((text, describe(res), n))
        if n > 1:
            bad.append((text, "ran twice"))
        if res == {"ok": True} and n != 1:
            bad.append((text, "ok but ran", n))
        if isinstance(res, ApiError) and res.code == "not_connected" and n != 0:
            bad.append((text, "not sent but ran", n))
    kinds = {}
    for _, r, n in table:
        kinds.setdefault((str(r), n), 0)
        kinds[(str(r), n)] += 1
    print(f"\nburst across drop: {kinds}")
    assert not bad, bad
    await proxy.close()
