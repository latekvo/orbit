"""Harness self-check: pair a real orbitd with a real orbit-fakephone, run one action, read stats."""

import asyncio

from harness import pair


async def test_pair_action_stats(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    dev = await pair(api, p)
    assert dev == p.device_id
    await api.wait_fresh_activities(dev, 5)
    lat, res = await api.timed(
        "activities.action", {"device": dev, "activity": "msg:chat", "action": "notif.0", "value": "hello"}
    )
    assert res == {"ok": True}, res
    await asyncio.sleep(0.2)
    acts = [a for a in p.actions() if a["value"] == "hello"]
    assert len(acts) == 1
    print("action latency", round(lat, 3), "daemon", await d.stats(), "phone", await p.stats())
