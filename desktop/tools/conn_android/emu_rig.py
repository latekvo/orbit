"""Real orbitd against the real companion app on an Android emulator, over ``adb forward`` or the emulator NAT.

    ORBIT_EMU_SERIAL=emulator-5554 [ORBIT_EMU_ROUTE=redir] uv run python tools/conn_android/emu_scenarios.py <name> ...

Prerequisites (once): companion and sample producer installed, notification access, POST_NOTIFICATIONS
and Bluetooth granted, the companion started, sample producers running
(``am start -n dev.orbit.sample/.MainActivity --es start media,download,timer,reply``).
The first run pairs (laptop side via the API, phone side by tapping "Accept" in the companion's
dialog); the pairing lives in ``$ORBIT_EMU_DIR/cfg`` (default /tmp/conn-emu) and is reused.

The action under test is the sample timer's "+1 min" button: every execution is one
``action=timer.add_minute ... source=broadcast`` line under logcat tag OrbitSample.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from rig import Proc, Rig

from orbit.client import ApiError, LocalClient

SERIAL = os.environ.get("ORBIT_EMU_SERIAL", "emulator-5554")
BASE = Path(os.environ.get("ORBIT_EMU_DIR", "/tmp/conn-emu"))
# "forward": adb forward, carried by the adb channel straight to the guest's loopback (no guest
# network involved). "redir": the emulator's own NAT (`adb emu redir`), arriving on the guest's
# eth0 from 10.0.2.2 like LAN traffic, so the guest's network state and firewall (doze) apply.
ROUTE = os.environ.get("ORBIT_EMU_ROUTE", "forward")
FORWARD_PORT, REDIR_PORT = 47901, 47902
HOST_PORT = REDIR_PORT if ROUTE == "redir" else FORWARD_PORT
PKG = "dev.orbit.companion"
SAMPLE = "dev.orbit.sample"
LISTENER = f"{PKG}/{PKG}.OrbitListenerService"


def adb(*args: str, timeout: float = 60, check: bool = False) -> str:
    out = subprocess.run(["adb", "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout)
    if check and out.returncode:
        raise RuntimeError(f"adb {' '.join(args)}: {out.stderr or out.stdout}")
    return out.stdout + out.stderr


def sh(cmd: str, timeout: float = 60) -> str:
    return adb("shell", cmd, timeout=timeout).strip()


def su(cmd: str, timeout: float = 60) -> str:
    return sh(f"su 0 sh -c '{cmd}'", timeout)


def device_epoch() -> float:
    return float(sh("date +%s.%N"))


def pid() -> str:
    return sh(f"pidof {PKG}")


def events_log() -> list[str]:
    return sh(f"run-as {PKG} cat files/events.log").splitlines()


def exec_lines(since_epoch: float) -> list[str]:
    """OrbitSample add_minute executions logged after ``since_epoch`` (device clock)."""
    out = adb("logcat", "-d", "-v", "epoch", "-s", "OrbitSample:I")
    lines = []
    for ln in out.splitlines():
        m = re.match(r"\s*(\d+\.\d+)\s", ln)
        if m and float(m.group(1)) > since_epoch and "action=timer.add_minute" in ln and "source=broadcast" in ln:
            lines.append(ln.strip())
    return lines


def forward() -> None:
    """Opens this route's host port and closes the other's, so orbitd can only use this route."""
    adb("emu", "redir", "del", f"tcp:{REDIR_PORT}")
    adb("forward", "--remove", f"tcp:{FORWARD_PORT}")
    if ROUTE == "redir":
        adb("emu", "redir", "add", f"tcp:{REDIR_PORT}:47820", check=True)
    else:
        adb("forward", f"tcp:{FORWARD_PORT}", "tcp:47820", check=True)


def unforward() -> None:
    adb("emu", "redir", "del", f"tcp:{REDIR_PORT}")
    adb("forward", "--remove", f"tcp:{FORWARD_PORT}")


def wait_boot(timeout: float = 240) -> float:
    t = time.monotonic()
    adb("wait-for-device", timeout=timeout)
    while time.monotonic() - t < timeout:
        if sh("getprop sys.boot_completed") == "1":
            return time.monotonic() - t
        time.sleep(1)
    raise TimeoutError("emulator did not boot")


def ui_tap_text(text: str, timeout: float = 15) -> bool:
    """Taps the first on-screen node whose text is ``text`` (uiautomator dump)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sh("uiautomator dump /sdcard/ui.xml")
        xml = sh("cat /sdcard/ui.xml")
        for m in re.finditer(r'<node [^>]*?text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            if m.group(1) == text:
                x1, y1, x2, y2 = map(int, m.groups()[1:])
                sh(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
                return True
        time.sleep(0.5)
    return False


class EmuRig(Rig):
    def __init__(self) -> None:
        super().__init__(BASE)
        self.sock = BASE / "o.sock"
        self.phone_port = HOST_PORT
        self.id_file = BASE / "device_id"
        self.device_id = self.id_file.read_text().strip() if self.id_file.exists() else None
        self.epoch0 = device_epoch()
        self.events0 = len(events_log())
        self.dlog0 = self._dlog_size()

    def reset_clock(self) -> None:
        self.t0 = time.monotonic()
        self.timeline.clear()
        self.epoch0 = device_epoch()
        self.events0 = len(events_log())
        self.dlog0 = self._dlog_size()

    def _dlog_size(self) -> int:
        p = BASE / "orbitd.log"
        return p.stat().st_size if p.exists() else 0

    async def start_daemon(self) -> None:
        self.sock.unlink(missing_ok=True)
        self.daemon = Proc(
            [
                str(Path(sys.executable).parent / "orbitd"),
                "-v",
                "--no-ble",
                "--no-mdns",
                "--config-dir",
                str(BASE / "cfg"),
                "--socket",
                str(self.sock),
                "--name",
                "EmuRigLaptop",
            ],
            BASE / "orbitd.log",
        )
        deadline = time.monotonic() + 15
        while not self.sock.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("orbitd did not start")
            await asyncio.sleep(0.05)
        self.client = await LocalClient.connect(self.sock)
        await self.client.call("subscribe")
        self._watch = asyncio.create_task(self._watcher())

    async def pair(self) -> str:
        assert self.client
        res = await self.client.call("devices.connect", {"host": "127.0.0.1", "port": HOST_PORT})
        self.device_id = res["id"]
        for _ in range(200):
            pairings = await self.client.call("pairing.list")
            if pairings:
                await self.client.call("pairing.respond", {"session": pairings[0]["session"], "accept": True})
                break
            await asyncio.sleep(0.05)
        await asyncio.to_thread(ui_tap_text, "Accept")
        await self.wait_state("connected", 20)
        self.id_file.write_text(self.device_id)
        return self.device_id

    async def ensure_connected(self, timeout: float = 30) -> None:
        if not self.device_id:
            await self.pair()
        else:
            # Makes this route's address the device's manual address.
            await self.client.call("devices.connect", {"host": "127.0.0.1", "port": HOST_PORT})
        devices = await self.client.call("devices.list")
        address = next((d["address"] for d in devices if d["id"] == self.device_id), None)
        if address != f"127.0.0.1:{HOST_PORT}":
            raise RuntimeError(f"session runs over {address}, not the {ROUTE} route")
        await self.wait_state("connected", timeout)
        await self.wait_activity_fresh(timeout)

    def timer(self) -> dict[str, Any] | None:
        for a in self.activities:
            if a.get("app", {}).get("id") == SAMPLE and a.get("kind") == "timer":
                return a
        return None

    async def wait_activity_fresh(self, timeout: float) -> float:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.timer()
            if t and not t.get("stale"):
                return self.now()
            await asyncio.sleep(0.05)
        raise TimeoutError("no fresh timer activity")

    def mine(self) -> list[dict[str, Any]]:
        return [a for a in self.activities if a.get("device") == self.device_id]

    async def action(self, activity: str | None = None, action: str | None = None) -> tuple[float, Any]:
        """Presses the timer's "+1 min" (or the given action); returns (latency s, result or 'code: message')."""
        assert self.client
        t = self.timer()
        if activity is None:
            if t is None:
                return 0.0, "no timer activity listed"
            activity = t["id"]
            action = next(a["id"] for a in t["actions"] if a.get("label") == "+1 min")
        t0 = time.monotonic()
        try:
            r = await self.client.call(
                "activities.action", {"device": self.device_id, "activity": activity, "action": action}
            )
        except ApiError as e:
            r = f"{e.code}: {e.message}"
        return round(time.monotonic() - t0, 2), r

    def execs(self) -> list[str]:
        return exec_lines(self.epoch0)

    def new_events(self) -> list[str]:
        return [ln.split("] ", 1)[-1] for ln in events_log()[self.events0 :]]

    def daemon_lines(self, *needles: str) -> list[str]:
        with open(BASE / "orbitd.log", "rb") as f:
            f.seek(self.dlog0)
            lines = f.read().decode(errors="replace").splitlines()
        return [ln for ln in lines if any(n in ln for n in needles)]

    async def close(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.close()
        if self.daemon:
            self.daemon.stop()
