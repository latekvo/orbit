"""Connectivity tests: real orbitd against the real Kotlin ResponderSession (jvmphone).

Slow (each test 10-100 s), so outside the default testpaths:

    (cd ../android/companion && ./gradlew :jvmphone:installDist)
    uv run pytest tools/conn_android -v                 # host-only tests
    CONN_DOCKER=1 uv run pytest tools/conn_android -v   # plus Docker network-fault tests

Tests named ``test_bug_*`` reproduce open findings and fail on the code they were written
against; the rest are regression checks of behavior that works.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from rig import JVMPHONE, Rig  # noqa: E402

pytestmark = pytest.mark.skipif(not JVMPHONE.exists(), reason="jvmphone not built")


async def paired_rig(**kw) -> Rig:
    rig = Rig(**kw)
    try:
        rig.start_phone()
        await rig.wait_phone_listening()
        await rig.start_daemon()
        await rig.pair()
    except BaseException:
        await rig.close()
        rig.cleanup()
        raise
    rig.t0 = time.monotonic()
    rig.timeline.clear()
    return rig


@pytest.fixture
async def rig():
    r = await paired_rig()
    yield r
    await r.close()
    r.cleanup()


# ---- open findings ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="responder side: the phone applies an action it reads after the laptop gave up")
async def test_bug_timed_out_action_is_applied_later(rig):
    """Phone frozen 20 s (app freezer / doze / emulator pause): the action draws no answer, the
    phone may apply it on resume; the user's retry must not make it two."""
    rig.phone.signal(signal.SIGSTOP)
    await asyncio.sleep(1)
    task = asyncio.create_task(rig.action())
    await asyncio.sleep(19)
    rig.phone.signal(signal.SIGCONT)
    _, result = await task
    assert "may" in str(result)
    await asyncio.sleep(2)
    _, retry = await rig.action()
    assert retry == {"ok": True}
    # One press failed, one succeeded: the counter must have moved once.
    assert len(rig.execs()) == 1, rig.execs()


@pytest.mark.xfail(strict=True, reason="responder side: the phone applies an action it reads after the laptop gave up")
async def test_bug_action_from_session_declared_dead_is_applied(rig):
    """Phone frozen 45 s: orbitd drops the session 5 s after the unanswered action and shows the
    device offline later. On resume the phone still reads and applies the action buffered on that
    dead session (only the phone can refuse it).
    (Racy on the phone: its ticker or its reader may run first; it failed 3/4 runs here.)"""
    rig.phone.signal(signal.SIGSTOP)
    await asyncio.sleep(1)
    task = asyncio.create_task(rig.action())
    await asyncio.sleep(44)
    assert rig.state() == "offline"
    rig.phone.signal(signal.SIGCONT)
    _, result = await task
    assert "may" in str(result)
    await rig.wait_state("connected", 20)
    await asyncio.sleep(1)
    assert rig.execs() == [], "an action answered as timed out on a dead session was applied"


async def test_bug_phone_back_within_grace_still_goes_offline(rig):
    """Companion restarts within the 15 s grace (sticky restart, app update) on a manual route:
    the reconnect waits for the backoff slot at t=15 s, so the device flips to offline and its
    activities vanish just before coming back."""
    await asyncio.sleep(31)  # a stable session: reconnect starts at once with a 1 s backoff
    rig.t0 = time.monotonic()
    rig.timeline.clear()
    rig.phone.signal(signal.SIGKILL)
    rig.phone.p.wait(5)
    await asyncio.sleep(8)
    rig.start_phone()
    await rig.wait_phone_listening()
    back = rig.now()
    await rig.wait_state("connected", 60)
    states = [d for _, k, d in rig.timeline if k == "state"]
    assert "offline" not in states, (back, rig.timeline)


async def test_bug_phone_back_after_grace_reconnects_late(rig):
    """Companion down 35 s (reboot, slow restart) on a manual route: the backoff has grown to
    32 s, so the laptop reconnects ~28 s after the phone is listening again."""
    await asyncio.sleep(31)
    rig.phone.signal(signal.SIGKILL)
    rig.phone.p.wait(5)
    await asyncio.sleep(35)
    rig.start_phone()
    await rig.wait_phone_listening()
    back = rig.now()
    t = await rig.wait_state("connected", 90)
    assert t - back < 5, f"reconnected {t - back:.1f} s after the phone was back"


# ---- regression checks -----------------------------------------------------------------------


async def test_kill9_detected_at_once_and_recovers(rig):
    rig.phone.signal(signal.SIGKILL)
    rig.phone.p.wait(5)
    t = await rig.wait_state("reconnecting", 2)
    assert t < 1
    assert all(a.get("stale") for a in rig.activities)
    _, during = await rig.action()
    assert during == "not_connected: reconnecting"
    rig.start_phone()
    await rig.wait_state("connected", 15)
    _, after = await rig.action()
    assert after == {"ok": True}
    assert len(rig.execs()) == 1


async def test_sigterm_sends_bye(rig):
    rig.phone.signal(signal.SIGTERM)
    rig.phone.p.wait(10)
    await rig.wait_state("reconnecting", 3)
    assert "phone said bye (service stopped)" in rig.daemon_text()


async def test_short_freeze_action_answered(rig):
    rig.phone.signal(signal.SIGSTOP)
    await asyncio.sleep(1)
    task = asyncio.create_task(rig.action())
    await asyncio.sleep(4)
    rig.phone.signal(signal.SIGCONT)
    lat, result = await task
    assert result == {"ok": True} and lat < 5
    assert len(rig.execs()) == 1


async def test_laptop_suspended_40s_recovers(rig):
    rig.daemon.signal(signal.SIGSTOP)
    await asyncio.sleep(40)
    assert "idle timeout" in rig.phone_text()
    rig.daemon.signal(signal.SIGCONT)
    t0 = time.monotonic()
    while "reconnected device=" not in rig.daemon_text():
        assert time.monotonic() - t0 < 3, "orbitd did not reconnect within 3 s of waking"
        await asyncio.sleep(0.05)
    await rig.wait_state("connected", 1)
    _, after = await rig.action()
    assert after == {"ok": True}


async def test_phone_does_not_leak_on_laptop_restarts(rig):
    before = rig.phone_thread_names()
    for _ in range(10):
        rig.daemon.signal(signal.SIGKILL)
        rig.daemon.p.wait(5)
        await rig.close_client_only()
        await rig.start_daemon()
        await rig.wait_state("connected", 30)
    await asyncio.sleep(2)
    after = rig.phone_thread_names()
    assert sum(n.startswith("orbit-send") for n in after) == 1
    assert sum(n.startswith("orbit-send") for n in before) == 1
    assert rig.phone_fds() <= 3


# ---- Docker network faults --------------------------------------------------------------------

docker_only = pytest.mark.skipif(os.environ.get("CONN_DOCKER") != "1", reason="set CONN_DOCKER=1")


@pytest.fixture(scope="module")
def docker_api_port():
    import docker_rig

    port = docker_rig.setup()
    yield port
    docker_rig.teardown()


@pytest.fixture
async def drig(docker_api_port):
    from docker_rig import DockerRig

    r = DockerRig(docker_api_port)
    try:
        r.start_phone()
        await r.wait_phone_listening()
        await r.start_daemon()
        await r.pair()
    except BaseException:
        await r.close()
        raise
    yield r
    await r.close()


@docker_only
async def test_bug_wifi_blip_8s_double_applies(drig):
    """An 8 s blackhole (Wi-Fi power save, roaming): the session survives, the action is answered
    `timeout`, TCP retransmission delivers it ~6 s after the link is back and the user's retry is
    applied too."""
    drig.blackhole(True)
    await asyncio.sleep(1)
    task = asyncio.create_task(drig.action())
    await asyncio.sleep(7)
    drig.blackhole(False)
    _, result = await task
    assert result == "timeout: phone did not respond"
    await asyncio.sleep(2)
    _, retry = await drig.action()
    assert retry == {"ok": True}
    assert len(drig.execs()) == 1, drig.execs()


@docker_only
async def test_lossy_link_actions_all_succeed(drig):
    drig.netem("delay 200ms 50ms loss 30%")
    results = [await drig.action() for _ in range(10)]
    drig.netem(None)
    assert all(r == {"ok": True} for _, r in results), results
    assert len(drig.execs()) == 10


@docker_only
async def test_bug_action_result_queued_behind_album_art(drig):
    """BLE-like 8 KB/s link, a media app posts new album art (4 x 30 KB): the phone applies the
    action at once, but its action_result waits behind ~160 KB of art on the single send queue,
    so the laptop reports `timeout` for an action that was applied ~10 s earlier."""
    drig.netem("rate 64kbit")
    await drig.client.call("activities.action", {"device": drig.device_id, "activity": "counter", "action": "notif.1"})
    t = time.monotonic()
    _, result = await drig.action()
    applied = len(drig.execs())
    drig.netem(None)
    assert applied == 1
    assert result == {"ok": True}, f"{result} after {time.monotonic() - t:.1f} s although applied"
