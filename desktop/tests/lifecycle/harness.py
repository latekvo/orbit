"""Process-level harness: real orbitd / orbit-fakephone processes, a fault-injecting TCP proxy, metrics.

Everything runs on 127.0.0.1 with --no-mdns/--no-ble, under a private /tmp directory, so the
user's own orbitd (default socket and config dir) is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from orbit.client import ApiError, LocalClient

HERE = Path(__file__).resolve().parent
PY = sys.executable
WRAP = HERE / "wrap.py"
ORBIT_CLI = str(Path(PY).parent / "orbit")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_root(tag: str) -> Path:
    base = Path("/tmp/conn-lifecycle")
    base.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=base))


class Proc:
    """A child process whose merged stdout/stderr is captured with monotonic timestamps."""

    def __init__(self, name: str, argv: list[str], log: Path, env: dict[str, str] | None = None) -> None:
        self.name, self.argv, self.log, self.env = name, argv, log, env or {}
        self.lines: list[tuple[float, str]] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._new = asyncio.Event()

    @property
    def pid(self) -> int:
        assert self.proc
        return self.proc.pid

    async def start(self) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.log.open("a") as f:
            while line := await self.proc.stdout.readline():
                text = line.decode(errors="replace").rstrip()
                self.lines.append((time.monotonic(), text))
                f.write(f"{time.monotonic():.3f} {text}\n")
                f.flush()
                self._new.set()

    def write(self, text: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(text.encode())

    def grep(self, pattern: str, since: float = 0.0) -> list[tuple[float, str]]:
        rx = re.compile(pattern)
        return [(t, s) for t, s in self.lines if t >= since and rx.search(s)]

    async def wait_line(self, pattern: str, timeout: float = 10.0, since: float = 0.0) -> tuple[float, str]:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            for t, s in self.lines:
                if t >= since and rx.search(s):
                    return t, s
            left = deadline - time.monotonic()
            if left <= 0:
                tail = "\n".join(s for _, s in self.lines[-15:])
                raise TimeoutError(f"{self.name}: no line /{pattern}/ within {timeout}s; tail:\n{tail}")
            self._new.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), min(left, 0.5))

    def events(self, kind: str | None = None, since: float = 0.0) -> list[dict[str, Any]]:
        out = []
        for t, s in self.lines:
            if t >= since and s.startswith("EVENT "):
                ev = json.loads(s[6:])
                ev["_t"] = t
                if kind is None or ev["kind"] == kind:
                    out.append(ev)
        return out

    async def stats(self, timeout: float = 5.0) -> dict[str, Any]:
        since = time.monotonic()
        os.kill(self.pid, signal.SIGUSR1)
        _, line = await self.wait_line(r"^STATS ", timeout, since=since)
        return json.loads(line[6:])

    def send_signal(self, sig: int) -> None:
        if self.proc and self.proc.returncode is None:
            os.kill(self.pid, sig)

    async def stop(self, sig: int = signal.SIGTERM, timeout: float = 10.0) -> int | None:
        if self.proc is None:
            return None
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGCONT)
                os.kill(self.pid, sig)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        if self._pump:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._pump, 2)
        return self.proc.returncode

    def rss_kb(self) -> int:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0)

    def os_threads(self) -> int:
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-M", "-p", str(self.pid)], capture_output=True, text=True).stdout
            return max(0, len(out.strip().splitlines()) - 1)
        return len(os.listdir(f"/proc/{self.pid}/task"))

    def open_fds(self) -> int:
        out = subprocess.run(["lsof", "-n", "-P", "-p", str(self.pid)], capture_output=True, text=True).stdout
        return max(0, len(out.strip().splitlines()) - 1)

    def tcp_fds(self) -> list[str]:
        out = subprocess.run(
            ["lsof", "-n", "-P", "-a", "-p", str(self.pid), "-i", "TCP"], capture_output=True, text=True
        ).stdout
        return out.strip().splitlines()[1:]


class Orbitd(Proc):
    def __init__(self, root: Path, name: str = "Lap", tag: str = "d", env: dict[str, str] | None = None) -> None:
        self.config = root / f"{tag}-cfg"
        self.socket = root / f"{tag}.sock"
        self.lapname = name
        super().__init__(
            f"orbitd[{tag}]",
            [
                PY,
                str(WRAP),
                "daemon",
                "--",
                "--name",
                name,
                "--config-dir",
                str(self.config),
                "--socket",
                str(self.socket),
                "--no-mdns",
                "--no-ble",
                "-v",
            ],
            root / f"{tag}.log",
            env,
        )

    async def start(self) -> None:
        await super().start()
        await self.wait_line(r"local API at", 15)

    async def api(self) -> Api:
        return await Api.connect(self.socket)


class Phone(Proc):
    def __init__(
        self,
        root: Path,
        name: str = "Fake Phone",
        tag: str = "p",
        port: int | None = None,
        accept: str | None = "accept",
        extra: list[str] | None = None,
        env: dict[str, str] | None = None,
        config: Path | None = None,
    ) -> None:
        self.config = config or root / f"{tag}-cfg"
        self.port = port or free_port()
        self.phonename = name
        args = [
            "--name",
            name,
            "--config-dir",
            str(self.config),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-mdns",
            "-v",
        ]
        if accept == "accept":
            args.append("--auto-accept")
        elif accept == "reject":
            args.append("--auto-reject")
        super().__init__(
            f"phone[{tag}]", [PY, str(WRAP), "phone", "--", *args, *(extra or [])], root / f"{tag}.log", env
        )

    async def start(self) -> None:
        await super().start()
        await self.wait_line(r"listening on", 15)

    def actions(self, since: float = 0.0) -> list[dict[str, Any]]:
        return self.events("action", since)

    @property
    def device_id(self) -> str:
        _, line = next((t, s) for t, s in self.lines if "device id" in s)
        return re.search(r"device id ([0-9a-f]{32})", line).group(1)


class Api:
    """LocalClient + a recorder of every subscribed event with its arrival time."""

    def __init__(self, client: LocalClient) -> None:
        self.c = client
        self.events: list[tuple[float, str, Any]] = []
        self._task: asyncio.Task | None = None
        self._new = asyncio.Event()

    @classmethod
    async def connect(cls, path: Path) -> Api:
        api = cls(await LocalClient.connect(path))
        await api.c.call("subscribe")
        api._task = asyncio.create_task(api._record())
        return api

    async def _record(self) -> None:
        async for event, data in self.c.events():
            self.events.append((time.monotonic(), event, data))
            self._new.set()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self.c.call(method, params)

    async def timed(self, method: str, params: dict[str, Any] | None = None) -> tuple[float, Any]:
        """(latency s, result or ApiError instance)."""
        t0 = time.monotonic()
        try:
            res: Any = await self.c.call(method, params)
        except ApiError as e:
            res = e
        return time.monotonic() - t0, res

    async def device(self, dev_id: str) -> dict[str, Any] | None:
        return next((d for d in await self.call("devices.list") if d["id"] == dev_id), None)

    def state_log(self, dev_id: str, since: float = 0.0) -> list[tuple[float, str]]:
        """Distinct consecutive device states seen in devices.changed events."""
        out: list[tuple[float, str]] = []
        for t, ev, data in self.events:
            if t < since or ev != "devices.changed":
                continue
            d = next((x for x in data["devices"] if x["id"] == dev_id), None)
            st = "absent" if d is None else d["state"] + ("" if d["paired"] else "/unpaired")
            if not out or out[-1][1] != st:
                out.append((t, st))
        return out

    def activity_log(self, dev_id: str, since: float = 0.0) -> list[tuple[float, int, bool]]:
        out: list[tuple[float, int, bool]] = []
        for t, ev, data in self.events:
            if t < since or ev != "activities.changed":
                continue
            acts = [a for a in data["activities"] if a["device"] == dev_id]
            entry = (len(acts), any(a.get("stale") for a in acts))
            if not out or out[-1][1:] != entry:
                out.append((t, *entry))
        return out

    async def wait_state(self, dev_id: str, state: str, timeout: float, since: float = 0.0) -> float:
        """Monotonic time at which the device was first seen in ``state`` (after ``since``)."""
        deadline = time.monotonic() + timeout
        while True:
            for t, st in self.state_log(dev_id, since):
                if st == state:
                    return t
            d = await self.device(dev_id)
            if d is not None and d["state"] == state and time.monotonic() >= since:
                return time.monotonic()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{dev_id[:8]} never {state!r}; log {self.state_log(dev_id, since)}")
            self._new.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), min(left, 0.25))

    async def wait_fresh_activities(self, dev_id: str, timeout: float, since: float = 0.0) -> float:
        deadline = time.monotonic() + timeout
        while True:
            acts = [a for a in await self.call("activities.list") if a["device"] == dev_id]
            if acts and not any(a.get("stale") for a in acts):
                return time.monotonic()
            if time.monotonic() > deadline:
                raise TimeoutError(f"no fresh activities for {dev_id[:8]}")
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        await self.c.close()
        if self._task:
            self._task.cancel()


async def pair(api: Api, phone: Phone, host: str = "127.0.0.1", port: int | None = None) -> str:
    """Pair through devices.connect + pairing.respond (phone must auto-accept). Returns the device id."""
    res = await api.call("devices.connect", {"host": host, "port": port or phone.port})
    dev = res["id"]
    for _ in range(200):
        pend = [p for p in await api.call("pairing.list") if p["deviceId"] == dev]
        if pend:
            await api.call("pairing.respond", {"session": pend[0]["session"], "accept": True})
            break
        await asyncio.sleep(0.02)
    for _ in range(300):
        d = await api.device(dev)
        if d and d["paired"] and d["connected"]:
            return dev
        await asyncio.sleep(0.02)
    raise AssertionError(f"pairing with {dev} did not complete")


# ---- fault-injecting TCP proxy ------------------------------------------------------------------


def _rst(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    with contextlib.suppress(OSError):
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


class Conn:
    def __init__(self, proxy: Proxy, cr, cw, ur, uw) -> None:  # type: ignore[no-untyped-def]
        self.proxy = proxy
        self.cr, self.cw, self.ur, self.uw = cr, cw, ur, uw
        self.up_frozen = False  # laptop -> phone bytes are discarded
        self.down_frozen = False  # phone -> laptop bytes are discarded
        self.opened = time.monotonic()
        self.up_bytes = self.down_bytes = 0
        self.closed = asyncio.Event()
        # After the next laptop->phone chunk is forwarded: freeze phone->laptop, then RST after a delay.
        self.lose_reply_after: float | None = None
        # The phone forgot this connection (e.g. while the laptop slept): the laptop's next byte draws an RST.
        self.rst_on_up = False
        self.tasks = [asyncio.create_task(self._pump(cr, uw, True)), asyncio.create_task(self._pump(ur, cw, False))]

    async def _pump(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, up: bool) -> None:
        try:
            while data := await r.read(65536):
                if up:
                    self.up_bytes += len(data)
                    if self.rst_on_up:
                        self.rst()
                        return
                    if self.up_frozen:
                        continue
                    w.write(data)
                    if self.lose_reply_after is not None:
                        delay, self.lose_reply_after = self.lose_reply_after, None
                        self.down_frozen = True
                        asyncio.get_running_loop().call_later(delay, self.rst)
                else:
                    self.down_bytes += len(data)
                    if self.down_frozen:
                        continue
                    w.write(data)
                with contextlib.suppress(ConnectionError):
                    await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if not self.up_frozen and not self.down_frozen:
                with contextlib.suppress(Exception):
                    w.close()
            self.closed.set()

    def freeze(self, up: bool = True, down: bool = True) -> None:
        """Black hole: keep both sockets open, drop the bytes (a half-open path, no FIN/RST)."""
        self.up_frozen |= up
        self.down_frozen |= down

    def rst(self) -> None:
        for w in (self.cw, self.uw):
            _rst(w)
        for t in self.tasks:
            t.cancel()
        self.closed.set()

    def rst_laptop_side_only(self) -> None:
        """RST toward the laptop; the phone side stays open but silent (phone sees a half-open link)."""
        _rst(self.cw)
        self.down_frozen = self.up_frozen = True


class Proxy:
    def __init__(self, target_port: int, target_host: str = "127.0.0.1", port: int = 0) -> None:
        self.target = (target_host, target_port)
        self.port = port
        self.conns: list[Conn] = []
        self.mode = "pass"  # pass | refuse | hang
        self.max_life: float | None = None
        self.connect_delay = 0.0  # hold each new connection this long before dialling the phone
        self.accepted = 0
        self._server: asyncio.Server | None = None
        self._hung: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> Proxy:
        self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            self._server = None
        for c in self.conns:
            c.rst()
        for w in self._hung:
            _rst(w)

    @property
    def live(self) -> list[Conn]:
        return [c for c in self.conns if not c.closed.is_set()]

    async def _on_conn(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        self.accepted += 1
        if self.mode == "refuse":
            _rst(cw)
            return
        if self.mode == "hang":
            self._hung.append(cw)
            return
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        try:
            ur, uw = await asyncio.open_connection(*self.target)
        except OSError:
            _rst(cw)
            return
        conn = Conn(self, cr, cw, ur, uw)
        self.conns.append(conn)
        if self.max_life is not None:
            asyncio.get_running_loop().call_later(self.max_life, conn.rst)


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
