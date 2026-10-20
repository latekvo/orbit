# ruff: noqa: E501
"""Kernel-level faults (Linux only, root): iptables DROP on the laptop<->phone TCP flow, so TCP's
own retransmission decides what arrives when. Run via desktop/tools/conn-jvm-docker/run.sh."""

import asyncio
import os
import shutil
import subprocess
import time

import pytest

from orbit.client import ApiError

from .harness import MEDIA, log, pair

pytestmark = pytest.mark.skipif(
    not (hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("iptables")),
    reason="needs root + iptables (Docker)",
)


def drop(port: int, on: bool) -> None:
    op = "-I" if on else "-D"
    for rule in (["INPUT", "-p", "tcp", "--dport", str(port)], ["INPUT", "-p", "tcp", "--sport", str(port)]):
        subprocess.run(["iptables", op, *rule, "-j", "DROP"], check=True)


async def direct(rig):
    phone, proxy, laptop = await rig()
    await phone.cmd("upsert", activity=MEDIA)
    dev = await pair(laptop, phone, phone.port)  # no proxy: the laptop talks to the phone's socket
    await laptop.wait_activities(lambda a: any(not x["stale"] for x in a), 5)
    return phone, laptop, dev


async def act(laptop, dev):
    return await laptop.timed_call(
        "activities.action", {"device": dev, "activity": MEDIA["id"], "action": "media.pause"}
    )


@pytest.mark.parametrize("outage", [8, 20])
async def test_netfilter_short_outage_never_applies_unanswered_actions_late(rig, outage):
    """An outage (kernel drops every packet) swallows an action and the user's retry. At 8 s the
    kernel is still retransmitting the closed connection's bytes when the path comes back."""
    phone, laptop, dev = await direct(rig)
    t0 = time.monotonic()
    drop(phone.port, True)
    try:
        dt1, r1 = await act(laptop, dev)
        retry = asyncio.create_task(act(laptop, dev))
        await asyncio.sleep(max(0.0, outage - (time.monotonic() - t0)))
    finally:
        drop(phone.port, False)
    t_heal = time.monotonic() - t0
    dt2, r2 = await retry
    await asyncio.sleep(25)
    acts = [round(ts - t0, 1) for ts, _ in phone.events("action", t0)]
    states = [(round(t - t0, 1), s) for t, s in laptop.state_log(dev, t0)]
    log(
        f"netfilter {outage} s outage: 1st {dt1:.1f}s {r1!r}; retry {dt2:.1f}s {r2!r}; healed at {t_heal:.1f}s; phone executed at {acts}; laptop states {states}"
    )
    assert isinstance(r1, ApiError) and ("may" in r1.message or r1.code == "not_connected")
    # Applied exactly as often as the user was told it succeeded: nothing arrives late.
    assert len(acts) == sum(r == {"ok": True} for r in (r1, r2))


async def test_netfilter_long_outage_does_not_apply_late(rig):
    phone, laptop, dev = await direct(rig)
    t0 = time.monotonic()
    drop(phone.port, True)
    try:
        dt1, r1 = await act(laptop, dev)
        await asyncio.sleep(max(0.0, 60 - (time.monotonic() - t0)))
    finally:
        drop(phone.port, False)
    t_heal = time.monotonic()
    t_con = await laptop.wait_state(dev, "connected", 90, t_heal)
    await asyncio.sleep(5)
    acts = [round(ts - t0, 1) for ts, _ in phone.events("action", t0)]
    closes = [(round(ts - t0, 1), e["reason"]) for ts, e in phone.events("session_closed", t0)]
    log(
        f"netfilter 60 s outage: action {dt1:.1f}s {r1!r}; reconnected {t_con - t_heal:.1f}s after heal; phone executed at {acts}; phone closes {closes}"
    )
    assert not acts
