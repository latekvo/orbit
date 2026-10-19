"""Soak test: orbitd + orbit-fakephone over TCP for N minutes with periodic actions; reports every drop.

    uv run python tools/soak.py --minutes 10
    uv run python tools/soak.py --minutes 3 --restart-phone-every 45   # also exercise reconnects

Exit status 0 only if every session drop was one this script caused (--restart-phone-every) and
every action outside such a window succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

from orbit.client import ApiError, LocalClient

BIN = Path(sys.executable).parent
EXPLAINED_WINDOW = 20.0  # seconds after a deliberate phone restart in which drops/failures are expected


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proc:
    def __init__(self, name: str, argv: list[str], log: Path) -> None:
        self.name, self.argv, self.log = name, argv, log
        self.lines: list[tuple[float, str]] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.log.open("a") as f:
            while line := await self.proc.stdout.readline():
                text = line.decode(errors="replace").rstrip()
                self.lines.append((time.monotonic(), text))
                f.write(text + "\n")
                f.flush()

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._pump:
            await self._pump


async def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := await predicate():
            return value
        await asyncio.sleep(0.1)
    raise SystemExit(f"soak: timed out waiting for {what}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--action-every", type=float, default=2.0, help="seconds between actions (default 2)")
    ap.add_argument("--restart-phone-every", type=float, default=0.0, help="seconds; 0 = never (default)")
    ap.add_argument("--dir", type=Path, help="work directory (default: a new temp dir, kept)")
    args = ap.parse_args()

    work = args.dir or Path(tempfile.mkdtemp(prefix="orbit-soak-"))
    work.mkdir(parents=True, exist_ok=True)
    sock, port = work / "orbitd.sock", free_port()
    os.environ["ORBIT_SOCKET"] = str(sock)
    phone_argv = [str(BIN / "orbit-fakephone"), "--no-mdns", "--auto-accept", "--host", "127.0.0.1"]
    phone_argv += ["--port", str(port), "--config-dir", str(work / "phone")]
    daemon_argv = [str(BIN / "orbitd"), "--no-mdns", "--no-ble", "--name", "Soak Laptop"]
    daemon_argv += ["--config-dir", str(work / "daemon"), "--socket", str(sock)]
    phone = Proc("phone", phone_argv, work / "fakephone.log")
    daemon = Proc("orbitd", daemon_argv, work / "orbitd.log")
    await phone.start()
    await daemon.start()
    restarts: list[float] = []
    results: list[tuple[float, str, str, float]] = []  # (time, action, outcome, latency)
    try:
        api = await wait_for(lambda: _try_connect(sock), 10, "orbitd socket")
        await asyncio.sleep(0.5)  # phone listening
        dev_id = (await api.call("devices.connect", {"host": "127.0.0.1", "port": port}))["id"]
        (pairing,) = await wait_for(lambda: api.call("pairing.list"), 10, "pairing")
        await api.call("pairing.respond", {"session": pairing["session"], "accept": True})

        async def media():
            acts = await api.call("activities.list")
            return next((a for a in acts if a["device"] == dev_id and a["kind"] == "media"), None)

        await wait_for(media, 10, "media activity")
        print(f"soak: paired with {dev_id} on 127.0.0.1:{port}; running {args.minutes:g} min, logs in {work}")
        start = time.monotonic()
        end = start + args.minutes * 60
        next_restart = start + args.restart_phone_every if args.restart_phone_every else float("inf")
        n = 0
        while time.monotonic() < end:
            if time.monotonic() >= next_restart:
                restarts.append(time.monotonic())
                await phone.stop()
                await phone.start()
                next_restart += args.restart_phone_every
            n += 1
            m = await media()
            ids = [a["id"] for a in (m or {}).get("actions", [])]
            action = "media.next" if n % 3 == 0 else next((i for i in ids if i in ("media.pause", "media.play")), "")
            t0 = time.monotonic()
            try:
                if m is None or not action:
                    raise ApiError("not_found", "no media activity")
                r = await api.call("activities.action", {"device": dev_id, "activity": m["id"], "action": action})
                outcome = "ok" if r.get("ok") else f"refused: {r.get('error')}"
            except ApiError as e:
                outcome = f"{e.code}: {e.message}"
            results.append((t0, action, outcome, time.monotonic() - t0))
            if n % 30 == 0:
                done = (time.monotonic() - start) / 60
                print(f"soak: {done:.1f} min, {n} actions, {sum(r[2] != 'ok' for r in results)} failed", flush=True)
            await asyncio.sleep(max(0.0, args.action_every - (time.monotonic() - t0)))
        await api.close()
    finally:
        await daemon.stop()
        await phone.stop()

    def explained(t: float) -> bool:
        return any(0 <= t - r < EXPLAINED_WINDOW for r in restarts)

    drops = [(t, line) for t, line in daemon.lines if "session_closed" in line and "orbitd shutting down" not in line]
    reconnects = [
        float(m.group(1)) for _, line in daemon.lines if (m := re.search(r"reconnected .*latency=([\d.]+)s", line))
    ]
    unexplained = [line for t, line in drops if not explained(t)]
    failed = [r for r in results if r[2] != "ok"]
    failed_unexplained = [r for r in failed if not explained(r[0])]
    lat = sorted(r[3] for r in results if r[2] == "ok")

    print("\n== soak report")
    print(f"duration          {args.minutes:g} min, phone restarts {len(restarts)}")
    print(f"actions           {len(results)} sent, {len(results) - len(failed)} ok, {len(failed)} failed")
    if lat:
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
        print(f"action latency    p50 {p(0.5) * 1000:.0f} ms, p99 {p(0.99) * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms")
    print(f"session drops     {len(drops)} ({len(drops) - len(unexplained)} caused by phone restarts)")
    if reconnects:
        print(f"reconnect latency max {max(reconnects):.2f} s over {len(reconnects)} reconnects")
    for line in unexplained:
        print(f"UNEXPLAINED DROP  {line}")
    for t, action, outcome, _ in failed_unexplained:
        print(f"UNEXPLAINED FAIL  +{t - results[0][0]:.1f}s {action}: {outcome}")
    ok = not unexplained and not failed_unexplained and len(reconnects) >= len(restarts)
    print("RESULT            " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


async def _try_connect(sock: Path):
    try:
        return await LocalClient.connect(sock)
    except OSError:
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
