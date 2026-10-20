"""Real-process connectivity harness: the Kotlin ResponderSession on the JVM, a fault-injecting TCP
proxy, and a real ``orbitd`` process talking to it.

    laptop (orbitd) --TCP--> FaultProxy --TCP--> JvmPhone (dev.orbit.harness.PhoneHarnessKt)

Build the JVM side first (writes android/companion/protocol/build/harness.classpath):

    cd android/companion && ./gradlew :protocol:harnessClasspath
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import signal
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orbit.client import ApiError, LocalClient

REPO = Path(__file__).resolve().parents[3]
CLASSPATH_FILE = Path(
    os.environ.get("ORBIT_HARNESS_CLASSPATH", REPO / "android/companion/protocol/build/harness.classpath")
)
JAVA_HOME = os.environ.get("JAVA_HOME", "/Applications/Android Studio.app/Contents/jbr/Contents/Home")
JAVA = str(Path(JAVA_HOME) / "bin/java") if Path(JAVA_HOME, "bin/java").exists() else "java"
TUNED_ORBITD = Path(__file__).with_name("orbitd_tuned.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def log(*parts: Any) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


class Proc:
    """A child process whose output lines are timestamped (monotonic) and kept."""

    def __init__(self, name: str, argv: list[str], logfile: Path, env: dict[str, str] | None = None) -> None:
        self.name, self.argv, self.logfile, self.env = name, argv, logfile, env
        self.lines: list[tuple[float, str]] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._new = asyncio.Event()

    async def start(self, stdin: bool = False) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(self.env or {})},
            limit=16 * 1024 * 1024,
        )
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.logfile.open("a") as f:
            while line := await self.proc.stdout.readline():
                text = line.decode(errors="replace").rstrip()
                self.lines.append((time.monotonic(), text))
                f.write(text + "\n")
                f.flush()
                self._new.set()

    async def wait_line(self, pred: Callable[[str], bool], timeout: float, since: float = 0.0) -> tuple[float, str]:
        deadline = time.monotonic() + timeout
        while True:
            for ts, text in self.lines:
                if ts >= since and pred(text):
                    return ts, text
            self._new.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{self.name}: no matching line within {timeout} s")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), left)

    def signal(self, sig: int) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(sig)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.send_signal(signal.SIGCONT)
                self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._pump:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pump, 2)


class JvmPhone(Proc):
    """The Kotlin responder harness. Events are JSON lines; commands go to stdin."""

    def __init__(self, workdir: Path, name: str = "phone", port: int = 0, **opts: Any) -> None:
        self.port = port or free_port()
        self.state = workdir / f"{name}-state"
        args = [JAVA, "-Xmx256m", "-cp", CLASSPATH_FILE.read_text().strip(), "dev.orbit.harness.PhoneHarnessKt"]
        args += ["--state", str(self.state), "--port", str(self.port), "--name", "JVM Phone"]
        for k, v in opts.items():
            args += [f"--{k.replace('_', '-')}", str(v).lower() if isinstance(v, bool) else str(v)]
        super().__init__(name, args, workdir / f"{name}.log")
        self.device_id = ""
        self.pid = 0

    async def start(self, stdin: bool = True) -> None:
        await super().start(stdin=True)
        ev = await self.wait_event("listening", 30)
        self.device_id, self.pid = ev["deviceId"], ev["pid"]

    def events(self, ev: str | None = None, since: float = 0.0) -> list[tuple[float, dict[str, Any]]]:
        out = []
        for ts, text in self.lines:
            if ts < since or not text.startswith("{"):
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if ev is None or obj.get("ev") == ev:
                out.append((ts, obj))
        return out

    async def wait_event(
        self, ev: str, timeout: float = 10.0, since: float = 0.0, pred: Callable[[dict], bool] | None = None
    ) -> dict[str, Any]:
        def match(text: str) -> bool:
            if not text.startswith("{"):
                return False
            try:
                obj = json.loads(text)
            except ValueError:
                return False
            return obj.get("ev") == ev and (pred is None or pred(obj))

        _, text = await self.wait_line(match, timeout, since)
        return json.loads(text)

    async def cmd(self, cmd: str, **fields: Any) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps({"cmd": cmd, **fields}) + "\n").encode())
        await self.proc.stdin.drain()

    async def stats(self) -> dict[str, Any]:
        since = time.monotonic()
        await self.cmd("stats")
        return await self.wait_event("stats", 15, since)

    async def jcmd_threads(self) -> int:
        p = await asyncio.create_subprocess_exec(
            str(Path(JAVA).parent / "jcmd"),
            str(self.pid),
            "Thread.print",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        return out.decode().count('" #')

    async def live_instances(self, *classes: str) -> dict[str, int]:
        """Live instance counts from ``jcmd GC.class_histogram`` (forces a full GC)."""
        p = await asyncio.create_subprocess_exec(
            str(Path(JAVA).parent / "jcmd"),
            str(self.pid),
            "GC.class_histogram",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        counts = {c: 0 for c in classes}
        for line in out.decode().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] in counts:
                counts[parts[3]] = int(parts[1])
        return counts

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            with contextlib.suppress(Exception):
                self.proc.send_signal(signal.SIGCONT)
                await self.cmd("exit")
                await asyncio.wait_for(self.proc.wait(), 5)
        await super().stop()


class Direction:
    """Fault settings for one direction of the proxy; changes apply to live connections."""

    def __init__(self) -> None:
        self.mode = "pass"  # pass | pause (stop reading: bytes stay in kernel buffers) | drop (read, discard)
        self.chunk = 65536  # max bytes per write (1 = byte-by-byte fragmentation)
        self.chunk_delay = 0.0
        self.latency = 0.0
        self.jitter = 0.0
        self.rate = 0.0  # bytes/s, 0 = unlimited
        self.resumed = asyncio.Event()
        self.resumed.set()
        self.bytes = 0

    def set(self, **kw: Any) -> None:
        for k, v in kw.items():
            if not hasattr(self, k):
                raise AttributeError(k)
            setattr(self, k, v)
        if self.mode == "pause":
            self.resumed.clear()
        else:
            self.resumed.set()


class FaultProxy:
    """TCP proxy between laptop (clients) and the phone (upstream) with injectable faults."""

    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1") -> None:
        self.upstream = (upstream_host, upstream_port)
        self.port = 0
        self.up = Direction()  # laptop -> phone
        self.down = Direction()  # phone -> laptop
        self.accepting = "pass"  # pass | refuse (accept + RST) | hang (accept, never connect upstream)
        self.conns: list[tuple[socket.socket, socket.socket, list[asyncio.Task]]] = []
        self.total = 0
        self._server: asyncio.AbstractServer | None = None
        self._lsock: socket.socket | None = None

    async def start(self, port: int = 0) -> None:
        loop = asyncio.get_running_loop()
        self._lsock = socket.socket()
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", port))
        self._lsock.listen(128)
        self._lsock.setblocking(False)
        self.port = self._lsock.getsockname()[1]
        self._accept_task = loop.create_task(self._accept_loop())

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._lsock
        while True:
            client, _ = await loop.sock_accept(self._lsock)
            client.setblocking(False)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.total += 1
            if self.accepting == "refuse":
                rst(client)
                continue
            if self.accepting == "hang":
                self.conns.append((client, client, []))
                continue
            loop.create_task(self._connect(client))

    async def _connect(self, client: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        server = socket.socket()
        server.setblocking(False)
        try:
            await loop.sock_connect(server, self.upstream)
        except OSError:
            rst(client)
            server.close()
            return
        server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tasks: list[asyncio.Task] = []
        entry = (client, server, tasks)
        self.conns.append(entry)
        tasks.append(loop.create_task(self._pump(client, server, self.up, entry)))
        tasks.append(loop.create_task(self._pump(server, client, self.down, entry)))

    async def _pump(self, src: socket.socket, dst: socket.socket, d: Direction, entry: tuple) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue()
        writer = loop.create_task(self._writer(dst, queue, d))
        entry[2].append(writer)
        last_due = 0.0
        try:
            while True:
                await d.resumed.wait()
                try:
                    data = await loop.sock_recv(src, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                await d.resumed.wait()  # a read already in progress when pausing is held here, like bytes in flight
                if d.mode == "drop":
                    continue
                for i in range(0, len(data), max(1, d.chunk)):
                    due = time.monotonic() + d.latency + random.uniform(0, d.jitter)
                    last_due = max(last_due, due)  # TCP stays ordered
                    await queue.put((last_due, data[i : i + max(1, d.chunk)]))
        finally:
            await queue.put(None)

    async def _writer(self, dst: socket.socket, queue: asyncio.Queue, d: Direction) -> None:
        loop = asyncio.get_running_loop()
        while (item := await queue.get()) is not None:
            due, chunk = item
            if (wait := due - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            try:
                await loop.sock_sendall(dst, chunk)
            except OSError:
                return
            d.bytes += len(chunk)
            if d.rate:
                await asyncio.sleep(len(chunk) / d.rate)
            if d.chunk_delay:
                await asyncio.sleep(d.chunk_delay)
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)  # forward the FIN

    def live(self) -> int:
        return sum(1 for c, s, _ in self.conns if c.fileno() != -1)

    def rst_all(self) -> int:
        """Reset both sides of every connection (like a NAT/AP dropping the flow with RSTs)."""
        n = 0
        for c, s, tasks in self.conns:
            for t in tasks:
                t.cancel()
            if c.fileno() != -1:
                n += 1
            rst(c)
            rst(s)
        self.conns.clear()
        return n

    def close_all(self) -> None:
        for c, s, tasks in self.conns:
            for t in tasks:
                t.cancel()
            c.close()
            s.close()
        self.conns.clear()

    def rst_laptop_side(self) -> None:
        """Reset only the laptop's end; the phone's end is abandoned without FIN/RST (the laptop
        roamed to another network: its old flow just stops arriving at the phone)."""
        abandoned = getattr(self, "_abandoned", [])
        for c, s, tasks in self.conns:
            for t in tasks:
                t.cancel()
            rst(c)
            abandoned.append((s, s, []))
        self._abandoned = abandoned
        self.conns = []

    def forget_all(self) -> None:
        """Silently abandon every connection (no FIN/RST ever reaches either side): a true blackhole
        of existing flows. The sockets are kept open (never read) until stop()."""
        for _, _, tasks in self.conns:
            for t in tasks:
                t.cancel()
        self._abandoned = getattr(self, "_abandoned", []) + self.conns
        self.conns = []

    async def stop(self) -> None:
        self._accept_task.cancel()
        self.close_all()
        for c, s, _ in getattr(self, "_abandoned", []):
            c.close()
            s.close()
        if self._lsock:
            self._lsock.close()


def rst(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(OSError):
        sock.close()


class Orbitd(Proc):
    """A real orbitd process (mDNS and BLE off) with isolated config dir and socket."""

    def __init__(self, workdir: Path, name: str = "laptop", **daemon_kwargs: Any) -> None:
        self.config = workdir / f"{name}-config"
        self.sock = Path("/tmp") / f"cj-{os.getpid()}-{name}.sock"
        if daemon_kwargs:
            argv = [sys.executable, str(TUNED_ORBITD), json.dumps(daemon_kwargs)]
        else:
            argv = [str(Path(sys.executable).parent / "orbitd")]
        argv += [
            "--config-dir",
            str(self.config),
            "--socket",
            str(self.sock),
            "--no-mdns",
            "--no-ble",
            "--name",
            "JVM Test Laptop",
        ]
        super().__init__(name, argv, workdir / f"{name}.log")
        self.client: LocalClient | None = None
        self.events: list[tuple[float, str, Any]] = []
        self._ev_task: asyncio.Task | None = None
        self._ev_new = asyncio.Event()

    async def start(self, stdin: bool = False) -> None:
        await super().start()
        await self.wait_line(lambda t: "local API at" in t, 30)
        self.client = await LocalClient.connect(self.sock)
        await self.client.call("subscribe")
        self._ev_task = asyncio.create_task(self._events())

    async def _events(self) -> None:
        assert self.client
        async for ev, data in self.client.events():
            self.events.append((time.monotonic(), ev, data))
            self._ev_new.set()

    async def call(self, method: str, params: dict | None = None) -> Any:
        assert self.client
        return await self.client.call(method, params or {})

    async def timed_call(self, method: str, params: dict | None = None) -> tuple[float, Any]:
        """(seconds, result or ApiError)."""
        t0 = time.monotonic()
        try:
            r = await self.call(method, params)
        except ApiError as e:
            r = e
        return time.monotonic() - t0, r

    def device(self, dev_id: str) -> dict | None:
        for _, ev, data in reversed(self.events):
            if ev == "devices.changed":
                return next((d for d in data["devices"] if d["id"] == dev_id), None)
        return None

    def state_log(self, dev_id: str, since: float = 0.0) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        for ts, ev, data in self.events:
            if ev == "devices.changed" and ts >= since:
                d = next((d for d in data["devices"] if d["id"] == dev_id), None)
                st = d["state"] if d else "absent"
                if not out or out[-1][1] != st:
                    out.append((ts, st))
        return out

    def activities(self) -> list[dict]:
        for _, ev, data in reversed(self.events):
            if ev == "activities.changed":
                return data["activities"]
        return []

    async def wait_event(self, ev: str, pred: Callable[[Any], bool], timeout: float, since: float = 0.0) -> float:
        deadline = time.monotonic() + timeout
        while True:
            for ts, e, data in self.events:
                if ts >= since and e == ev and pred(data):
                    return ts
            self._ev_new.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {ev} matching within {timeout} s")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ev_new.wait(), left)

    async def wait_state(self, dev_id: str, state: str, timeout: float, since: float = 0.0) -> float:
        def pred(data: Any) -> bool:
            d = next((d for d in data["devices"] if d["id"] == dev_id), None)
            return (d["state"] if d else "absent") == state

        return await self.wait_event("devices.changed", pred, timeout, since)

    async def wait_activities(self, pred: Callable[[list], bool], timeout: float, since: float = 0.0) -> float:
        return await self.wait_event("activities.changed", lambda d: pred(d["activities"]), timeout, since)

    async def stop(self) -> None:
        if self.client:
            await self.client.close()
        if self._ev_task:
            self._ev_task.cancel()
        await super().stop()


MEDIA = {
    "id": "media:com.example",
    "kind": "media",
    "app": {"id": "com.example", "name": "Example"},
    "title": "Song",
    "media": {"state": "playing", "positionMs": 0, "durationMs": 200000, "rate": 1.0, "seekable": True},
    "actions": [{"id": "media.pause", "label": "Pause"}, {"id": "notif.0", "label": "Like"}],
}


async def pair(laptop: Orbitd, phone: JvmPhone, port: int) -> str:
    """Pair over 127.0.0.1:port (phone auto-accepts); returns the phone's device id once connected."""
    await phone.cmd("auto_accept", value=True)
    t0 = time.monotonic()
    res = await laptop.call("devices.connect", {"host": "127.0.0.1", "port": port})
    dev_id = res["id"]
    await laptop.wait_event("pairing.changed", lambda d: bool(d["pairings"]), 10, t0)
    pairing = next(
        p for _, e, d in reversed(laptop.events) if e == "pairing.changed" and d["pairings"] for p in d["pairings"]
    )
    await laptop.call("pairing.respond", {"session": pairing["session"], "accept": True})
    await phone.wait_event("session_state", 10, t0, lambda e: e["state"] == "PAIRED")
    await laptop.wait_event(
        "devices.changed",
        lambda d: any(x["id"] == dev_id and x["paired"] and x["connected"] for x in d["devices"]),
        10,
        t0,
    )
    return dev_id
