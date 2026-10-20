"""Launch a real Orbit entry point (orbitd / orbit-fakephone) with test instrumentation.

    python wrap.py daemon -- <orbitd args>
    python wrap.py phone  -- <orbit-fakephone args>

Environment:
  ORBIT_WALL_SKEW_MS     shift the wall clock (time.time / time.time_ns) by this many ms
  ORBIT_SKEW_FILE        on SIGUSR2, re-read the skew (ms) from this file (a clock jump mid-run)
  ORBIT_FREEZE_MONO=1    time.monotonic() does not advance while the process is SIGSTOPped,
                         like CLOCK_MONOTONIC across a real laptop suspend (the wall clock still jumps)

SIGUSR1 prints one ``STATS {json}`` line (asyncio tasks, threads, fds, sessions...).
The phone also prints ``EVENT {json}`` for every FakePhone event (session, ready, paired, action...).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time

_real_time, _real_time_ns, _real_mono = time.time, time.time_ns, time.monotonic
_skew_ms = int(os.environ.get("ORBIT_WALL_SKEW_MS", "0"))


def _time() -> float:
    return _real_time() + _skew_ms / 1000


def _time_ns() -> int:
    return _real_time_ns() + _skew_ms * 1_000_000


time.time, time.time_ns = _time, _time_ns


def _reload_skew(*_: object) -> None:
    global _skew_ms
    path = os.environ.get("ORBIT_SKEW_FILE")
    if path:
        with open(path) as f:
            _skew_ms = int(f.read().strip() or 0)
        print(f"WRAP skew now {_skew_ms} ms", flush=True)


signal.signal(signal.SIGUSR2, _reload_skew)

if os.environ.get("ORBIT_FREEZE_MONO") == "1":
    _lock = threading.Lock()
    _state = {"last": _real_mono(), "offset": 0.0}
    GAP = 0.5  # a gap longer than this between observations means we were stopped

    def _observe() -> float:
        with _lock:
            real = _real_mono()
            gap = real - _state["last"]
            if gap > GAP:
                _state["offset"] += gap - 0.02
            _state["last"] = real
            return real - _state["offset"]

    def _ticker() -> None:
        while True:
            _real_sleep(0.02)
            _observe()

    _real_sleep = time.sleep
    time.monotonic = _observe
    threading.Thread(target=_ticker, daemon=True, name="mono-freeze").start()

_loop: asyncio.AbstractEventLoop | None = None
_objects: dict[str, object] = {}


def _stats(*_: object) -> None:
    out: dict[str, object] = {"pid": os.getpid(), "threads": threading.active_count()}
    try:
        out["fds"] = len(os.listdir("/dev/fd"))
    except OSError:
        pass
    if _loop is not None:
        out["tasks"] = len(asyncio.all_tasks(_loop))
    d = _objects.get("daemon")
    if d is not None:
        out.update(
            sessions=len(d.sessions),
            devices=len(d.devices),
            bg=len(d._bg),
            api_tasks=len(d.api._tasks),
            api_clients=len(d.api.clients),
            pending=sum(len(s._pending) for s in d.sessions.values()),
            art=sum(len(x.art) for x in d.devices.values()),
        )
    p = _objects.get("phone")
    if p is not None:
        out.update(sessions=len(p._sessions), conn_tasks=len(p._conn_tasks), waiters=len(p._waiters))
    print("STATS " + json.dumps(out), flush=True)


signal.signal(signal.SIGUSR1, _stats)


def _capture_loop() -> None:
    global _loop
    _loop = asyncio.get_running_loop()


def main() -> None:
    role = sys.argv[1]
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if role == "daemon":
        from orbit.daemon import core
        from orbit.daemon import main as dmain

        orig_start = core.Daemon.start

        async def start(self):  # type: ignore[no-untyped-def]
            _capture_loop()
            _objects["daemon"] = self
            await orig_start(self)

        core.Daemon.start = start
        dmain.main(argv)
    elif role == "phone":
        from orbit.fakephone import main as pmain
        from orbit.fakephone import phone

        orig_start = phone.FakePhone.start
        orig_emit = phone.FakePhone._emit

        async def pstart(self):  # type: ignore[no-untyped-def]
            _capture_loop()
            _objects["phone"] = self
            await orig_start(self)

        def emit(self, kind, data):  # type: ignore[no-untyped-def]
            print("EVENT " + json.dumps({"kind": kind, **data}, default=str), flush=True)
            orig_emit(self, kind, data)

        phone.FakePhone.start = pstart
        phone.FakePhone._emit = emit
        pmain.main(argv)
    else:
        raise SystemExit(f"unknown role {role}")


if __name__ == "__main__":
    main()
