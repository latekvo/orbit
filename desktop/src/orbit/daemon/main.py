"""``orbitd`` entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path

from ..paths import config_dir, socket_path
from .api import StartupError
from .core import Daemon


def default_name() -> str:
    return socket.gethostname().removesuffix(".local") or "computer"


async def run(args: argparse.Namespace) -> None:
    daemon = Daemon(
        Path(args.config_dir) if args.config_dir else config_dir(),
        args.name or default_name(),
        Path(args.socket) if args.socket else socket_path(),
        mdns=not args.no_mdns,
        ble=not args.no_ble,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await daemon.start()
    try:
        await stop.wait()
    finally:
        logging.getLogger(__name__).info("shutting down")
        await daemon.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="orbitd", description="Orbit desktop daemon: mirrors your phone's live activities."
    )
    p.add_argument("--name", help="device name shown on the phone (default: hostname)")
    p.add_argument("--config-dir", help="identity and paired devices directory (default: platform config dir)")
    p.add_argument("--socket", help="local API socket path (default: $ORBIT_SOCKET or platform default)")
    p.add_argument("--no-mdns", action="store_true", help="disable mDNS/DNS-SD discovery")
    p.add_argument("--no-ble", action="store_true", help="disable Bluetooth LE discovery and transport")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("zeroconf").setLevel(logging.WARNING)
        logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except StartupError as e:
        print(f"orbitd: {e}", file=sys.stderr)
        sys.exit(1)
