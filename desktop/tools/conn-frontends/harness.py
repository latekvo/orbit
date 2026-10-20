"""Connectivity harness: real orbitd + orbit-fakephone processes, a fault-injecting TCP proxy
between them, and an event-recording local-API client.

Everything lives under /tmp/conn-frontends/<run>/ (short AF_UNIX paths); the user's own orbitd
socket and config dir are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from orbit.client import LocalClient

BIN = Path(sys.executable).parent
ROOT = Path("/tmp/conn-frontends")


def bin_path(name: str) -> str:
    return str(BIN / name)


class Proc:
    def __init__(self, name: str, argv: list[str], log: Path, env: dict[str, str] | None = None) -> None:
        self.name, self.argv, self.log_path, self.env = name, argv, log, env
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> Proc:
        self._log = open(self.log_path, "ab")  # noqa: SIM115
        env = {**os.environ, **(self.env or {}), "PYTHONUNBUFFERED": "1"}
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdout=self._log, stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True
        )
        return self

    @property
    def pid(self) -> int:
        assert self.proc
        return self.proc.pid

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    async def wait_log(self, pattern: str, timeout: float = 10.0) -> re.Match:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if m := re.search(pattern, self.log()):
                return m
            await asyncio.sleep(0.05)
        raise AssertionError(f"{self.name}: no {pattern!r} in log within {timeout} s:\n{self.log()[-3000:]}")

    def send_signal(self, sig: int) -> None:
        if self.proc and self.proc.returncode is None:
            os.kill(self.proc.pid, sig)

    async def stop(self, sig: int = signal.SIGTERM, timeout: float = 8.0) -> int | None:
        if not self.proc:
            return None
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.proc.pid, signal.SIGCONT)
                os.kill(self.proc.pid, sig)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
        self._log.close()
        return self.proc.returncode

    def rss_kb(self) -> int:
        out = os.popen(f"ps -o rss= -p {self.pid}").read().strip()
        return int(out or 0)

    def fds(self) -> int:
        out = os.popen(f"lsof -p {self.pid} 2>/dev/null | wc -l").read().strip()
        return int(out or 0)


class FaultProxy:
    """TCP proxy 127.0.0.1:<port> -> target. Faults:

    - ``blackhole()``: stop forwarding in both directions (connections stay open, bytes queue
      up like on a link that went silent); ``heal()`` releases them.
    - ``reset()``: abort every open connection.
    - ``refuse()``: stop listening (new connects get ECONNREFUSED); ``listen()`` again.
    """

    def __init__(self, target_port: int, target_host: str = "127.0.0.1") -> None:
        self.target = (target_host, target_port)
        self.port = 0
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.pairs: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.accepted = 0

    async def listen(self) -> int:
        self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def refuse(self) -> None:
        if self._server:
            self._server.close()
            self._server = None

    def blackhole(self) -> None:
        self.flowing.clear()

    def heal(self) -> None:
        self.flowing.set()

    def reset(self) -> None:
        for a, b in self.pairs:
            for w in (a, b):
                with contextlib.suppress(Exception):
                    w.transport.abort()
        self.pairs.clear()

    async def close(self) -> None:
        await self.refuse()
        self.reset()
        for t in list(self._tasks):
            t.cancel()

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        try:
            ur, uw = await asyncio.open_connection(*self.target)
        except OSError:
            writer.transport.abort()
            return
        self.pairs.append((writer, uw))
        for src, dst in ((reader, uw), (ur, writer)):
            t = asyncio.create_task(self._pump(src, dst))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _pump(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self.flowing.wait()
                data = await src.read(65536)
                if not data:
                    break
                await self.flowing.wait()
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.close()


class Recorder:
    """Subscribed local-API client that timestamps every event (seconds since ``t0``)."""

    def __init__(self, client: LocalClient, t0: float) -> None:
        self.client, self.t0 = client, t0
        self.events: list[tuple[float, str, Any]] = []
        self._task = asyncio.create_task(self._run())

    @classmethod
    async def connect(cls, sock: Path, t0: float) -> Recorder:
        c = await LocalClient.connect(sock)
        await c.call("subscribe")
        return cls(c, t0)

    async def _run(self) -> None:
        async for ev, data in self.client.events():
            self.events.append((time.monotonic() - self.t0, ev, data))

    def states(self, dev_id: str) -> list[tuple[float, str]]:
        """Distinct consecutive device states with the time each was first seen."""
        out: list[tuple[float, str]] = []
        for t, ev, data in self.events:
            if ev != "devices.changed":
                continue
            d = next((x for x in data["devices"] if x["id"] == dev_id), None)
            st = d["state"] if d else "absent"
            if not out or out[-1][1] != st:
                out.append((round(t, 2), st))
        return out

    def last(self, ev: str) -> Any:
        return next((d for _, e, d in reversed(self.events) if e == ev), None)

    async def until(self, pred, timeout: float = 10.0) -> float:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self):
                return time.monotonic() - self.t0
            await asyncio.sleep(0.02)
        raise AssertionError(f"condition not met within {timeout} s; states={self.events[-3:]}")

    async def close(self) -> None:
        await self.client.close()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class World:
    """One isolated orbitd + one fake phone behind a FaultProxy, paired over the proxy address."""

    def __init__(self, tag: str = "w") -> None:
        ROOT.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=ROOT))
        self.sock = self.dir / "d.sock"
        self.daemon: Proc | None = None
        self.phone: Proc | None = None
        self.proxy: FaultProxy | None = None
        self.phone_id = ""
        self.recorders: list[Recorder] = []
        self.applets: list[Applet] = []

    async def applet(self, name: str = "applet", theme: str = "light") -> Applet:
        a = await Applet(self.sock, self.dir / name, theme).start()
        self.applets.append(a)
        return a

    async def start_phone(self, extra: list[str] | None = None) -> None:
        self.phone = await Proc(
            "fakephone",
            [bin_path("orbit-fakephone"), "--no-mdns", "--port", "0", "--auto-accept", "--host", "127.0.0.1",
             "--config-dir", str(self.dir / "phone"), "--name", "Test Phone", *(extra or [])],
            self.dir / "phone.log",
        ).start()
        m = await self.phone.wait_log(r"listening on 127\.0\.0\.1:(\d+) .* device id ([0-9a-f]{32})")
        self.phone_port, self.phone_id = int(m.group(1)), m.group(2)
        self.proxy = FaultProxy(self.phone_port)
        await self.proxy.listen()

    async def start_daemon(self, extra: list[str] | None = None) -> None:
        self.daemon = await Proc(
            "orbitd",
            [bin_path("orbitd"), "--no-mdns", "--no-ble", "--name", "Test Mac", "--config-dir",
             str(self.dir / "cfg"), "--socket", str(self.sock), *(extra or [])],
            self.dir / "orbitd.log",
        ).start()
        deadline = time.monotonic() + 10
        while not self.sock.exists():
            if time.monotonic() > deadline:
                raise AssertionError(self.daemon.log())
            await asyncio.sleep(0.05)

    async def restart_daemon(self, sig: int = signal.SIGTERM) -> None:
        assert self.daemon
        await self.daemon.stop(sig)
        await self.start_daemon()

    async def cli(self, *args: str, timeout: float = 60.0) -> tuple[int, str, str]:
        p = await asyncio.create_subprocess_exec(
            bin_path("orbit"), "--socket", str(self.sock), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except TimeoutError:
            p.kill()
            await p.wait()
            return -999, "", "TIMEOUT"
        return p.returncode, out.decode(), err.decode()

    async def pair(self) -> None:
        assert self.proxy
        code, out, err = await self.cli("connect", f"127.0.0.1:{self.proxy.port}")
        assert code == 0, (out, err)
        code, out, err = await self.cli("pair", "--accept", "--wait", "5", "--timeout", "15")
        assert code == 0, (out, err)
        await self.wait_connected()

    async def api(self, method: str, params: dict | None = None, timeout: float = 40.0) -> Any:
        c = await LocalClient.connect(self.sock)
        try:
            return await asyncio.wait_for(c.call(method, params), timeout)
        finally:
            await c.close()

    async def device(self) -> dict:
        devs = await self.api("devices.list")
        return next(d for d in devs if d["id"] == self.phone_id)

    async def wait_connected(self, timeout: float = 30.0) -> float:
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            with contextlib.suppress(Exception):
                if (await self.device())["state"] == "connected" and await self.api("activities.list"):
                    return time.monotonic() - t
            await asyncio.sleep(0.1)
        raise AssertionError("not connected in time:\n" + (self.daemon.log()[-3000:] if self.daemon else ""))

    async def recorder(self, t0: float | None = None) -> Recorder:
        r = await Recorder.connect(self.sock, t0 if t0 is not None else time.monotonic())
        self.recorders.append(r)
        return r

    def phone_actions(self, action: str) -> int:
        assert self.phone
        return len(re.findall(rf"action {re.escape(action)} -> (?!failed)", self.phone.log()))

    async def close(self) -> None:
        for r in self.recorders:
            with contextlib.suppress(Exception):
                await r.close()
        for a in self.applets:
            with contextlib.suppress(Exception):
                await a.stop()
        if self.proxy:
            await self.proxy.close()
        for p in (self.daemon, self.phone):
            if p:
                await p.stop()


class Applet:
    """applet_probe.py subprocess: the real applet UI, offscreen, recording what it shows."""

    def __init__(self, sock: Path, out: Path, theme: str = "light") -> None:
        self.sock, self.out, self.theme = sock, out, theme
        self.proc: Proc | None = None

    async def start(self) -> Applet:
        self.out.mkdir(parents=True, exist_ok=True)
        probe = Path(__file__).with_name("applet_probe.py")
        self.proc = await Proc(
            "applet", [sys.executable, str(probe), str(self.sock), str(self.out), "--theme", self.theme],
            self.out / "applet.log", env={"QT_QPA_PLATFORM": "offscreen"},
        ).start()
        self.t0 = time.monotonic()
        return self

    def cmd(self, line: str) -> None:
        with open(self.out / "cmd", "a") as f:
            f.write(line + "\n")

    async def shot(self, name: str) -> Path:
        path = self.out / f"{name}.png"
        path.unlink(missing_ok=True)
        self.cmd(f"shot {name}")
        for _ in range(100):
            if path.exists() and path.stat().st_size:
                await asyncio.sleep(0.1)
                return path
            await asyncio.sleep(0.05)
        raise AssertionError(f"no screenshot {name}")

    def states(self) -> list[dict]:
        p = self.out / "ui.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def now(self) -> dict:
        s = self.states()
        return s[-1] if s else {}

    def first(self, pred, after: float = 0.0) -> dict | None:
        """First recorded state (monotonic time ``m`` >= after) satisfying pred."""
        return next((s for s in self.states() if s["m"] >= after and pred(s)), None)

    async def until(self, pred, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur = self.now()
            if cur and pred(cur):
                return cur
            await asyncio.sleep(0.05)
        raise AssertionError(f"applet: condition not met within {timeout} s; last={dump(self.now())}")

    async def stop(self) -> None:
        if self.proc:
            self.cmd("quit")
            await self.proc.stop()


def media_id(activities: list[dict]) -> str:
    return next(a["id"] for a in activities if a.get("kind") == "media")


def measure(name: str, **kv: Any) -> None:
    line = "MEASURE " + name + " " + " ".join(f"{k}={v}" for k, v in kv.items())
    print(line, flush=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / "measures.log", "a") as f:
        f.write(time.strftime("%H:%M:%S ") + line + "\n")


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str)
