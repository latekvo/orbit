"""What the applet shows during connectivity trouble (real applet UI offscreen, real orbitd + fakephone)."""

import asyncio
import signal
import time

import pytest
from harness import measure

MEDIA = "media:com.example.music"


def card(s, aid=MEDIA):
    return next((c for c in s["cards"] if c["id"] == aid), None)


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_states_blip_reconnecting_offline(paired, theme):
    w = paired
    a = await w.applet(theme=theme)
    await a.until(lambda s: card(s))
    await a.shot(f"{theme}-connected")
    await w.proxy.refuse()
    t = time.monotonic()
    w.proxy.reset()
    s = await a.until(lambda s: card(s) and card(s)["stale"], 5)
    await a.shot(f"{theme}-reconnecting")
    a.cmd("devices")
    await asyncio.sleep(0.3)
    await a.shot(f"{theme}-reconnecting-devices")
    a.cmd("activities")
    off = await a.until(lambda s: not s["cards"], 20)
    await asyncio.sleep(0.3)
    await a.shot(f"{theme}-offline")
    a.cmd("devices")
    await asyncio.sleep(0.3)
    await a.shot(f"{theme}-offline-devices")
    measure(f"applet_states_{theme}", stale_header=s["header"], offline_after=round(off["m"] - t, 1),
            offline_header=off["header"], offline_empty=off["empty"], tooltip=off["tooltip"])
    assert s["header"] == "Test Phone reconnecting..."
    assert off["empty"][0] == "No phone connected"


async def test_click_while_reconnecting_is_disabled_not_dropped_silently(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    before = w.phone_actions("media.next")
    await w.proxy.refuse()
    w.proxy.reset()
    s = await a.until(lambda s: card(s) and card(s)["stale"], 5)
    a.cmd(f"click {MEDIA} media.next")
    await asyncio.sleep(1)
    measure("applet_click_reconnecting", enabled=card(s)["enabled"], applied=w.phone_actions("media.next") - before,
            error=card(a.now())["error"] if card(a.now()) else None)
    assert card(s)["enabled"]["media.next"] is False


async def test_click_into_blackhole_message_and_late_apply(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    before = w.phone_actions("media.next")
    w.proxy.blackhole()
    t = time.monotonic()
    a.cmd(f"click {MEDIA} media.next")
    await a.until(lambda s: card(s)["pending"] == ["media.next"], 3)
    a.cmd(f"click {MEDIA} media.next")  # impatient second click while pending
    await asyncio.sleep(1)
    await a.shot("blackhole-pending")
    st = await a.until(lambda s: card(s)["error"], 25)
    lat, s = st["m"] - t, card(st)
    await a.shot("blackhole-error")
    w.proxy.heal()
    await asyncio.sleep(3)
    applied = w.phone_actions("media.next") - before
    measure("applet_click_blackhole", latency=round(lat, 1), error=s["error"], role=s["errorRole"],
            applied_after_heal=applied)
    assert applied <= 1  # the second click while pending is not sent
    # The phone did apply it: the message must not read as a plain failure.
    assert applied == 0 or s["errorRole"] == "notice" or "may" in s["error"]


async def test_click_then_link_reset_says_may_have_gone_through(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    w.proxy.blackhole()
    a.cmd(f"click {MEDIA} media.next")
    await a.until(lambda s: card(s)["pending"] == ["media.next"], 3)
    await asyncio.sleep(1)
    w.proxy.reset()
    w.proxy.heal()
    s = card(await a.until(lambda s: card(s) and card(s)["error"], 10))
    await a.shot("sent-then-reset")
    measure("applet_click_reset", error=s["error"], role=s["errorRole"])
    assert s["error"] == "Connection dropped - the action may have gone through"


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGKILL"])
async def test_daemon_restart_while_applet_open(paired, sig):
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    t = time.monotonic()
    await w.daemon.stop(getattr(signal, sig))
    down = await a.until(lambda s: not s["connected"], 5)
    await asyncio.sleep(0.5)
    await a.shot(f"daemon-down-{sig}")
    await asyncio.sleep(2)
    t_start = time.monotonic()
    await w.start_daemon()
    s = await a.until(lambda s: s["connected"] and card(s), 30)
    measure(f"applet_daemon_restart_{sig}", noticed_after=round(down["m"] - t, 2), header=down["header"],
            empty=down["empty"], tooltip=down["tooltip"], menu=down["menuHead"],
            back_after_start=round(s["m"] - t_start, 2))
    assert s["m"] - t_start < 6


async def test_applet_started_before_daemon(world):
    w = world
    await w.start_phone()
    a = await w.applet()
    s0 = await a.until(lambda s: "connected" in s, 5)
    await asyncio.sleep(12)  # let the backoff reach its cap
    await a.shot("before-daemon")
    t = time.monotonic()
    await w.start_daemon()
    s = await a.until(lambda s: s["connected"], 15)
    measure("applet_before_daemon", initial=s0["empty"], header=s0["header"], connected_after=round(s["m"] - t, 2))
    assert s["m"] - t < 6


async def test_frozen_daemon(paired):
    """orbitd stopped (SIGSTOP, e.g. a hung loop): what does the applet show, and for how long?"""
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    w.daemon.send_signal(signal.SIGSTOP)
    t = time.monotonic()
    try:
        a.cmd(f"click {MEDIA} media.next")
        st = await a.until(lambda s: card(s) and card(s)["error"], 40)
        s = card(st)
        await asyncio.sleep(5)
        cur = a.now()
        measure("applet_frozen_daemon", error_after=round(st["m"] - t, 1), error=s["error"],
                header_after_error=cur["header"], connected_flag=cur["connected"])
        await a.shot("frozen-daemon")
    finally:
        w.daemon.send_signal(signal.SIGCONT)
    assert s["error"] == "orbitd isn't responding"


async def test_click_while_daemon_dies_mid_action(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: card(s))
    before = w.phone_actions("media.next")
    w.proxy.blackhole()  # keep the action in flight
    a.cmd(f"click {MEDIA} media.next")
    await a.until(lambda s: card(s)["pending"] == ["media.next"], 3)
    await asyncio.sleep(0.5)
    await w.daemon.stop(signal.SIGKILL)
    w.proxy.heal()
    await asyncio.sleep(2)
    applied = w.phone_actions("media.next") - before
    s = a.now()
    measure("applet_daemon_dies_mid_action", applied=applied, ui=s["empty"], header=s["header"])
    await w.start_daemon()
    s2 = await a.until(lambda s: card(s), 30)
    msg = [x for x in a.states() if card(x) and card(x)["error"]]
    measure("applet_daemon_dies_mid_action_after", error_ever_shown=[card(x)["error"] for x in msg][:1])
    # The phone applied the press; the user was never told it may have gone through.
    assert applied == 0 or msg
