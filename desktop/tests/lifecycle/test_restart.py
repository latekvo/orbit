"""Phone app killed/restarted/reinstalled, orbitd restarted/killed: what the laptop shows and how fast it recovers."""

import asyncio
import shutil
import signal
import subprocess
import time

import pytest
from harness import ORBIT_CLI, Phone, pair

from orbit.client import ApiError


async def paired_setup(lab, **phone_kw):
    d = await lab.daemon()
    p = await lab.phone(**phone_kw)
    api = await lab.api(d)
    dev = await pair(api, p)
    await api.wait_fresh_activities(dev, 5)
    return d, p, api, dev


async def restart_phone(lab, old: Phone, down: float, sig=signal.SIGKILL, new_identity=False) -> Phone:
    await old.stop(sig)
    if new_identity:
        shutil.rmtree(old.config)
    await asyncio.sleep(down)
    p = Phone(lab.root, name=old.phonename, tag=old.name + "r", port=old.port, config=old.config)
    lab.procs.append(p)
    await p.start()
    return p


async def test_phone_killed_and_restarted_quickly(lab):
    d, p, api, dev = await paired_setup(lab)
    await asyncio.sleep(31)  # a stable session (>= 30 s) resets the reconnect backoff
    t_kill = time.monotonic()
    p2 = await restart_phone(lab, p, 0.5)
    t_up = time.monotonic()
    t_conn = await api.wait_state(dev, "connected", 20, since=t_kill + 0.01)
    t_fresh = await api.wait_fresh_activities(dev, 10)
    states = [(round(t - t_kill, 2), s) for t, s in api.state_log(dev, t_kill)]
    acts = [(round(t - t_kill, 2), n, st) for t, n, st in api.activity_log(dev, t_kill)]
    print(
        f"\nquick restart: phone back after {t_up - t_kill:.2f}s, connected after {t_conn - t_kill:.2f}s, "
        f"fresh after {t_fresh - t_kill:.2f}s\n states {states}\n activities {acts}"
    )
    assert t_conn - t_up < 2.5
    assert "offline" not in [s for _, s in states]
    assert len(p2.events("session")) == 1


@pytest.mark.parametrize("down", [5, 20, 45])
async def test_phone_down_for_a_while_reconnects_soon_after_it_is_back(lab, down):
    d, p, api, dev = await paired_setup(lab)
    await asyncio.sleep(31)
    t_kill = time.monotonic()
    await restart_phone(lab, p, down)
    t_up = time.monotonic()
    t_conn = await api.wait_state(dev, "connected", 90, since=t_kill + 0.01)
    attempts = d.grep(r"connecting to .* failed", t_kill)
    states = [(round(t - t_kill, 2), s) for t, s in api.state_log(dev, t_kill)]
    print(
        f"\nphone down {down}s: back at {t_up - t_kill:.2f}s, connected at {t_conn - t_kill:.2f}s "
        f"(lag {t_conn - t_up:.2f}s); failed attempts at "
        f"{[round(t - t_kill, 1) for t, _ in attempts]}\n states {states}"
    )
    assert t_conn - t_up < 5, f"phone reachable for {t_conn - t_up:.1f}s before orbitd reconnected"


async def test_phone_reinstalled_new_identity_same_name(lab):
    d, p, api, dev = await paired_setup(lab)
    t0 = time.monotonic()
    p2 = await restart_phone(lab, p, 0.5, new_identity=True)
    new_id = p2.device_id
    assert new_id != dev
    await asyncio.sleep(40)
    old = await api.device(dev)
    lat, res = await api.timed("activities.action", {"device": dev, "activity": "msg:chat", "action": "notif.1"})
    _, conn = await api.timed("devices.connect", {"id": dev})
    hs = p2.events("session")
    failures = d.grep(r"connecting to .* failed", t0)
    status = subprocess.run([ORBIT_CLI, "--socket", str(d.socket), "status"], capture_output=True, text=True).stdout
    print(
        f"\nreinstall: old device after 40s: {old}\n action -> {res!r}\n connect old id -> {conn!r}"
        f"\n daemon handshakes on the new phone in 40s: {len(hs)}\n last failure log: "
        f"{failures[-1][1] if failures else None}\n orbit status:\n{status}"
    )
    # What a user needs: an accurate reason, not a generic "phone offline" forever.
    assert isinstance(res, ApiError)
    assert "offline" not in res.message, "reinstalled phone reported as plain 'phone offline'"


async def test_phone_reinstalled_then_repaired_leaves_two_devices_same_name(lab):
    d, p, api, dev = await paired_setup(lab)
    p2 = await restart_phone(lab, p, 0.5, new_identity=True)
    await pair(api, p2)
    await asyncio.sleep(70)  # let the old pin's connector retry a few times
    devs = await api.call("devices.list")
    same = [x for x in devs if x["name"] == p.phonename]
    sessions = p2.events("session")
    print(
        f"\nafter re-pairing: {[(x['id'][:8], x['paired'], x['state']) for x in same]}; "
        f"handshakes on phone: {len(sessions)}; old-pin retries/min: "
        f"{len(d.grep('connecting to .* failed: .*expected ' + dev))}"
    )
    assert len(same) == 1, "the stale pin of the reinstalled phone stays listed (paired, offline) forever"


async def test_daemon_sigterm_restart(lab):
    d, p, api, dev = await paired_setup(lab)
    await api.close()
    t0 = time.monotonic()
    rc = await d.stop(signal.SIGTERM)
    t_stopped = time.monotonic()
    closed = p.events("session_closed", t0)
    d2 = await lab.daemon()
    api2 = await lab.api(d2)
    t_conn = await api2.wait_state(dev, "connected", 15)
    t_fresh = await api2.wait_fresh_activities(dev, 5)
    print(
        f"\ndaemon SIGTERM: exit {rc} in {t_stopped - t0:.2f}s; phone closed: "
        f"{[(round(e['_t'] - t0, 2), e['reason']) for e in closed]}; restarted daemon connected after "
        f"{t_conn - t0:.2f}s, fresh {t_fresh - t0:.2f}s; phone live sessions {(await p.stats())['sessions']}"
    )
    assert rc == 0 and len(closed) == 1 and "bye" in closed[0]["reason"]
    assert (await p.stats())["sessions"] == 1


async def test_daemon_kill9_stale_socket_restart(lab):
    d, p, api, dev = await paired_setup(lab)
    await api.close()
    t0 = time.monotonic()
    await d.stop(signal.SIGKILL)
    await asyncio.sleep(0.5)
    closed = p.events("session_closed", t0)
    stale = d.socket.exists()
    d2 = await lab.daemon()
    api2 = await lab.api(d2)
    t_conn = await api2.wait_state(dev, "connected", 15)
    print(
        f"\ndaemon kill -9: phone noticed after {[(round(e['_t'] - t0, 2), e['reason']) for e in closed]}; "
        f"socket file left: {stale}; restarted daemon connected after {t_conn - t0:.2f}s; "
        f"phone live sessions {(await p.stats())['sessions']}"
    )
    assert closed and closed[0]["_t"] - t0 < 1
    assert (await p.stats())["sessions"] == 1


async def test_cli_against_stale_socket_after_kill9(lab):
    d = await lab.daemon()
    await d.stop(signal.SIGKILL)
    r = subprocess.run([ORBIT_CLI, "--socket", str(d.socket), "status"], capture_output=True, text=True)
    print(f"\nCLI with dead daemon: rc={r.returncode} stderr={r.stderr.strip()!r}")
    assert r.returncode == 2 and "is it running" in r.stderr
