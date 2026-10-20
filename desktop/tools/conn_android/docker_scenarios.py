"""Network-fault scenarios (real orbitd <-> real Kotlin ResponderSession in Docker namespaces).

    cd desktop && uv run python tools/conn_android/docker_scenarios.py <name> ... | all
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import docker_rig  # noqa: E402
from docker_rig import DockerRig  # noqa: E402

SCENARIOS = {}
API_PORT = 0


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


async def paired(**kw) -> DockerRig:
    rig = DockerRig(API_PORT, **kw)
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


def closes(rig: DockerRig) -> dict:
    return {
        "phone_closes": [ln.split(" ", 1)[1] for ln in rig.phone_text().splitlines() if "CLOSED" in ln][-4:],
        "daemon_closes": [ln.split("cause=")[-1] for ln in rig.daemon_text().splitlines() if "session_closed" in ln][-4:],
        "daemon_reconnects": [ln.split("latency=")[-1] for ln in rig.daemon_text().splitlines() if "reconnected" in ln][-4:],
    }


def phone_ms_to_rig(rig: DockerRig, line: str, offset_ms: int) -> float:
    return round((int(line.split()[0]) - offset_ms) / 1000, 2)


async def fault(rig: DockerRig, on, off, seconds: float, action_at: float | None) -> dict:
    wall0 = int(time.time() * 1000)
    on()
    task = None
    if action_at is not None:
        await asyncio.sleep(action_at)
        rig.mark("action sent")
        task = asyncio.create_task(rig.action())
    await asyncio.sleep(seconds - (action_at or 0))
    off()
    act = await task if task else None
    t_conn = None
    try:
        t_conn = await rig.wait_state("connected", 90)
    except TimeoutError:
        pass
    await asyncio.sleep(2)
    after = await rig.action()
    execs = [t for t in (phone_ms_to_rig(rig, ln, wall0) for ln in rig.execs()) if t >= 0]
    return {
        "fault_s": seconds,
        "action_during": act,
        "connected_at_s": t_conn,
        "action_after": after,
        "exec_times_s": execs,
        "timeline": rig.timeline,
        **closes(rig),
    }


def mk(name, kind, seconds, action_at):
    async def run():
        rig = await paired()
        try:
            if kind == "blackhole":
                return await fault(rig, lambda: rig.blackhole(True), lambda: rig.blackhole(False), seconds, action_at)
            if kind in ("in", "out"):
                return await fault(rig, lambda: rig.blackhole(True, kind), lambda: rig.blackhole(False, kind), seconds, action_at)
            if kind == "wifi":
                return await fault(rig, rig.net_disconnect, rig.net_connect, seconds, action_at)
            if kind == "freeze":
                return await fault(rig, lambda: rig.freeze(True), lambda: rig.freeze(False), seconds, action_at)
        finally:
            await rig.close()
    run.__name__ = name
    SCENARIOS[name] = run


mk("blackhole_8s_action", "blackhole", 8, 1)
mk("blackhole_20s_action", "blackhole", 20, 1)
mk("blackhole_45s_action", "blackhole", 45, 1)
mk("drop_phone_to_laptop_45s", "out", 45, None)
mk("drop_laptop_to_phone_45s", "in", 45, None)
mk("wifi_off_10s_action", "wifi", 10, 1)
mk("wifi_off_40s", "wifi", 40, None)
mk("freezer_20s_action", "freeze", 20, 1)
mk("freezer_45s_action", "freeze", 45, 1)
mk("freezer_45s_action_b", "freeze", 45, 1)
mk("freezer_45s_action_c", "freeze", 45, 1)


async def netem_run(spec: str, n: int, gap: float) -> dict:
    rig = await paired()
    try:
        rig.netem(spec)
        results = []
        for _ in range(n):
            results.append(await rig.action())
            await asyncio.sleep(gap)
        rig.netem(None)
        lat = sorted(r[0] for r in results if isinstance(r[1], dict) and r[1].get("ok"))
        return {
            "netem": spec,
            "ok": len(lat),
            "failed": [r for r in results if not (isinstance(r[1], dict) and r[1].get("ok"))],
            "latency_p50_max": [lat[len(lat) // 2], lat[-1]] if lat else None,
            "execs": len(rig.execs()),
            "timeline": [t for t in rig.timeline if t[1] == "state"],
            **closes(rig),
        }
    finally:
        await rig.close()


@scenario
async def netem_loss30_delay200():
    return await netem_run("delay 200ms 50ms loss 30%", 20, 2)


@scenario
async def netem_delay3s():
    return await netem_run("delay 3000ms", 6, 2)


@scenario
async def netem_loss60():
    return await netem_run("loss 60%", 15, 3)


async def art_burst(rate: str, n: int = 4) -> dict:
    """A media app posts new album art while the link is BLE-slow: is an action stuck behind it?"""
    rig = await paired(art_burst=n)
    try:
        rig.netem(f"rate {rate}")
        wall0 = int(time.time() * 1000)
        burst = await rig.client.call(
            "activities.action", {"device": rig.device_id, "activity": "counter", "action": "notif.1"}
        )
        rig.mark("burst answered", burst)
        lat, res = await rig.action()
        rig.mark("increment answered", [lat, res])
        for _ in range(60):
            if len(rig.execs()) >= 1 and sum(1 for a in rig.activities if a["id"].startswith("media:")) == n:
                break
            await asyncio.sleep(1)
        rig.mark("burst delivered")
        rig.netem(None)
        await asyncio.sleep(1)
        retry = await rig.action()
        return {
            "rate": rate,
            "increment_during_burst": [lat, res],
            "exec_times_s": [phone_ms_to_rig(rig, ln, wall0) for ln in rig.execs()],
            "retry": retry,
            "timeline": [t for t in rig.timeline if t[1] != "activities"],
            **closes(rig),
        }
    finally:
        await rig.close()


@scenario
async def art_burst_64kbit():
    return await art_burst("64kbit")


@scenario
async def art_burst_one_art_64kbit():
    return await art_burst("64kbit", 1)


@scenario
async def art_burst_one_art_32kbit():
    return await art_burst("32kbit", 1)


@scenario
async def art_burst_256kbit():
    return await art_burst("256kbit")


@scenario
async def ip_change():
    """Wi-Fi reconnects with a different DHCP lease; orbitd only knows the manual address."""
    rig = await paired()
    try:
        rig.net_disconnect()
        await asyncio.sleep(2)
        rig.net_connect("172.29.77.11")
        try:
            await rig.wait_state("reconnecting", 40)
            t = await rig.wait_state("connected", 60)
        except TimeoutError:
            t = None
        return {"connected_at_s": t, "timeline": rig.timeline, **closes(rig)}
    finally:
        await rig.close()
        rig.net_disconnect()
        rig.net_connect()


async def main(names: list[str]) -> None:
    global API_PORT
    if names == ["all"]:
        names = list(SCENARIOS)
    API_PORT = docker_rig.setup()
    try:
        for name in names:
            t = time.monotonic()
            try:
                out = await SCENARIOS[name]()
            except Exception as e:
                out = {"error": repr(e)}
            print(json.dumps({"scenario": name, "wall_s": round(time.monotonic() - t, 1), **out}, default=str), flush=True)
    finally:
        docker_rig.teardown()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
