"""AP / client isolation (or a laptop VPN) with Bluetooth available: mDNS still lists the phone's
addresses, but TCP to every one of them black-holes. README promises "Bluetooth still works".

Real daemon, real local API, fake GATT radio (tests/ble_radio.py); LAN connects to the advertised
TEST-NET addresses are made to hang the way a dropped SYN does (open_tcp never returns), so nothing
leaves this machine. Android advertises one IPv4, its fe80, and usually 1-2 global IPv6 + a ULA.
"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ble_radio import Radio, StateLog  # noqa: E402
from daemon_helpers import until  # noqa: E402
from test_ble import FakeGattLink, ble_phone, connected_via, gatt  # noqa: E402, F401

from orbit.client import ApiError  # noqa: E402
from orbit.daemon import core  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("ORBIT_GAPS"), reason="connectivity-gap findings; set ORBIT_GAPS=1")

DEAD = ["192.0.2.10", "2001:db8::10", "2001:db8::11", "fd00:db8::10"]
INSTANCE = "P._orbit._tcp.local."


@pytest.fixture
def blackholed_lan(monkeypatch):
    attempts = []
    real = core.open_tcp

    async def open_tcp(host, port):
        if host in DEAD:
            attempts.append((time.monotonic(), host))
            await asyncio.sleep(3600)
        return await real(host, port)

    monkeypatch.setattr(core, "open_tcp", open_tcp)
    monkeypatch.setattr(core, "STABLE_SESSION", 30.0)  # the real value; ble_phone lowers it
    return attempts


def announce(daemon, dev_id, n):
    daemon.on_lan(INSTANCE, {"id": dev_id, "name": "P", "platform": "android",
                             "addresses": [(a, 47820) for a in DEAD[:n]]})  # fmt: skip


@pytest.mark.parametrize("n_dead", [1, 2, 4])
async def test_isolated_lan_with_ble_connect_and_reconnect(ble_phone, blackholed_lan, n_dead):  # noqa: F811
    daemon, api, dev_id = await ble_phone(ble_link_timeout=4.0, connect_timeout=10.0)
    loop = asyncio.get_running_loop()
    # The phone is visible over mDNS (dead addresses) before Bluetooth adverts arrive.
    announce(daemon, dev_id, n_dead)
    radio = Radio(daemon, dev_id)
    log = StateLog(api, dev_id)
    t0 = loop.time()
    await until(lambda: connected_via(daemon, dev_id, radio.address), timeout=90)
    first = loop.time() - t0

    # BLE link drops (the OPPO pattern); time until it is back over BLE.
    await asyncio.sleep(1)
    t2 = loop.time()
    old = daemon.devices[dev_id].session
    for link in radio.links():
        link.drop()
    await until(lambda: daemon.devices[dev_id].session is not old, timeout=5)
    await until(
        lambda: daemon.devices[dev_id].session not in (None, old) and connected_via(daemon, dev_id, radio.address),
        timeout=90,
    )
    reconnect = loop.time() - t2

    # On BLE, the phone re-announces over mDNS every 10 s for 40 s: each one makes orbitd try LAN again.
    before = len(blackholed_lan)
    session = daemon.devices[dev_id].session
    for _ in range(4):
        announce(daemon, dev_id, n_dead)
        await asyncio.sleep(10)
    upgrade_attempts = len(blackholed_lan) - before
    kept = daemon.devices[dev_id].session is session
    # The user clicks Disconnect, then Connect (devices.connect answers within 12 s per LOCAL_API).
    await api.call("devices.disconnect", {"id": dev_id})
    t1 = loop.time()
    try:
        res = await api.call("devices.connect", {"id": dev_id})
    except ApiError as e:
        res = (e.code, e.message)
    explicit = loop.time() - t1

    print(f"n_dead={n_dead}: first BLE session after {first:.1f} s; devices.connect -> {res} in {explicit:.1f} s; "
          f"BLE reconnect {reconnect:.1f} s; {upgrade_attempts} LAN attempts while on BLE, session kept {kept}; "
          f"states {log.states}")  # fmt: skip
    radio.stop()
    log.stop()
    assert isinstance(res, dict) and res.get("id") == dev_id, f"devices.connect failed with BLE in range: {res}"
    assert reconnect < 5, f"BLE reconnect took {reconnect:.1f} s because dead LAN addresses are tried first"
