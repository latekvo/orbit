"""orbit-mpris across link faults and orbitd restarts, on a real Linux session bus.
Run via linux/run.sh (Docker); skipped elsewhere."""

import asyncio
import os
import shutil
import signal
import sys
import time

import pytest
from harness import Proc, bin_path, measure

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("playerctl")),
    reason="needs Linux with a D-Bus session and playerctl (tools/conn-frontends/linux/run.sh)",
)


async def sh(*argv, timeout=40.0):
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    t = time.monotonic()
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except TimeoutError:
        p.kill()
        await p.wait()
        return -999, "", "TIMEOUT", time.monotonic() - t
    return p.returncode, out.decode().strip(), err.decode().strip(), time.monotonic() - t


PREFIX = ["org.mpris.MediaPlayer2.orbit_"]  # narrowed per test to its own phone (one bus, parallel tests)


async def players():
    _, out, _, _ = await sh("busctl", "--user", "list", "--no-pager", "--no-legend")
    return [line.split()[0] for line in out.splitlines() if line.startswith(PREFIX[0])]


async def presence(seconds, step=0.05):
    """[(t, present)] transitions over ``seconds``."""
    t0, out = time.monotonic(), []
    while time.monotonic() - t0 < seconds:
        p = bool(await players())
        if not out or out[-1][1] != p:
            out.append((round(time.monotonic() - t0, 2), p))
        await asyncio.sleep(step)
    return out


@pytest.fixture
async def mpris(paired):
    w = paired
    PREFIX[0] = f"org.mpris.MediaPlayer2.orbit_{w.phone_id[:8]}"
    m = await Proc("mpris", [bin_path("orbit-mpris"), "-v"], w.dir / "mpris.log",
                   env={"ORBIT_SOCKET": str(w.sock), "XDG_CACHE_HOME": str(w.dir / "cache")}).start()
    for _ in range(100):
        if await players():
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(m.log())
    yield w, m
    await m.stop()


async def test_blip_keeps_media_controls(mpris):
    w, m = mpris
    w.proxy.reset()
    trans = await presence(4)
    measure("mpris_blip", transitions=trans)
    # a 1 s reconnect must not make the desktop's media widget vanish and reappear
    assert trans == [(trans[0][0], True)]


async def test_media_key_during_reconnecting(mpris):
    w, m = mpris
    before = w.phone_actions("media.next")
    name = (await players())[0].removeprefix("org.mpris.MediaPlayer2.")
    w.proxy.reset()
    await asyncio.sleep(0.1)
    code, out, err, lat = await sh("playerctl", "-p", name, "next")
    await asyncio.sleep(2)
    measure("mpris_key_reconnecting", exit=code, err=err, latency=round(lat, 2),
            applied=w.phone_actions("media.next") - before)
    assert w.phone_actions("media.next") - before == 1


@pytest.mark.parametrize("down_for", [2, 15])
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGKILL"])
async def test_daemon_restart(mpris, sig, down_for):
    w, m = mpris
    await w.daemon.stop(getattr(signal, sig))
    t_down = time.monotonic()
    gone = await presence(1.5)
    await asyncio.sleep(down_for)
    await w.start_daemon()
    t_up = time.monotonic()
    t_back = None
    while time.monotonic() - t_up < 30:
        if await players():
            t_back = time.monotonic() - t_up
            break
        await asyncio.sleep(0.05)
    measure(f"mpris_daemon_restart_{sig}_{down_for}s", removed=gone, back_after_daemon_up=round(t_back, 2)
            if t_back is not None else None, mpris_alive=m.proc.returncode is None)
    assert t_back is not None and t_back < 6  # the applet's bound (RECONNECT_MAX_MS 5 s)


async def test_media_key_into_blackhole(mpris):
    w, m = mpris
    before = w.phone_actions("media.next")
    name = (await players())[0].removeprefix("org.mpris.MediaPlayer2.")
    w.proxy.blackhole()
    code, out, err, lat = await sh("playerctl", "-p", name, "next")
    w.proxy.heal()
    await asyncio.sleep(3)
    measure("mpris_key_blackhole", exit=code, err=err[:200], latency=round(lat, 2),
            applied_after_heal=w.phone_actions("media.next") - before)


async def test_ble_unavailable_is_reported(world):
    """orbitd with BLE on a host without BlueZ: what do `orbit status` and the API say?"""
    w = world
    w.daemon = await Proc("orbitd", [bin_path("orbitd"), "--no-mdns", "--name", "Test Mac", "--config-dir",
                                     str(w.dir / "cfg"), "--socket", str(w.sock)], w.dir / "orbitd.log").start()
    for _ in range(100):
        if w.sock.exists():
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(3)
    st = await w.api("status")
    code, out, err = await w.cli("status")
    measure("ble_unavailable", api_ble=st["ble"], api_error=st["bleError"], cli=out.replace("\n", " | "))
    assert st["ble"] == "unavailable"
    assert "bluetooth" in out.lower() or "ble" in out.lower(), "orbit status does not mention Bluetooth at all"
