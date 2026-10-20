"""orbit-fakephone (the real FakePhone responder) with a control socket and a timestamped event log.

    python /harness/phone.py --config-dir C [--port P] [--advertise A,B,...] [--ctl /tmp/phone.ctl]

Control: one JSON line per connection to --ctl, answered with one JSON line:
  {"cmd": "status"}                      port, advertised addresses, sessions
  {"cmd": "big", "size": N}              upsert an activity whose art goes in one ~N-byte frame
  {"cmd": "burst", "n": N, "size": S}    N upserts, each with new ~S-byte art (album-art burst)
  {"cmd": "advertise", "addresses": [...]}  re-announce the service with these addresses ([] = current interfaces)
Events (session, session_closed, action, ...) are appended to --events as {"t", "kind", ...}.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import struct
import sys
import time
import zlib
from pathlib import Path

import orbit.fakephone.phone as phone_mod
from orbit.fakephone.mdns import SERVICE_TYPE, Advertiser, advertised_addresses
from zeroconf.asyncio import AsyncServiceInfo

OVERRIDE: list[str] | None = None


class OverrideAdvertiser(Advertiser):
    def __init__(self, name, device_id, platform, host, port):
        super().__init__(name, device_id, platform, host, port)
        if OVERRIDE:
            self._set(OVERRIDE)

    def _set(self, addresses):
        self.addresses = list(addresses)
        self.info = AsyncServiceInfo(
            SERVICE_TYPE,
            self.info.name,
            port=self.info.port,
            properties=self.info.properties,
            server=self.info.server,
            parsed_addresses=self.addresses,
        )

    async def readvertise(self, addresses):
        """Re-register on a fresh zeroconf (the old one's sockets are bound to the old interface address),
        the way Android's NSD service re-announces after a network change."""
        import zeroconf as zc_mod
        from zeroconf.asyncio import AsyncZeroconf

        with __import__("contextlib").suppress(Exception):
            await self._zc.async_close()
        self._set(addresses or advertised_addresses("0.0.0.0"))
        v6 = any(":" in a for a in self.addresses)
        self._zc = AsyncZeroconf(ip_version=zc_mod.IPVersion.All if v6 else zc_mod.IPVersion.V4Only)
        await (await self._zc.async_register_service(self.info, allow_name_change=True))


phone_mod.Advertiser = OverrideAdvertiser


def png_of_size(size: int, seed: int) -> bytes:
    """A valid PNG whose (incompressible) ancillary chunk makes it ~size bytes."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\xff")
    pad = os.urandom(max(0, size - 60)) + (seed % 2**32).to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"zzZz", pad) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


async def control(phone, reader, writer):
    try:
        req = json.loads(await reader.readline())
        cmd = req["cmd"]
        if cmd == "status":
            out = {
                "id": phone.device_id,
                "port": phone.port,
                "addresses": phone.advertiser.addresses if phone.advertiser else None,
                "sessions": [
                    {"id": s.id, "peer": s.peer.id, "paired": s.paired, "address": s.address} for s in phone.sessions
                ],
            }
        elif cmd == "big":
            key = phone.add_art(png_of_size(int(req["size"]), int(time.time() * 1000)))
            part_bytes, phone_mod.ART_PART_BYTES = phone_mod.ART_PART_BYTES, 1 << 30  # one frame, not parts
            try:
                phone.upsert({"id": "big", "kind": "generic", "app": {"id": "big", "name": "Big"}, "title": "big",
                              "artKey": key})
            finally:
                phone_mod.ART_PART_BYTES = part_bytes
            out = {"key": key}
        elif cmd == "burst":
            keys = []
            for i in range(int(req["n"])):
                key = phone.add_art(png_of_size(int(req["size"]), i + int(time.time() * 1000)))
                keys.append(key)
                phone.upsert({"id": f"burst{i % 4}", "kind": "media", "app": {"id": "b", "name": "B"},
                              "title": f"track {i}", "artKey": key})
            out = {"keys": len(keys)}
        elif cmd == "advertise":
            await phone.advertiser.readvertise(req.get("addresses") or [])
            out = {"addresses": phone.advertiser.addresses}
        else:
            out = {"error": f"unknown cmd {cmd}"}
    except Exception as e:
        out = {"error": repr(e)}
    writer.write((json.dumps(out) + "\n").encode())
    await writer.drain()
    writer.close()


async def main():
    global OVERRIDE
    p = argparse.ArgumentParser()
    p.add_argument("--config-dir", required=True)
    p.add_argument("--name", default="Fake Phone")
    p.add_argument("--port", type=int, default=47820)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--advertise", default="")
    p.add_argument("--ctl", default="/tmp/phone.ctl")
    p.add_argument("--events", default="/tmp/phone-events.jsonl")
    p.add_argument("--tick", type=float, default=1.0)
    args = p.parse_args()
    OVERRIDE = [a for a in args.advertise.split(",") if a] or None
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s")
    phone = phone_mod.FakePhone(Path(args.config_dir), name=args.name, host=args.host, port=args.port,
                                auto_accept=True, tick=args.tick)
    events = open(args.events, "a")
    emit = phone._emit

    def logged_emit(kind, data):
        events.write(json.dumps({"t": time.time(), "kind": kind, **{k: v for k, v in data.items() if k != "activity"}}) + "\n")
        events.flush()
        emit(kind, data)

    phone._emit = logged_emit
    await phone.start()
    with __import__("contextlib").suppress(FileNotFoundError):
        os.unlink(args.ctl)
    server = await asyncio.start_unix_server(lambda r, w: control(phone, r, w), args.ctl)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()
    await phone.stop()


asyncio.run(main())
