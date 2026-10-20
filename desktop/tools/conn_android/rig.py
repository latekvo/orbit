"""Real orbitd + real Kotlin ResponderSession (android/companion/jvmphone) connectivity rig.

Both ends are real processes on real sockets. The phone is the JVM build of the companion's
protocol module wired like Orbit.kt/TcpServer.kt; build it first:

    cd android/companion && ./gradlew :jvmphone:installDist

Every timestamp in ``Rig.timeline`` is seconds since ``Rig.t0`` (time.monotonic()).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from orbit.client import ApiError, LocalClient

REPO = Path(__file__).resolve().parents[3]
JVMPHONE_DEFAULT = REPO / "android/companion/jvmphone/build/install/jvmphone/bin/jvmphone"
JVMPHONE = Path(os.environ.get("ORBIT_JVMPHONE", JVMPHONE_DEFAULT))
JAVA_HOME = os.environ.get("JAVA_HOME", "/Applications/Android Studio.app/Contents/jbr/Contents/Home")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proc:
    def __init__(self, argv: list[str], log: Path, env: dict[str, str] | None = None) -> None:
        self.log_path = log
        self._f = open(log, "ab")
        self.p = subprocess.Popen(argv, stdout=self._f, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})})

    def signal(self, sig: int) -> None:
        if self.p.poll() is None:
            self.p.send_signal(sig)

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGCONT)
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self._f.close()

    def text(self) -> str:
        return self.log_path.read_text(errors="replace")


class Rig:
    """One orbitd and one JVM phone, both in a private temp dir (never the user's config/socket)."""

    def __init__(self, base: Path | None = None, action_delay_ms: int = 0) -> None:
        self.base = Path(base or tempfile.mkdtemp(prefix="cad-", dir="/tmp"))
        self.base.mkdir(parents=True, exist_ok=True)
        self.sock = self.base / "d.sock"
        self.phone_port = free_port()
        self.phone_host = "127.0.0.1"
        self.action_delay_ms = action_delay_ms
        self.phone: Proc | None = None
        self.daemon: Proc | None = None
        self.client: LocalClient | None = None
        self.t0 = time.monotonic()
        self.timeline: list[tuple[float, str, Any]] = []
        self.devices: list[dict[str, Any]] = []
        self.activities: list[dict[str, Any]] = []
        self._watch: asyncio.Task | None = None
        self.device_id: str | None = None

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def mark(self, what: str, data: Any = None) -> None:
        self.timeline.append((self.now(), what, data))

    # ---- processes ----

    def start_phone(self) -> None:
        if not JVMPHONE.exists():
            raise RuntimeError(f"{JVMPHONE} missing: run ./gradlew :jvmphone:installDist in android/companion")
        self.phone = Proc(
            [str(JVMPHONE), str(self.base / "phone"), str(self.phone_port), str(self.action_delay_ms)],
            self.base / "phone.log",
            {"JAVA_HOME": JAVA_HOME},
        )
        self.mark("phone_start")

    async def wait_phone_listening(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                with socket.create_connection(("127.0.0.1", self.phone_port), 0.2):
                    pass
                # The probe itself opened a session; it ends at once (EOF during handshake).
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("phone not listening")

    async def start_daemon(self) -> None:
        self.sock.unlink(missing_ok=True)
        self.daemon = Proc(
            [
                str(Path(sys.executable).parent / "orbitd"), "--no-ble", "--no-mdns",
                "--config-dir", str(self.base / "cfg"), "--socket", str(self.sock), "--name", "RigLaptop",
            ],
            self.base / "orbitd.log",
        )
        deadline = time.monotonic() + 15
        while not self.sock.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("orbitd did not start")
            await asyncio.sleep(0.05)
        self.client = await LocalClient.connect(self.sock)
        await self.client.call("subscribe")
        self._watch = asyncio.create_task(self._watcher())

    async def _watcher(self) -> None:
        last_state = None
        last_stale = None
        assert self.client
        async for ev, data in self.client.events():
            if ev == "devices.changed":
                self.devices = data["devices"]
                for d in self.devices:
                    if d["id"] == self.device_id and d["state"] != last_state:
                        last_state = d["state"]
                        self.mark("state", d["state"])
            elif ev == "activities.changed":
                self.activities = data["activities"]
                summary = [(a["id"], a.get("title"), a.get("stale")) for a in self.activities]
                stale = tuple(summary)
                if stale != last_stale:
                    last_stale = stale
                    self.mark("activities", summary)

    async def pair(self) -> str:
        assert self.client
        res = await self.client.call("devices.connect", {"host": self.phone_host, "port": self.phone_port})
        self.device_id = res["id"]
        for _ in range(100):
            pairings = await self.client.call("pairing.list")
            if pairings:
                await self.client.call("pairing.respond", {"session": pairings[0]["session"], "accept": True})
                break
            await asyncio.sleep(0.05)
        await self.wait_state("connected", 10)
        await self.wait_activity_fresh(10)
        return self.device_id

    def state(self) -> str | None:
        for d in self.devices:
            if d["id"] == self.device_id:
                return d["state"]
        return None

    async def wait_state(self, state: str, timeout: float) -> float:
        """Seconds (rig clock) at which ``state`` was reached; TimeoutError otherwise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state() == state:
                return self.now()
            await asyncio.sleep(0.02)
        raise TimeoutError(f"state {state!r} not reached within {timeout} s (is {self.state()!r})")

    async def wait_activity_fresh(self, timeout: float) -> float:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(a["id"] == "counter" and not a.get("stale") for a in self.activities):
                return self.now()
            await asyncio.sleep(0.02)
        raise TimeoutError("no fresh counter activity")

    async def action(self) -> tuple[float, dict[str, Any] | str]:
        """Run one counter increment; returns (latency s, result or 'code: message')."""
        assert self.client
        t = time.monotonic()
        try:
            r = await self.client.call(
                "activities.action", {"device": self.device_id, "activity": "counter", "action": "notif.0"}
            )
        except ApiError as e:
            r = f"{e.code}: {e.message}"
        return round(time.monotonic() - t, 2), r

    def execs(self) -> list[str]:
        return [ln for ln in self.phone_text().splitlines() if " EXEC " in ln]

    def phone_text(self) -> str:
        return (self.base / "phone.log").read_text(errors="replace") if (self.base / "phone.log").exists() else ""

    def daemon_text(self) -> str:
        return self.daemon.text() if self.daemon else ""

    def phone_threads(self) -> int | None:
        """Live JVM thread count of the phone (jcmd), or None."""
        if not self.phone or self.phone.p.poll() is not None:
            return None
        jcmd = Path(JAVA_HOME) / "bin/jcmd"
        cmd = [str(jcmd), str(self.phone.p.pid), "Thread.print"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return out.stdout.count('" #') or out.stdout.count('"\n') or None

    def phone_thread_names(self) -> list[str]:
        if not self.phone or self.phone.p.poll() is not None:
            return []
        jcmd = Path(JAVA_HOME) / "bin/jcmd"
        cmd = [str(jcmd), str(self.phone.p.pid), "Thread.print"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return [ln.split('"')[1] for ln in out.stdout.splitlines() if ln.startswith('"')]

    def phone_fds(self) -> int | None:
        if not self.phone or self.phone.p.poll() is not None:
            return None
        out = subprocess.run(["lsof", "-p", str(self.phone.p.pid)], capture_output=True, text=True)
        return sum(1 for ln in out.stdout.splitlines() if "TCP" in ln)

    async def close_client_only(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.close()
        self.client = None

    async def close(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.close()
        for p in (self.daemon, self.phone):
            if p:
                p.stop()

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)
