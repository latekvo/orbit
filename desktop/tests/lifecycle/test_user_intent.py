"""User intent racing the reconnect machinery: disconnect / forget while a reconnect attempt is in flight."""

import asyncio
import time

from harness import Proxy, pair


async def test_disconnect_while_reconnect_attempt_in_flight(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    pr = Proxy(p.port)
    await pr.__aenter__()
    dev = await pair(api, p, port=pr.port)
    await asyncio.sleep(31)
    pr.connect_delay = 2.0  # slow network: the reconnect handshake takes ~2 s
    pr.live[0].rst()
    await asyncio.sleep(0.5)  # orbitd is now mid-attempt
    t0 = time.monotonic()
    _, res = await api.timed("devices.disconnect", {"id": dev})
    await asyncio.sleep(5)
    x = await api.device(dev)
    states = [(round(t - t0, 2), s) for t, s in api.state_log(dev, t0)]
    print(
        f"\ndisconnect during reconnect attempt: disconnect -> {res!r}; 5 s later state={x['state']} "
        f"connected={x['connected']}; states {states}"
    )
    assert not x["connected"], "user pressed Disconnect, orbitd connected anyway"
    await pr.close()
