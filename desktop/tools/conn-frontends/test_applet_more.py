"""More applet scenarios: Bluetooth unavailable, connect to a silent address, a frozen applet,
applet started long before orbitd."""

import asyncio
import json
import signal
import time
from pathlib import Path

import pytest
from harness import measure


async def status_rewriting_relay(listen: Path, target: Path, ble: str, error: str):
    """Unix-socket relay to the real orbitd that reports Bluetooth as ``ble`` in `status`, as
    orbitd itself does on a host whose Bluetooth is off (see test_mpris_linux::test_ble_unavailable)."""

    async def on_conn(r, w):
        ur, uw = await asyncio.open_unix_connection(str(target), limit=64 * 1024 * 1024)

        async def up():
            while line := await r.readline():
                uw.write(line)
            uw.close()

        async def down():
            while line := await ur.readline():
                msg = json.loads(line)
                if isinstance(msg.get("result"), dict) and "ble" in msg["result"]:
                    msg["result"].update(ble=ble, bleError=error)
                    line = json.dumps(msg).encode() + b"\n"
                w.write(line)
            w.close()

        await asyncio.gather(up(), down(), return_exceptions=True)

    return await asyncio.start_unix_server(on_conn, str(listen), limit=64 * 1024 * 1024)


async def test_bluetooth_unavailable_is_not_shown(paired):
    w = paired
    relay_path = w.dir / "r.sock"
    server = await status_rewriting_relay(relay_path, w.sock, "unavailable", "Bluetooth is turned off")
    try:
        from harness import Applet
        a = await Applet(relay_path, w.dir / "applet-ble").start()
        w.applets.append(a)
        await a.until(lambda s: s["cards"], 10)
        on = a.now()
        a.cmd("devices")
        await asyncio.sleep(0.4)
        await a.shot("ble-unavailable-devices")
        await w.proxy.refuse()
        w.proxy.reset()
        off = await a.until(lambda s: not s["cards"], 25)
        a.cmd("activities")
        await asyncio.sleep(0.4)
        await a.shot("ble-unavailable-offline")
        text = json.dumps(a.now()).lower()
        measure("applet_ble_unavailable", header=on["header"], menu=on["menuHead"], offline_empty=off["empty"],
                mentions_bluetooth="bluetooth is turned off" in text or "unavailable" in text)
        assert "bluetooth is turned off" in text or "bluetooth unavailable" in text
    finally:
        server.close()


async def test_add_by_address_to_silent_host(paired):
    """TCP accepts but the handshake never answers (captive portal / wrong service on the port)."""
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["connected"], 10)
    w.proxy.blackhole()
    t = time.monotonic()
    code, out, err = await w.cli("connect", f"127.0.0.1:{w.proxy.port}", timeout=60)
    lat = time.monotonic() - t
    measure("connect_silent_host", exit=code, err=err.strip(), latency=round(lat, 1))
    assert lat <= 13  # LOCAL_API.md: devices.connect answers within 12 s


async def test_frozen_applet_recovers_on_resume(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["cards"], 10)
    a.proc.send_signal(signal.SIGSTOP)
    await w.proxy.refuse()
    w.proxy.reset()
    await asyncio.sleep(20)  # meanwhile the phone went offline (15 s grace)
    rss = w.daemon.rss_kb() // 1024
    a.proc.send_signal(signal.SIGCONT)
    t = time.monotonic()
    s = await a.until(lambda s: not s["cards"] and s["header"] == "Test Phone offline", 5)
    measure("frozen_applet_resume", caught_up_after=round(s["m"] - t, 2), daemon_rss_mb=rss)


@pytest.mark.parametrize("wait", [3, 12])
async def test_applet_before_daemon_timing(world, wait):
    w = world
    await w.start_phone()
    a = await w.applet()
    await a.until(lambda s: "connected" in s, 5)
    await asyncio.sleep(wait)
    await w.start_daemon()  # returns once the socket file exists
    t = time.monotonic()
    s = await a.until(lambda s: s["connected"], 15)
    measure(f"applet_before_daemon_{wait}s", connected_after_socket=round(s["m"] - t, 2))
    assert s["m"] - t < 6
