# ruff: noqa: E501
"""Minutes of flapping links and reconnect storms: resource leaks on the JVM side, duplicate actions."""

import asyncio
import collections
import os
import random
import time

from orbit.client import ApiError
from orbit.proto import FrameStream, KeyPair, initiate

from .harness import MEDIA, log
from .test_jvm_basic import setup


async def orbitd_fds(pid: int) -> int:
    p = await asyncio.create_subprocess_exec(
        "lsof", "-p", str(pid), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await p.communicate()
    return max(0, len(out.splitlines()) - 1)


async def rss_kb(pid: int) -> int:
    p = await asyncio.create_subprocess_exec("ps", "-o", "rss=", "-p", str(pid), stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    return int(out.strip() or 0)


def summary(st):
    keep = ("threads", "fds", "heapUsed", "sessions", "unauthenticated", "schedulerQueue", "accepted", "refused")
    return {k: st[k] for k in keep} | {
        "send_threads": st["threadNames"].get("orbit-send-N", 0),
        "session_threads": st["threadNames"].get("orbit-session", 0),
    }


async def test_flapping_link_for_minutes(rig):
    minutes = float(os.environ.get("ORBIT_FLAP_MINUTES", "4"))
    phone, proxy, laptop, dev = await setup(rig)
    await asyncio.sleep(2)
    base = summary(await phone.stats())
    base_fds, base_rss = await orbitd_fds(laptop.proc.pid), await rss_kb(laptop.proc.pid)
    rng = random.Random(1)
    end = time.monotonic() + minutes * 60
    outcomes = collections.Counter()
    faults = collections.Counter()
    latencies = []
    sent_values = []
    i = 0
    while time.monotonic() < end:
        i += 1
        with_timeout = asyncio.create_task(
            laptop.timed_call(
                "activities.action", {"device": dev, "activity": MEDIA["id"], "action": "media.seek", "value": i}
            )
        )
        await asyncio.sleep(rng.uniform(0.0, 3.0))
        fault = rng.choice(["rst", "fin", "rst", "refuse"])
        faults[fault] += 1
        if fault == "rst":
            proxy.rst_all()
        elif fault == "fin":
            proxy.close_all()
        else:
            proxy.accepting = "refuse"
            proxy.rst_all()
            await asyncio.sleep(rng.uniform(0.5, 2.0))
            proxy.accepting = "pass"
        dt, r = await with_timeout
        sent_values.append(i)
        key = "ok" if r == {"ok": True} else f"{r.code}: {r.message[:60]}" if isinstance(r, ApiError) else repr(r)
        outcomes[key] += 1
        latencies.append(dt)
        await asyncio.sleep(rng.uniform(0.5, 4.0))
    await laptop.wait_state(dev, "connected", 70)
    await asyncio.sleep(35)  # let every timer (idle, handshake, flush) run out
    after = summary(await phone.stats())
    fds, rss = await orbitd_fds(laptop.proc.pid), await rss_kb(laptop.proc.pid)
    values = collections.Counter(e["value"] for _, e in phone.events("action"))
    dups = {v: n for v, n in values.items() if n > 1}
    log(f"flap {minutes} min: {i} cycles, faults {dict(faults)}")
    log("action outcomes", dict(outcomes), f"max latency {max(latencies):.1f}s")
    log("jvm before", base)
    log("jvm after ", after)
    log(
        f"orbitd fds {base_fds} -> {fds}, rss {base_rss} -> {rss} kB; phone closes {len(phone.events('session_closed'))}"
    )
    log("close reasons", collections.Counter(e["reason"][:40] for _, e in phone.events("session_closed")))
    assert not dups, f"actions executed more than once: {dups}"
    assert after["send_threads"] <= 1 and after["sessions"] == 1 and after["unauthenticated"] == 0
    assert after["fds"] <= base["fds"] + 2
    assert after["schedulerQueue"] <= base["schedulerQueue"] + 2
    assert fds <= base_fds + 3


async def storm_client(port: int, stage: str, laptop_key: KeyPair) -> str:
    """One connection that dies at ``stage``."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        if stage == "connect":
            return stage
        if stage == "partial_commit":
            writer.write(b'\x00\x00\x00\x40{"t":"comm')
            await writer.drain()
            return stage
        stream = FrameStream(reader, writer)
        ch = await asyncio.wait_for(initiate(stream, laptop_key, "Storm", "linux"), 5)
        if stage == "handshake":
            return stage
        await ch.send({"t": "ready", "paired": False, "now": 0})
        await asyncio.wait_for(ch.recv(), 5)
        if stage == "ready":
            return stage
        await ch.send({"t": "pair_request"})
        return stage
    finally:
        if stage.endswith("rst"):
            pass
        writer.transport.abort() if random.random() < 0.5 else writer.close()


async def test_reconnect_storm_no_leaks(rig):
    """3000 short-lived connections (8 at a time) that die at every handshake stage, mixed RST/FIN;
    then the paired laptop must still get in, and the phone must be back to its baseline."""
    phone, proxy, laptop, dev = await setup(rig)
    await asyncio.sleep(1)
    base = summary(await phone.stats())
    stages = ["connect", "partial_commit", "handshake", "ready", "pair_request"]
    keys = [KeyPair.generate() for _ in range(8)]
    results = collections.Counter()
    sem = asyncio.Semaphore(8)
    t0 = time.monotonic()

    async def one(n):
        async with sem:
            try:
                results[await storm_client(phone.port, stages[n % len(stages)], keys[n % 8])] += 1
            except Exception as e:
                results[f"err {type(e).__name__}"] += 1

    await asyncio.gather(*(one(n) for n in range(3000)))
    t_storm = time.monotonic() - t0
    mid = summary(await phone.stats())
    dt, r = await laptop.timed_call(
        "activities.action", {"device": dev, "activity": MEDIA["id"], "action": "media.pause"}
    )
    await asyncio.sleep(35)
    after = summary(await phone.stats())
    log(f"storm: 3000 connections in {t_storm:.1f}s; results {dict(results)}")
    log("jvm before", base)
    log("jvm mid   ", mid)
    log("jvm after ", after)
    log(f"paired laptop action during/after storm: {dt:.2f}s {r}")
    assert r == {"ok": True}
    assert after["sessions"] == 1 and after["unauthenticated"] == 0
    assert after["send_threads"] <= 1
    assert after["fds"] <= base["fds"] + 2
