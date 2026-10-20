"""Connectivity scenarios against the real orbitd and the real Kotlin ResponderSession.

    cd desktop && uv run python tools/conn_android/scenarios.py <name> [<name> ...]   (or "all")

Each scenario prints one JSON object: measured numbers, the device-state timeline and the
phone's EXEC lines (every executed action). Rig clock: seconds since the scenario started.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rig import Rig  # noqa: E402

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


async def paired_rig(**kw) -> Rig:
    rig = Rig(**kw)
    try:
        rig.start_phone()
        await rig.wait_phone_listening()
        await rig.start_daemon()
        await rig.pair()
    except BaseException:
        await rig.close()
        raise
    rig.t0 = time.monotonic()
    rig.timeline.clear()
    return rig


async def kill_restart(gap: float, sig: int, age: float = 0) -> dict:
    rig = await paired_rig()
    try:
        if age:
            await asyncio.sleep(age)  # a session older than STABLE_SESSION is reconnected at once
            rig.t0 = time.monotonic()
            rig.timeline.clear()
        rig.mark("kill", signal.Signals(sig).name)
        rig.phone.signal(sig)
        rig.phone.p.wait(10)
        t_kill = rig.now()
        t_reco = await rig.wait_state("reconnecting", 35)
        lat_during, res_during = await rig.action()
        await asyncio.sleep(max(0, gap - (rig.now() - t_kill)))
        rig.start_phone()
        t_restart = rig.now()
        try:
            t_conn = await rig.wait_state("connected", 90)
        except TimeoutError:
            t_conn = None
        lat_after, res_after = await rig.action() if t_conn else (None, None)
        return {
            "gap_s": gap,
            "detected_after_s": round(t_reco - t_kill, 2),
            "action_during_outage": [lat_during, res_during],
            "reconnected_after_restart_s": round(t_conn - t_restart, 2) if t_conn else None,
            "action_after": [lat_after, res_after],
            "timeline": rig.timeline,
            "execs": rig.execs(),
        }
    finally:
        await rig.close()


@scenario
async def kill9_restart_1s():
    return await kill_restart(1, signal.SIGKILL)


@scenario
async def kill9_restart_8s():
    return await kill_restart(8, signal.SIGKILL)


@scenario
async def kill9_restart_20s():
    return await kill_restart(20, signal.SIGKILL)


@scenario
async def kill9_stable_restart_5s():
    return await kill_restart(5, signal.SIGKILL, age=31)


@scenario
async def kill9_stable_restart_12s():
    return await kill_restart(12, signal.SIGKILL, age=31)


@scenario
async def kill9_stable_restart_35s():
    return await kill_restart(35, signal.SIGKILL, age=31)


@scenario
async def phone_leaks_after_laptop_flaps():
    """20 orbitd kill -9/restart cycles: the phone must not accumulate threads, sockets or sessions."""
    rig = await paired_rig()
    try:
        base_threads = rig.phone_thread_names()
        base_fds = rig.phone_fds()
        times = []
        for _ in range(20):
            rig.daemon.signal(signal.SIGKILL)
            rig.daemon.p.wait(5)
            await rig.close_client_only()
            t = time.monotonic()
            await rig.start_daemon()
            await rig.wait_state("connected", 30)
            times.append(round(time.monotonic() - t, 2))
        await asyncio.sleep(2)
        names = rig.phone_thread_names()
        return {
            "reconnect_s_min_max": [min(times), max(times)],
            "threads_before": len(base_threads),
            "threads_after": len(names),
            "orbit_threads_after": sorted(n for n in names if n.startswith("orbit")),
            "tcp_fds_before_after": [base_fds, rig.phone_fds()],
        }
    finally:
        await rig.close()


@scenario
async def sigterm_restart_3s():
    return await kill_restart(3, signal.SIGTERM)


async def freeze(seconds: float, action_at: float | None) -> dict:
    """SIGSTOP the whole phone process (emulator pause / deep freeze), SIGCONT after ``seconds``."""
    rig = await paired_rig()
    try:
        rig.phone.signal(signal.SIGSTOP)
        rig.mark("SIGSTOP")
        action_task = None
        if action_at is not None:
            await asyncio.sleep(action_at)
            rig.mark("action sent")
            action_task = asyncio.create_task(rig.action())
        await asyncio.sleep(seconds - (action_at or 0))
        execs_before = len(rig.execs())
        rig.phone.signal(signal.SIGCONT)
        rig.mark("SIGCONT")
        act = await action_task if action_task else None
        if act:
            rig.mark("action answered", act)
        try:
            t_conn = await rig.wait_state("connected", 60) if rig.state() != "connected" else rig.now()
        except TimeoutError:
            t_conn = None
        await asyncio.sleep(3)
        lat_after, res_after = await rig.action()
        return {
            "freeze_s": seconds,
            "action_during_freeze": act,
            "execs_before_resume": execs_before,
            "connected_at_s": t_conn,
            "action_after": [lat_after, res_after],
            "timeline": rig.timeline,
            "execs": rig.execs(),
            "phone_close_lines": [ln for ln in rig.phone_text().splitlines() if "CLOSED" in ln][-4:],
            "daemon_lines": [ln for ln in rig.daemon_text().splitlines() if "session_closed" in ln or "reconnected" in ln][-4:],
        }
    finally:
        await rig.close()


@scenario
async def freeze_5s_action():
    return await freeze(5, 1)


@scenario
async def freeze_20s_action():
    return await freeze(20, 1)


@scenario
async def freeze_45s_action():
    return await freeze(45, 1)


@scenario
async def freeze_45s_idle():
    return await freeze(45, None)


@scenario
async def laptop_kill9():
    """orbitd dies (kernel closes its sockets): the phone must drop the session at once."""
    rig = await paired_rig()
    try:
        rig.daemon.signal(signal.SIGKILL)
        rig.daemon.p.wait(5)
        rig.mark("orbitd SIGKILL")
        await asyncio.sleep(3)
        closed = [ln for ln in rig.phone_text().splitlines() if "CLOSED" in ln]
        return {"phone_close_lines": closed[-2:], "timeline": rig.timeline}
    finally:
        await rig.close()


@scenario
async def laptop_freeze_40s():
    """orbitd SIGSTOPped (laptop lid closed / suspended): phone closes after idle; laptop on wake?"""
    rig = await paired_rig()
    try:
        rig.daemon.signal(signal.SIGSTOP)
        rig.mark("orbitd SIGSTOP")
        await asyncio.sleep(40)
        rig.daemon.signal(signal.SIGCONT)
        rig.mark("orbitd SIGCONT")
        await asyncio.sleep(0.5)
        t_conn = None
        try:
            await rig.wait_state("reconnecting", 5)
            t_conn = await rig.wait_state("connected", 30)
        except TimeoutError:
            pass
        lat, res = await rig.action()
        return {
            "connected_at_s": t_conn,
            "action_after": [lat, res],
            "phone_close_lines": [ln for ln in rig.phone_text().splitlines() if "CLOSED" in ln][-2:],
            "timeline": rig.timeline,
        }
    finally:
        await rig.close()


@scenario
async def slow_action_11s():
    """Phone takes 11 s to execute (slow app / binder): laptop answer vs the phone's real outcome."""
    rig = await paired_rig(action_delay_ms=11000)
    try:
        lat, res = await rig.action()
        await asyncio.sleep(2)
        return {"action": [lat, res], "execs": rig.execs(), "timeline": rig.timeline}
    finally:
        await rig.close()


@scenario
async def rapid_flap():
    """10 kill -9/restart cycles 2 s apart; then check reconnect time, threads/fds and exec count."""
    rig = await paired_rig()
    try:
        for _ in range(10):
            rig.phone.signal(signal.SIGKILL)
            rig.phone.p.wait(5)
            await asyncio.sleep(0.5)
            rig.start_phone()
            await asyncio.sleep(1.5)
        t_last = rig.now()
        try:
            t_conn = await rig.wait_state("connected", 120)
        except TimeoutError:
            t_conn = None
        lat, res = await rig.action()
        return {
            "reconnect_after_last_restart_s": round(t_conn - t_last, 2) if t_conn else None,
            "action_after": [lat, res],
            "retry_lines": [ln.split(": ", 1)[-1] for ln in rig.daemon_text().splitlines() if "retrying in" in ln][-6:],
            "timeline": rig.timeline[-6:],
        }
    finally:
        await rig.close()


async def main(names: list[str]) -> None:
    if names == ["all"]:
        names = list(SCENARIOS)
    for name in names:
        t = time.monotonic()
        try:
            out = await SCENARIOS[name]()
        except Exception as e:  # report and continue
            out = {"error": repr(e)}
        out = {"scenario": name, "wall_s": round(time.monotonic() - t, 1), **out}
        for d in Path("/tmp").glob("cad-*"):
            __import__("shutil").rmtree(d, ignore_errors=True)
        print(json.dumps(out, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
