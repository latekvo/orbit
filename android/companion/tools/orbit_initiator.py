"""Test initiator for the Orbit Android companion, built on the desktop reference implementation.

Run from the repo's desktop/ directory so `orbit.proto` is importable:

    cd desktop && uv run --with bleak --with zeroconf python ../android/companion/tools/orbit_initiator.py \
        --tcp 127.0.0.1:47820 --duration 20 --action media:com.example media.pause

Transports: --tcp HOST:PORT, --mdns (resolve `_orbit._tcp` with zeroconf), --ble (bleak central).
Every received message is printed as one JSON line (art data abbreviated). Identity and pins are
kept in --state (default ~/.orbit-test-initiator).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

from orbit.proto import FrameStream, KeyPair, ProtocolError, initiate

SERVICE_UUID = "616dfa5c-069e-4839-8335-87edbeb2c1a5"
RX_UUID = "616dfa5d-069e-4839-8335-87edbeb2c1a5"
TX_UUID = "616dfa5e-069e-4839-8335-87edbeb2c1a5"


def log(kind: str, **fields) -> None:
    print(json.dumps({"ts": round(time.time(), 3), "ev": kind, **fields}, ensure_ascii=False), flush=True)


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        key = path / "identity.pem"
        if not key.exists():
            key.write_bytes(KeyPair.generate().to_pem())
        self.identity = KeyPair.from_pem(key.read_bytes())
        self.pins_file = path / "pins.json"
        self.pins = json.loads(self.pins_file.read_text()) if self.pins_file.exists() else {}

    def pinned(self, peer) -> bool:
        return self.pins.get(peer.id, {}).get("static") == base64.b64encode(peer.static).decode()

    def pin(self, peer) -> None:
        self.pins[peer.id] = {"name": peer.name, "static": base64.b64encode(peer.static).decode()}
        self.pins_file.write_text(json.dumps(self.pins, indent=1))

    def unpin(self, peer_id: str) -> None:
        self.pins.pop(peer_id, None)
        self.pins_file.write_text(json.dumps(self.pins, indent=1))


class BleWriter:
    """asyncio StreamWriter look-alike writing to the RX characteristic (write with response)."""

    def __init__(self, client) -> None:
        self.client = client
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        chunk = max(20, min(512, self.client.mtu_size - 3))
        while self.buf:
            part, self.buf = bytes(self.buf[:chunk]), self.buf[chunk:]
            await self.client.write_gatt_char(RX_UUID, part, response=True)

    def close(self) -> None:
        asyncio.ensure_future(self.client.disconnect())


async def open_ble(args):
    from bleak import BleakClient, BleakScanner

    log("ble_scan", seconds=args.scan)
    found = await BleakScanner.discover(timeout=args.scan, return_adv=True, service_uuids=[SERVICE_UUID])
    target = None
    for dev, adv in found.values():
        sd = adv.service_data.get(SERVICE_UUID)
        log("ble_seen", address=dev.address, name=adv.local_name, rssi=adv.rssi,
            service_uuids=adv.service_uuids, service_data=sd.hex() if sd else None)
        if SERVICE_UUID in [u.lower() for u in adv.service_uuids] or sd is not None:
            if args.device_prefix is None or (sd and sd.hex() == args.device_prefix):
                target = dev
    if target is None:
        raise SystemExit("no Orbit peripheral found")
    client = BleakClient(target)
    await client.connect()
    log("ble_connected", address=target.address, mtu=client.mtu_size)
    reader = asyncio.StreamReader()
    stats = {"chunks": 0, "max": 0}

    def on_notify(_char, data: bytearray) -> None:
        stats["chunks"] += 1
        stats["max"] = max(stats["max"], len(data))
        reader.feed_data(bytes(data))

    client.set_disconnected_callback(lambda _c: reader.feed_eof()) if hasattr(client, "set_disconnected_callback") else None
    await client.start_notify(TX_UUID, on_notify)
    return reader, BleWriter(client), stats


async def open_mdns(args):
    from zeroconf import ServiceBrowser, Zeroconf

    zc = Zeroconf()
    found: list = []

    class L:
        def add_service(self, z, type_, name):
            info = z.get_service_info(type_, name, 3000)
            if info:
                found.append(info)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    ServiceBrowser(zc, "_orbit._tcp.local.", L())
    deadline = time.time() + args.scan
    while time.time() < deadline and not found:
        await asyncio.sleep(0.2)
    zc.close()
    if not found:
        raise SystemExit("no _orbit._tcp service found")
    info = found[0]
    props = {k.decode(): (v.decode() if v else None) for k, v in info.properties.items()}
    host = info.parsed_addresses()[0]
    log("mdns_found", name=info.name, host=host, port=info.port, txt=props)
    return await asyncio.open_connection(host, info.port)


async def main(args) -> int:
    state = State(Path(args.state).expanduser())
    stats = None
    if args.ble:
        reader, writer, stats = await open_ble(args)
    elif args.mdns:
        reader, writer = await open_mdns(args)
    else:
        host, port = args.tcp.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
    stream = FrameStream(reader, writer)
    t0 = time.time()
    ch = await initiate(stream, state.identity, args.name, "macos")
    log("handshake", ms=round((time.time() - t0) * 1000), peer=ch.peer.id, name=ch.peer.name,
        platform=ch.peer.platform, sas=ch.sas)
    if args.expect_id and ch.peer.id != args.expect_id:
        log("wrong_device", expected=args.expect_id)
        return 2
    paired_local = state.pinned(ch.peer)
    await ch.send({"t": "ready", "paired": paired_local, "now": int(time.time() * 1000)})
    for raw in args.raw:
        await ch.send(json.loads(raw))
    sent_decision = False
    remote_accepted = False
    req = 100
    pending_actions = list(args.action or [])
    action_times: dict[int, float] = {}
    deadline = time.time() + args.duration
    paired = False

    async def run_actions():
        nonlocal req
        for act in pending_actions:
            await asyncio.sleep(args.action_delay)
            req += 1
            msg = {"t": "action", "req": req, "activity": act[0], "action": act[1]}
            if len(act) > 2:
                v = act[2]
                msg["value"] = int(v) if v.lstrip("-").isdigit() else v
            action_times[req] = time.time()
            log("send_action", **msg)
            await ch.send(msg)

    action_task = None
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ch.recv(), remaining)
            except asyncio.TimeoutError:
                break
            t = msg["t"]
            if t == "art":
                log("recv", t="art", key=msg["key"], mime=msg["mime"], bytes=len(base64.b64decode(msg["data"])))
                if args.save_art:
                    Path(args.save_art).mkdir(parents=True, exist_ok=True)
                    ext = "png" if msg["mime"] == "image/png" else "jpg"
                    (Path(args.save_art) / f"{msg['key']}.{ext}").write_bytes(base64.b64decode(msg["data"]))
            elif t == "action_result":
                log("recv", **msg, rtt_ms=round((time.time() - action_times.get(msg.get("req"), time.time())) * 1000))
            else:
                log("recv", **msg)
            if t == "ready":
                if msg["paired"] and paired_local:
                    paired = True
                elif not args.no_pair:
                    await ch.send({"t": "pair_request"})
                    log("pairing", sas=ch.sas, hint="confirm this code on the phone")
                    await ch.send({"t": "pair_decision", "accepted": True})
                    sent_decision = True
            elif t == "pair_decision":
                remote_accepted = bool(msg.get("accepted"))
                if not remote_accepted:
                    log("pairing_rejected")
                    return 3
                if sent_decision:
                    state.pin(ch.peer)
                    paired = True
                    log("paired", peer=ch.peer.id)
            elif t == "ping":
                await ch.send({"t": "pong", "req": msg.get("req")})
            elif t == "unpair":
                state.unpin(ch.peer.id)
                log("unpaired_by_phone")
                return 0
            elif t == "snapshot" and action_task is None and pending_actions:
                action_task = asyncio.ensure_future(run_actions())
            if paired and args.exit_after_snapshot and t == "snapshot":
                break
    except (ProtocolError, asyncio.IncompleteReadError, ConnectionError) as e:
        log("closed", error=str(e) or type(e).__name__)
    finally:
        if stats is not None:
            log("ble_stats", **stats)
        if args.bye:
            try:
                await ch.send({"t": "bye", "reason": "test done"})
            except Exception:
                pass
        ch.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--tcp", help="HOST:PORT")
    g.add_argument("--ble", action="store_true")
    g.add_argument("--mdns", action="store_true")
    p.add_argument("--state", default="~/.orbit-test-initiator")
    p.add_argument("--name", default="Test Mac")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--scan", type=float, default=8)
    p.add_argument("--device-prefix", help="16 hex chars of the deviceId expected in BLE service data")
    p.add_argument("--expect-id")
    p.add_argument("--action", nargs="+", action="append", metavar="ARG", help="ACTIVITY ACTION [VALUE]; repeatable")
    p.add_argument("--action-delay", type=float, default=2)
    p.add_argument("--raw", action="append", default=[], help="JSON message sent right after ready")
    p.add_argument("--no-pair", action="store_true", help="never send pair_request")
    p.add_argument("--exit-after-snapshot", action="store_true")
    p.add_argument("--save-art")
    p.add_argument("--bye", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
