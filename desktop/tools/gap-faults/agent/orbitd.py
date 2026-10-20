"""The real ``orbitd`` entry point (orbit.daemon.main.main), plus a once-a-second resource/state dump.

    python /harness/orbitd.py --config-dir C --socket S [orbitd args...]

ORBIT_STATS (default /tmp/orbitd-stats.jsonl) receives one JSON line per second.
"""

import asyncio
import json
import os
import threading
import time

import orbit.daemon.main as entry
from orbit.daemon.core import Daemon

STATS = os.environ.get("ORBIT_STATS", "/tmp/orbitd-stats.jsonl")

# ORBIT_SLEEP_FILE: seconds (float) to hide from time.monotonic(). A suspended laptop's monotonic
# clock (macOS mach_absolute_time, Linux CLOCK_MONOTONIC) does not advance during sleep; SIGSTOP
# alone does not reproduce that, so a "sleep" test writes the stopped duration here before SIGCONT.
SLEEP_FILE = os.environ.get("ORBIT_SLEEP_FILE")
if SLEEP_FILE:
    _real_monotonic = time.monotonic
    _cache = [None, 0.0]

    def _monotonic() -> float:
        try:
            mtime = os.stat(SLEEP_FILE).st_mtime_ns
            if mtime != _cache[0]:
                with open(SLEEP_FILE) as f:
                    _cache[:] = [mtime, float(f.read().strip() or 0)]
        except (OSError, ValueError):
            pass
        return _real_monotonic() - _cache[1]

    time.monotonic = _monotonic


def _status(field: str) -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1])
    return -1


class StatsDaemon(Daemon):
    async def start(self) -> None:
        await super().start()
        self._stats_task = asyncio.ensure_future(self._stats())

    async def _stats(self) -> None:
        with open(STATS, "a") as f:
            while True:
                lan = self._lan
                zc = getattr(lan, "_azc", None) if lan else None
                row = {
                    "t": time.time(),
                    "mono": time.monotonic(),
                    "tasks": len(asyncio.all_tasks()),
                    "bg": len(self._bg),
                    "api_tasks": len(self.api._tasks),
                    "lan_tasks": len(lan._tasks) if lan else None,
                    "sessions": len(self.sessions),
                    "devices": len(self.devices),
                    "fds": len(os.listdir("/proc/self/fd")),
                    "rss_kb": _status("VmRSS"),
                    "threads": threading.active_count(),
                    "os_threads": _status("Threads"),
                    "zc_ifaces": len(zc.zeroconf.engine.senders) if zc else None,
                    "devs": {
                        d.id[:8]: {
                            "state": "connected" if d.session else "reconnecting" if d.grace else "offline",
                            "lan": {k: v.addresses for k, v in d.lan.items()},
                            "acts": len(d.activities),
                            "connector": None if d.connector is None else ("done" if d.connector.done() else "running"),
                        }
                        for d in self.devices.values()
                    },
                }
                f.write(json.dumps(row) + "\n")
                f.flush()
                await asyncio.sleep(1)


entry.Daemon = StatsDaemon
entry.main()
