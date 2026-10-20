"""Measure orbitd's BLE link to a real, paired phone over GATT and over the L2CAP channel.

    uv run python tools/ble-bench/bench.py --config-dir ~/Library/Application\\ Support/orbit
    uv run python tools/ble-bench/bench.py --config-dir ... --transport l2cap --runs 5 --pings 100

Each run starts a separate orbitd (its own socket, no mDNS, ORBIT_BLE_TRANSPORT=<transport>) on a
copy of the config dir, so the phone's pin of this computer matches; LAN addresses are dropped
from the copy, so the session is BLE. Stop the everyday orbitd first (see README.md).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import signal
import statistics
import sys
import tempfile
import time
from pathlib import Path

from orbit.client import ApiError, LocalClient

BIN = Path(sys.executable).parent
DISCOVERED = re.compile(r"(?:discovered .* via BLE at|now advertises via BLE at) (\S+)$")
LINK = re.compile(r"BLE link to (\S+), MTU (\d+), via (.+)$")
ENDED = re.compile(r"BLE link to \S+ via .+ ended after ([\d.]+) s: (\d+) bytes in, (\d+) bytes out")
FAILED = re.compile(r"connecting to .* failed: (.*); retrying")


class Orbitd:
    """orbitd as a subprocess; its log lines are kept with their arrival time (time.monotonic)."""

    def __init__(self, argv: list[str], env: dict[str, str], log: Path) -> None:
        self.argv, self.env, self.log = argv, env, log
        self.lines: list[tuple[float, str]] = []
        self._new = asyncio.Event()
        self.proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.log.open("w") as f:
            while line := await self.proc.stdout.readline():
                text = line.decode(errors="replace").rstrip()
                self.lines.append((time.monotonic(), text))
                self._new.set()
                f.write(text + "\n")
                f.flush()
        self._new.set()

    async def wait_line(self, pattern: re.Pattern, after: float, timeout: float, what: str) -> tuple[float, re.Match]:
        """The first line since ``after`` that matches ``pattern``: (arrival time, match)."""
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            for t, text in self.lines[seen:]:
                if t >= after and (m := pattern.search(text)):
                    return t, m
            seen = len(self.lines)
            if self.proc and self.proc.returncode is not None:
                raise SystemExit(f"bench: orbitd exited ({self.proc.returncode}) waiting for {what}; log: {self.log}")
            if time.monotonic() >= deadline:
                failures = [m[1] for t, text in self.lines if t >= after and (m := FAILED.search(text))]
                hint = f"; last attempt: {failures[-1]}" if failures else ""
                raise SystemExit(f"bench: no {what} within {timeout:g} s{hint}; log: {self.log}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), deadline - time.monotonic())
            except TimeoutError:
                pass

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._pump:
            await self._pump


class Activities:
    """Follows activities.changed for one device: when the snapshot arrived and when each new art key did."""

    def __init__(self, api: LocalClient, dev_id: str) -> None:
        self.dev_id = dev_id
        self.snapshot: float | None = None
        self.last_art: float | None = None
        self.keys: set[str] = set()
        self._task = asyncio.create_task(self._watch(api))

    async def _watch(self, api: LocalClient) -> None:
        async for event, data in api.events():
            if event != "activities.changed":
                continue
            now = time.monotonic()
            acts = [a for a in data["activities"] if a["device"] == self.dev_id and not a.get("stale")]
            if acts and self.snapshot is None:
                self.snapshot = now
            keys = {a[k] for a in acts for k in ("artKey", "iconKey") if a.get(k)}
            if keys - self.keys:
                self.keys |= keys
                self.last_art = now

    async def settled(self, timeout: float, quiet: float) -> tuple[float, float]:
        """(snapshot time, time of the last new art) once no new art arrived for ``quiet`` s."""
        deadline = time.monotonic() + timeout
        while self.snapshot is None:
            if time.monotonic() > deadline:
                raise SystemExit("bench: no activities from the phone; start some media playback on it")
            await asyncio.sleep(0.05)
        while time.monotonic() - max(self.snapshot, self.last_art or 0) < quiet:
            await asyncio.sleep(0.1)
        return self.snapshot, self.last_art or self.snapshot

    def cancel(self) -> None:
        self._task.cancel()


def prepare_config(src: Path, dst: Path, device: str | None) -> tuple[str, str]:
    """Copy the identity and the one phone's pin; returns (device id, name)."""
    peers = json.loads((src / "peers.json").read_text())
    matches = [pid for pid in peers if device is None or pid.startswith(device) or peers[pid]["name"] == device]
    if len(matches) != 1:
        listed = ", ".join(f"{pid[:8]} ({p['name']})" for pid, p in peers.items()) or "none"
        raise SystemExit(f"bench: pick one paired device with --device (paired: {listed})")
    (dev_id,) = matches
    dst.mkdir(mode=0o700)
    shutil.copy2(src / "identity.pem", dst / "identity.pem")
    pin = dict(peers[dev_id], address=None, last_lan=None)  # BLE only
    (dst / "peers.json").write_text(json.dumps({dev_id: pin}))
    os.chmod(dst / "peers.json", 0o600)
    return dev_id, pin["name"]


async def connect_api(path: Path, timeout: float) -> LocalClient:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await LocalClient.connect(path)
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"bench: orbitd's socket {path} did not come up") from None
            await asyncio.sleep(0.1)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


async def run_once(args: argparse.Namespace, transport: str, work: Path) -> dict:
    work.mkdir(parents=True)
    dev_id, name = prepare_config(args.config_dir, work / "config", args.device)
    sock = work / "orbitd.sock"
    env = dict(os.environ, ORBIT_BLE_TRANSPORT=transport)
    argv = [*args.orbitd, "--config-dir", str(work / "config"), "--socket", str(sock), "--no-mdns", "-v"]
    orbitd = Orbitd(argv, env, work / "orbitd.log")
    started = time.monotonic()
    await orbitd.start()
    activities = None
    try:
        api = await connect_api(sock, 15)
        api.call_timeout = 40
        await api.call("subscribe")
        activities = Activities(api, dev_id)
        t_seen, _ = await orbitd.wait_line(DISCOVERED, started, args.connect_timeout, f"advertisement from {name}")
        t_link, link = await orbitd.wait_line(LINK, started, args.connect_timeout, "BLE link")
        via = link[3]
        if transport != "auto" and not via.lower().startswith(transport):
            raise SystemExit(f"bench: asked for {transport} but the link is via {via}")
        t_snapshot, t_art = await activities.settled(args.connect_timeout, args.settle)
        images, art = set(activities.keys), 0
        for key in images:
            art += len(base64.b64decode((await api.call("art.get", {"device": dev_id, "key": key}))["data"]))
        t_disconnect = time.monotonic()
        await api.call("devices.disconnect", {"id": dev_id})
        _, ended = await orbitd.wait_line(ENDED, t_disconnect, 10, "end of the BLE link")
        received = int(ended[2])

        # Pings on a second link: the art is cached by now, so little else is on the air.
        t_reconnect = time.monotonic()
        await api.call("devices.connect", {"id": dev_id})
        t_link2, _ = await orbitd.wait_line(LINK, t_reconnect, args.connect_timeout, "second BLE link")
        rtts = []
        for _ in range(args.pings):
            rtts.append((await api.call("devices.ping", {"id": dev_id}))["rttMs"])
        await api.close()
    except ApiError as e:
        raise SystemExit(f"bench: {e.code}: {e.message}; log: {orbitd.log}") from None
    finally:
        if activities:
            activities.cancel()
        await orbitd.stop()
    burst = max(t_art - t_link, 1e-3)
    return {
        "transport": transport,
        "via": via,
        "mtu": int(link[2]),
        "link_setup_s": t_link - t_seen,
        "snapshot_s": t_snapshot - t_link,
        "snapshot_art_s": t_art - t_link,
        "art_images": len(images),
        "art_bytes": art,
        "bytes_in": received,
        "bytes_per_s_in": received / burst,
        "reconnect_s": t_link2 - t_reconnect,
        "ping_ms_median": statistics.median(rtts),
        "ping_ms_p90": percentile(rtts, 90),
        "ping_ms_min": min(rtts),
        "ping_ms_max": max(rtts),
        "pings": rtts,
        "log": str(orbitd.log),
    }


def describe(r: dict) -> str:
    return (
        f"{r['via']}, MTU {r['mtu']}: link {r['link_setup_s']:.2f} s, snapshot +{r['snapshot_s']:.2f} s, "
        f"snapshot+art ({r['art_images']} images, {r['art_bytes']} B) +{r['snapshot_art_s']:.2f} s, "
        f"{r['bytes_in']} B in at {r['bytes_per_s_in'] / 1000:.1f} KB/s; "
        f"ping median {r['ping_ms_median']:.0f} ms, p90 {r['ping_ms_p90']:.0f} ms "
        f"(min {r['ping_ms_min']:.0f}, max {r['ping_ms_max']:.0f}, n={len(r['pings'])})"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", type=Path, required=True, help="the everyday orbitd's config dir (only read)")
    ap.add_argument("--device", help="paired phone: id prefix or name (default: the only one)")
    ap.add_argument("--transport", default="gatt,l2cap", help="comma-separated: gatt, l2cap, auto (default gatt,l2cap)")
    ap.add_argument("--runs", type=int, default=3, help="runs per transport, each with a fresh orbitd (default 3)")
    ap.add_argument("--pings", type=int, default=50, help="pings per run (default 50)")
    ap.add_argument("--settle", type=float, default=10.0, help="art is complete after this long without new art (s)")
    ap.add_argument("--connect-timeout", type=float, default=60.0, help="for the advertisement and the link (s)")
    ap.add_argument("--orbitd", nargs="+", default=[str(BIN / "orbitd")], help="command that runs orbitd")
    ap.add_argument("--json", type=Path, help="also write every run's numbers here")
    args = ap.parse_args()
    args.config_dir = args.config_dir.expanduser()
    transports = [t.strip() for t in args.transport.split(",") if t.strip()]
    if not transports or set(transports) - {"gatt", "l2cap", "auto"}:
        ap.error("--transport takes gatt, l2cap and/or auto")

    work = Path(tempfile.mkdtemp(prefix="orbit-bench-", dir="/tmp"))  # short: Unix socket paths
    print(f"bench: logs in {work}", flush=True)
    results: dict[str, list[dict]] = {t: [] for t in transports}
    for n in range(args.runs):
        for transport in transports:  # interleaved, so a drifting radio environment hits both alike
            r = await run_once(args, transport, work / f"{transport}-{n + 1}")
            results[transport].append(r)
            print(f"{transport} run {n + 1}: {describe(r)}", flush=True)

    print("\nmedians over runs:")
    keys = ("link_setup_s", "snapshot_s", "snapshot_art_s", "bytes_per_s_in", "ping_ms_median", "ping_ms_p90")
    print(f"{'':8}" + "".join(f"{k:>16}" for k in keys))
    for transport, runs in results.items():
        print(f"{transport:8}" + "".join(f"{statistics.median(r[k] for r in runs):16.2f}" for k in keys))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
