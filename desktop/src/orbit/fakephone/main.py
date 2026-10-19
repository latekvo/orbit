"""``orbit-fakephone``: run a simulated Orbit phone from the terminal."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path

from ..paths import config_dir
from .phone import DEFAULT_PORT, FakePhone, Session


class TerminalPrompt:
    """Asks "Pair with X? Code N matches? [y/N]" on stdout and reads the answer from stdin without blocking."""

    def __init__(self) -> None:
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        self._eof = asyncio.Event()
        self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        # A daemon thread, so a blocked readline never delays exit.
        threading.Thread(target=self._read, args=(loop,), daemon=True, name="stdin").start()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(self._lines.put_nowait, line)
        loop.call_soon_threadsafe(self._eof.set)

    async def __call__(self, session: Session) -> bool:
        async with self._lock:
            while not self._lines.empty():  # drop input typed before this prompt
                self._lines.get_nowait()
            print(f"Pair with {session.peer.name}? Code {session.sas} matches? [y/N]", flush=True)
            line = asyncio.ensure_future(self._lines.get())
            eof = asyncio.ensure_future(self._eof.wait())
            try:
                await asyncio.wait({line, eof}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                eof.cancel()
                if not line.done():
                    line.cancel()
            if not line.done() or line.cancelled():
                print("stdin is closed, cannot answer; use --auto-accept or --auto-reject", flush=True)
                await asyncio.Future()  # leave it to the pairing timeout
            return line.result().strip().lower() in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orbit-fakephone", description="Simulated Orbit phone (protocol Responder).")
    p.add_argument("--name", default="Fake Phone", help="device name (mDNS instance name, hello.name)")
    p.add_argument(
        "--config-dir", type=_path, default=None, help="identity and pins (default: <orbit config>/fakephone)"
    )
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port, 0 = any (default {DEFAULT_PORT}, falls back to any free port if taken)",
    )
    p.add_argument("--no-mdns", action="store_true", help="do not advertise _orbit._tcp")
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--auto-accept", action="store_true", help="accept every pairing request")
    decision.add_argument("--auto-reject", action="store_true", help="reject every pairing request")
    p.add_argument("--tick", type=float, default=1.0, help="seconds between simulation steps (default 1.0)")
    p.add_argument("--platform", default="android", help="hello.platform and TXT pf (default android)")
    p.add_argument("--keepalive", type=float, default=10.0, help="ping after this many idle seconds (default 10)")
    p.add_argument("--dead-after", type=float, default=30.0, help="close after this many silent seconds (default 30)")
    p.add_argument("--pairing-timeout", type=float, default=120.0, help="abandon pairing after seconds (default 120)")
    p.add_argument("-v", "--verbose", action="store_true", help="also log every message sent and received")
    return p


def _path(value: str) -> Path:
    return Path(value).expanduser()


async def run(args: argparse.Namespace) -> int:
    auto_accept = True if args.auto_accept else False if args.auto_reject else None
    phone = FakePhone(
        args.config_dir or config_dir() / "fakephone",
        name=args.name,
        host=args.host,
        port=args.port,
        mdns=not args.no_mdns,
        auto_accept=auto_accept,
        tick=args.tick,
        keepalive=args.keepalive,
        dead_after=args.dead_after,
        pairing_timeout=args.pairing_timeout,
        platform=args.platform,
        prompt=TerminalPrompt() if auto_accept is None else None,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await phone.start()
    except OSError as e:
        print(f"orbit-fakephone: cannot start: {e}", file=sys.stderr, flush=True)
        await phone.stop()
        return 1
    await stop.wait()
    phone.log.info("shutting down")
    await phone.stop()
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.verbose:
        logging.getLogger("orbit.fakephone").setLevel(logging.DEBUG)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
