"""Connectivity scenarios: real orbitd against the companion app on an Android emulator.

    cd desktop && uv run python tools/conn_android/emu_scenarios.py <name> [<name> ...]

See emu_rig.py for the prerequisites. Each scenario prints one JSON object: the device-state
timeline (seconds since the fault), action outcomes with latency, the exact OrbitSample executions,
the companion's new events.log lines and orbitd's session lines.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from emu_rig import (  # noqa: E402
    LISTENER,
    PKG,
    ROUTE,
    SAMPLE,
    EmuRig,
    adb,
    forward,
    pid,
    sh,
    ui_tap_text,
    wait_boot,
)

SCENARIOS: dict[str, Callable[[EmuRig], Awaitable[dict]]] = {}
DAEMON_NEEDLES = ("session_closed", "reconnected", "did not come back", "nothing answers", "not sent", "retrying in")


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


async def wait_until(pred: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def wait_state(rig: EmuRig, state: str, timeout: float) -> float | None:
    try:
        return await rig.wait_state(state, timeout)
    except TimeoutError:
        return None


async def wait_not_connected(rig: EmuRig, timeout: float) -> float | None:
    ok = await wait_until(lambda: rig.state() != "connected", timeout, 0.02)
    return rig.now() if ok else None


async def wait_new_pid(old: str, timeout: float) -> tuple[float | None, str]:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        p = await asyncio.to_thread(pid)
        if p and p != old:
            return round(time.monotonic() - t, 2), p
        await asyncio.sleep(0.25)
    return None, await asyncio.to_thread(pid)


def ensure_timer() -> None:
    adb(
        "shell",
        "am",
        "broadcast",
        "-n",
        f"{SAMPLE}/.ActionReceiver",
        "--es",
        "action",
        "timer.start",
        "--el",
        "value",
        "36000000",
    )


def ensure_producers() -> None:
    sh(f"am start -n {SAMPLE}/.MainActivity --es start media,download,timer,reply")
    time.sleep(1.5)
    ensure_timer()
    sh("input keyevent KEYCODE_HOME")


async def ready(rig: EmuRig, timeout: float = 60) -> None:
    """Connected with a fresh timer activity; restarts the producers once if the timer is missing."""
    await rig.wait_state("connected", timeout)
    try:
        await rig.wait_activity_fresh(5)
    except TimeoutError:
        await asyncio.to_thread(ensure_producers)
        await rig.wait_activity_fresh(20)


def report(rig: EmuRig, **extra) -> dict:
    return {
        **extra,
        "timeline": rig.timeline,
        "execs": len(rig.execs()),
        "exec_lines": [ln.split("OrbitSample: ", 1)[-1][:60] for ln in rig.execs()],
        "events_log": rig.new_events(),
        "orbitd": [ln.split(" ", 2)[-1][:220] for ln in rig.daemon_lines(*DAEMON_NEEDLES)][-12:],
    }


async def act(rig: EmuRig, label: str) -> list:
    lat, res = await rig.action()
    rig.mark(f"action {label}", [lat, res])
    return [lat, res]


# ---- 1. process deaths -------------------------------------------------------------------------


async def death(rig: EmuRig, how: str, cmd: str, detect_s: float = 35, back_s: float = 90, reopen: bool = True) -> dict:
    await ready(rig)
    old = await asyncio.to_thread(pid)
    rig.reset_clock()
    rig.mark("fault", how)
    out = await asyncio.to_thread(sh, cmd)
    t_detect = await wait_not_connected(rig, detect_s)
    during = await act(rig, "during") if t_detect is not None else None
    t_pid, new = await wait_new_pid(old, back_s if t_detect is not None else 3)
    t_conn = await wait_state(rig, "connected", back_s) if t_detect is not None else None
    reopened = None
    if t_detect is not None and t_conn is None and reopen:
        # The user opens the app: the only way out of the force-stopped state.
        rig.mark("user opens app")
        t_open = rig.now()
        await asyncio.to_thread(sh, f"am start -n {PKG}/.MainActivity")
        t2 = await wait_state(rig, "connected", 60)
        reopened = round(t2 - t_open, 2) if t2 else None
        await asyncio.to_thread(sh, "input keyevent KEYCODE_HOME")
    try:
        await rig.wait_activity_fresh(20)
    except TimeoutError:
        pass
    after = await act(rig, "after")
    await asyncio.sleep(1)
    return report(
        rig,
        cmd=cmd,
        cmd_output=out[:200],
        pid_before_after=[old, new],
        dropped_after_s=t_detect,
        new_pid_after_s=t_pid,
        reconnected_at_s=t_conn,
        reconnect_after_user_open_s=reopened,
        action_during=during,
        action_after=after,
    )


@scenario
async def force_stop(rig):
    return await death(rig, "am force-stop", f"am force-stop {PKG}")


@scenario
async def am_kill(rig):
    return await death(rig, "am kill", f"am kill {PKG}", detect_s=10)


@scenario
async def kill9(rig):
    p = await asyncio.to_thread(pid)
    return await death(rig, "kill -9", f"su 0 kill -9 {p}")


@scenario
async def am_crash(rig):
    return await death(rig, "am crash", f"am crash {PKG}")


# ---- 2. network toggles -----------------------------------------------------------------------


async def toggle(rig: EmuRig, name: str, off: str, on: str, hold: float = 20, action_at: float = 3) -> dict:
    await ready(rig)
    rig.reset_clock()
    rig.mark(f"{name} off")
    await asyncio.to_thread(sh, off)
    await asyncio.sleep(action_at)
    rig.mark("action sent")
    task = asyncio.create_task(rig.action())
    await asyncio.sleep(hold - action_at)
    execs_before_on = len(rig.execs())
    state_before_on = rig.state()
    rig.mark(f"{name} on")
    await asyncio.to_thread(sh, on)
    t_on = rig.now()
    during = list(await task)
    rig.mark("action answered", during)
    t_conn = await wait_state(rig, "connected", 60)
    await asyncio.sleep(3)
    execs_after_on = len(rig.execs())
    after = await act(rig, "after")
    await asyncio.sleep(1)
    return report(
        rig,
        hold_s=hold,
        action_during=during,
        state_before_on=state_before_on,
        execs_before_on=execs_before_on,
        execs_after_on_before_retry=execs_after_on,
        connected_after_on_s=round(t_conn - t_on, 2) if t_conn else None,
        action_after=after,
        lan_session_closes=len(rig.daemon_lines("session_closed")),
    )


@scenario
async def wifi_toggle(rig):
    return await toggle(rig, "wifi", "svc wifi disable", "svc wifi enable")


@scenario
async def airplane_45s(rig):
    return await toggle(
        rig, "airplane", "cmd connectivity airplane-mode enable", "cmd connectivity airplane-mode disable", 45, 8
    )


@scenario
async def airplane_20s_late_action(rig):
    return await toggle(
        rig, "airplane", "cmd connectivity airplane-mode enable", "cmd connectivity airplane-mode disable", 20, 8
    )


@scenario
async def wifi_45s(rig):
    return await toggle(rig, "wifi", "svc wifi disable", "svc wifi enable", 45, 8)


@scenario
async def data_toggle(rig):
    return await toggle(rig, "mobile data", "svc data disable", "svc data enable")


@scenario
async def airplane_toggle(rig):
    return await toggle(
        rig, "airplane", "cmd connectivity airplane-mode enable", "cmd connectivity airplane-mode disable"
    )


# ---- 3. doze, standby buckets, battery saver ---------------------------------------------------


async def hold_state(
    rig: EmuRig, name: str, enter: list[str], leave: list[str], hold: float, probe_every: float = 30
) -> dict:
    await ready(rig)
    rig.reset_clock()
    rig.mark(f"{name} enter")
    entered = [await asyncio.to_thread(sh, c) for c in enter]
    actions = []
    t = 0.0
    while t < hold:
        step = min(probe_every, hold - t)
        await asyncio.sleep(step)
        t += step
        actions.append(await act(rig, f"in {name} t={t:.0f}"))
    state_in = rig.state()
    rig.mark(f"{name} leave")
    left = [await asyncio.to_thread(sh, c) for c in leave]
    t_conn = await wait_state(rig, "connected", 60)
    after = await act(rig, "after")
    await asyncio.sleep(1)
    return report(
        rig,
        enter_output=[e[:160] for e in entered],
        leave_output=[e[:160] for e in left],
        actions_while_in=actions,
        state_at_end_of_hold=state_in,
        connected_after_leave_s=t_conn,
        action_after=after,
        session_closes=len(rig.daemon_lines("session_closed")),
    )


DOZE_IN = [
    "dumpsys battery unplug",
    "input keyevent 223",
    "dumpsys deviceidle force-idle deep",
    "dumpsys deviceidle get deep",
]
DOZE_OUT = ["dumpsys deviceidle unforce", "dumpsys battery reset", "input keyevent 224", "wm dismiss-keyguard"]


@scenario
async def doze_deep_exempt(rig):
    return await hold_state(rig, "deep doze (exempt)", DOZE_IN, DOZE_OUT, 120)


@scenario
async def doze_deep_not_exempt(rig):
    out = await hold_state(
        rig, "deep doze (no exemption)", ["dumpsys deviceidle whitelist -" + PKG, *DOZE_IN], DOZE_OUT, 120
    )
    sh(f"dumpsys deviceidle whitelist +{PKG}")
    return out


@scenario
async def doze_light_not_exempt(rig):
    light_in = [
        "dumpsys deviceidle whitelist -" + PKG,
        "dumpsys battery unplug",
        "input keyevent 223",
        "dumpsys deviceidle force-idle light",
        "dumpsys deviceidle get light",
    ]
    out = await hold_state(rig, "light doze (no exemption)", light_in, DOZE_OUT, 90)
    sh(f"dumpsys deviceidle whitelist +{PKG}")
    return out


@scenario
async def doze_step_not_exempt(rig):
    """Walks the deep-idle state machine with `step` (the path a real phone takes)."""
    steps = ["dumpsys deviceidle whitelist -" + PKG, "dumpsys battery unplug", "input keyevent 223"]
    steps += ["dumpsys deviceidle step deep"] * 6
    out = await hold_state(rig, "doze stepped (no exemption)", steps, DOZE_OUT, 90)
    sh(f"dumpsys deviceidle whitelist +{PKG}")
    return out


@scenario
async def bucket_rare(rig):
    return await hold_state(
        rig,
        "bucket rare",
        [f"am set-standby-bucket {PKG} rare", f"am get-standby-bucket {PKG}"],
        [f"am set-standby-bucket {PKG} active"],
        90,
    )


@scenario
async def bucket_restricted(rig):
    return await hold_state(
        rig,
        "bucket restricted",
        [f"am set-standby-bucket {PKG} restricted", f"am get-standby-bucket {PKG}"],
        [f"am set-standby-bucket {PKG} active"],
        90,
    )


@scenario
async def battery_saver(rig):
    return await hold_state(
        rig,
        "battery saver",
        ["dumpsys battery unplug", "settings put global low_power 1", "settings get global low_power"],
        ["settings put global low_power 0", "dumpsys battery reset"],
        90,
    )


@scenario
async def battery_saver_screen_off(rig):
    return await hold_state(
        rig,
        "battery saver + screen off",
        ["dumpsys battery unplug", "settings put global low_power 1", "input keyevent 223"],
        ["settings put global low_power 0", "dumpsys battery reset", "input keyevent 224", "wm dismiss-keyguard"],
        120,
    )


# ---- 4. screen off ----------------------------------------------------------------------------


@scenario
async def screen_off_6min(rig):
    return await hold_state(
        rig, "screen off", ["input keyevent 223"], ["input keyevent 224", "wm dismiss-keyguard"], 360, 60
    )


@scenario
async def screen_off_6min_unplugged(rig):
    return await hold_state(
        rig,
        "screen off unplugged",
        ["dumpsys battery unplug", "input keyevent 223"],
        ["dumpsys battery reset", "input keyevent 224", "wm dismiss-keyguard"],
        360,
        60,
    )


# ---- 5. whole-phone freeze --------------------------------------------------------------------


async def freeze(rig: EmuRig, seconds: float, action_at: float | None = 1.0) -> dict:
    await ready(rig)
    rig.reset_clock()
    rig.mark("avd stop")
    await asyncio.to_thread(adb, "emu", "avd", "stop")
    task = None
    if action_at is not None:
        await asyncio.sleep(action_at)
        rig.mark("action sent")
        task = asyncio.create_task(rig.action())
    await asyncio.sleep(seconds - (action_at or 0))
    state_before_resume = rig.state()
    rig.mark("avd start")
    await asyncio.to_thread(adb, "emu", "avd", "start")
    during = None
    if task:
        during = list(await task)
        rig.mark("action answered", during)
    t_conn = await wait_state(rig, "connected", 60)
    await asyncio.sleep(3)
    execs_after_resume = len(rig.execs())
    retry = await act(rig, "retry")
    await asyncio.sleep(1.5)
    return report(
        rig,
        freeze_s=seconds,
        state_before_resume=state_before_resume,
        action_during_freeze=during,
        execs_after_resume_before_retry=execs_after_resume,
        connected_at_s=t_conn,
        action_retry=retry,
        phone_closes=[e for e in rig.new_events() if "closed" in e],
    )


@scenario
async def pause_20s(rig):
    return await freeze(rig, 20)


@scenario
async def pause_60s(rig):
    return await freeze(rig, 60)


@scenario
async def pause_8s(rig):
    return await freeze(rig, 8)


# ---- 6. app update ----------------------------------------------------------------------------

APK = Path(__file__).resolve().parents[3] / "android/companion/app/build/outputs/apk/debug/app-debug.apk"


@scenario
async def app_update(rig):
    await ready(rig)
    old = await asyncio.to_thread(pid)
    rig.reset_clock()
    rig.mark("install -r")
    out = await asyncio.to_thread(adb, "install", "-r", str(APK), timeout=120)
    rig.mark("installed")
    t_detect = await wait_not_connected(rig, 30)
    t_pid, new = await wait_new_pid(old, 60)
    t_conn = await wait_state(rig, "connected", 90)
    try:
        await rig.wait_activity_fresh(20)
    except TimeoutError:
        pass
    after = await act(rig, "after")
    await asyncio.sleep(1)
    return report(
        rig,
        install=out.strip()[-60:],
        pid_before_after=[old, new],
        dropped_at_s=t_detect,
        new_pid_after_install_s=t_pid,
        reconnected_at_s=t_conn,
        action_after=after,
    )


# ---- 7. reboot --------------------------------------------------------------------------------


async def reboot(rig: EmuRig) -> float:
    rig.mark("reboot")
    await asyncio.to_thread(adb, "reboot")
    boot = await asyncio.to_thread(wait_boot)
    rig.mark("boot completed")
    await asyncio.to_thread(forward)
    await asyncio.to_thread(sh, "wm dismiss-keyguard")
    return boot


@scenario
async def reboot_running(rig):
    await ready(rig)
    rig.reset_clock()
    t_boot_wall = await reboot(rig)
    t_booted = rig.now()
    t_pid = None
    ok = await wait_until(lambda: bool(pid()), 60, 0.5)
    if ok:
        t_pid = rig.now()
    t_conn = await wait_state(rig, "connected", 120)
    out = report(
        rig, boot_s=round(t_boot_wall, 1), booted_at_s=t_booted, companion_pid_at_s=t_pid, reconnected_at_s=t_conn
    )
    # The reboot ended the producers (and cleared logcat); start them again for the action.
    await asyncio.to_thread(ensure_producers)
    try:
        await rig.wait_activity_fresh(30)
        out["action_after"] = await act(rig, "after")
    except TimeoutError:
        out["action_after"] = "no timer activity"
    await asyncio.sleep(1)
    out["execs"] = len(rig.execs())
    out["events_log"] = rig.new_events()
    return out


# ---- 8. user Stop -----------------------------------------------------------------------------


def press(text: str) -> bool:
    sh(f"am start -n {PKG}/.MainActivity")
    time.sleep(1.5)
    return ui_tap_text(text)


@scenario
async def user_stop(rig):
    await ready(rig)
    rig.reset_clock()
    rig.mark("press Stop")
    pressed = await asyncio.to_thread(press, "Stop")
    t_drop = await wait_not_connected(rig, 10)
    await asyncio.sleep(3)
    state_after_stop = rig.state()
    during = await act(rig, "after stop")
    rig.mark("rotate")
    await asyncio.to_thread(sh, "settings put system accelerometer_rotation 0; settings put system user_rotation 1")
    await asyncio.sleep(3)
    await asyncio.to_thread(sh, "settings put system user_rotation 0")
    await asyncio.sleep(3)
    await asyncio.to_thread(sh, "input keyevent KEYCODE_HOME")
    rig.mark("laptop connect attempt")
    try:
        await rig.client.call("devices.connect", {"id": rig.device_id})
        connect_res = "connected"
    except Exception as e:  # noqa: BLE001
        connect_res = repr(e)
    rig.mark("listener rebind (disallow/allow)")
    await asyncio.to_thread(
        sh, f"cmd notification disallow_listener {LISTENER}; sleep 2; cmd notification allow_listener {LISTENER}"
    )
    await asyncio.sleep(5)
    pid_before_reboot = await asyncio.to_thread(pid)
    await reboot(rig)
    await asyncio.sleep(20)
    stayed = rig.state()
    svc = await asyncio.to_thread(sh, f"dumpsys activity services {PKG} | grep -c OrbitService")
    rig.mark("press Start")
    pressed_start = await asyncio.to_thread(press, "Start")
    t_start = rig.now()
    t_conn = await wait_state(rig, "connected", 60)
    await asyncio.to_thread(sh, "input keyevent KEYCODE_HOME")
    await asyncio.to_thread(ensure_producers)
    try:
        await rig.wait_activity_fresh(30)
        after = await act(rig, "after start")
    except TimeoutError:
        after = "no timer activity"
    await asyncio.sleep(1)
    ev = rig.new_events()
    stopped = ev[ev.index("service stop by user") :] if "service stop by user" in ev else []
    stopped = stopped[: next((i for i, e in enumerate(stopped) if "from Start button" in e), len(stopped))]
    return report(
        rig,
        sessions_opened_while_stopped=sum(" open (" in e for e in stopped),
        state_3s_after_stop=state_after_stop,
        pressed_stop=pressed,
        dropped_at_s=t_drop,
        action_after_stop=during,
        laptop_connect_while_stopped=connect_res,
        pid_before_reboot=pid_before_reboot,
        state_20s_after_reboot=stayed,
        orbitservice_lines_after_reboot=svc,
        pressed_start=pressed_start,
        reconnect_after_start_s=round(t_conn - t_start, 2) if t_conn else None,
        action_after_start=after,
    )


# ---- 9. notification access ---------------------------------------------------------------------


@scenario
async def listener_revoke(rig):
    await ready(rig)
    old_timer = rig.timer()
    rig.reset_clock()
    rig.mark("disallow_listener")
    await asyncio.to_thread(sh, f"cmd notification disallow_listener {LISTENER}")
    cleared = await wait_until(lambda: not rig.mine(), 15, 0.02)
    t_cleared = rig.now() if cleared else None
    left = [(a["id"], [x["id"] for x in a["actions"]]) for a in rig.mine()]
    stale_action = list(
        await rig.action(old_timer["id"], next(a["id"] for a in old_timer["actions"] if a["label"] == "+1 min"))
    )
    rig.mark("stale action", stale_action)
    await asyncio.sleep(5)
    state_revoked = rig.state()
    rig.mark("allow_listener")
    await asyncio.to_thread(sh, f"cmd notification allow_listener {LISTENER}")
    back = await wait_until(lambda: rig.timer() is not None, 20, 0.02)
    t_back = rig.now() if back else None
    after = await act(rig, "after regrant")
    await asyncio.sleep(1)
    return report(
        rig,
        cleared_at_s=t_cleared,
        left_while_revoked=left,
        action_on_old_activity=stale_action,
        state_while_revoked=state_revoked,
        restored_at_s=t_back,
        action_after=after,
        session_closes=len(rig.daemon_lines("session_closed")),
    )


# ---- 10. Bluetooth toggle ---------------------------------------------------------------------


@scenario
async def bluetooth_toggle(rig):
    await ready(rig)
    rig.reset_clock()
    rig.mark("bt disable")
    await asyncio.to_thread(sh, "svc bluetooth disable")
    await asyncio.sleep(8)
    during = await act(rig, "bt off")
    rig.mark("bt enable")
    await asyncio.to_thread(sh, "svc bluetooth enable")
    await asyncio.sleep(10)
    after = await act(rig, "bt on")
    await asyncio.sleep(1)
    ev = rig.new_events()
    return report(
        rig,
        action_bt_off=during,
        action_bt_on=after,
        gatt_closed=sum("gatt server closed" in e for e in ev),
        gatt_opened=sum("gatt server opened" in e for e in ev),
        lan_session_closes=len(rig.daemon_lines("session_closed")),
    )


async def main(names: list[str]) -> None:
    forward()
    rig = EmuRig()
    await rig.start_daemon()
    try:
        await rig.ensure_connected(60)
        for name in names:
            t = time.monotonic()
            try:
                out = await SCENARIOS[name](rig)
            except Exception as e:  # report and continue
                out = {"error": repr(e), "timeline": rig.timeline}
            out = {"scenario": name, "route": ROUTE, "wall_s": round(time.monotonic() - t, 1), **out}
            print(json.dumps(out, default=str), flush=True)
    finally:
        await rig.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
