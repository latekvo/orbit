"""An Orbit "phone" as a BlueZ GATT peripheral, with Android BleServer's link semantics.

For a Linux VM with two virtual controllers (BlueZ emulator/btvirt): run this on one adapter and
orbitd (real bleak, real BlueZ) on the other. Mirrors BleServer.kt:
  - service 616dfa5c... with RX (write) and TX (notify); id prefix as service data in the scan response;
  - enabling TX notifications starts a new responder session (any previous one ends);
  - disabling them ends the session and drops the connection (BleLink.close -> cancelConnection).

    python bluez_phone.py --adapter hci0 --identity phone.pem --btmgmt ~/src/bluez-5.72/tools/btmgmt

Keep it on hci0: btmgmt add-adv was seen acting on index 0 whatever --index said.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from dbus_fast import BusType, Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from orbit.proto import FrameStream, KeyPair, device_id, respond

SERVICE = "616dfa5c-069e-4839-8335-87edbeb2c1a5"
RX = "616dfa5d-069e-4839-8335-87edbeb2c1a5"
TX = "616dfa5e-069e-4839-8335-87edbeb2c1a5"
APP = "/dev/orbit/phone"
T0 = time.monotonic()


def say(*a):
    print(f"[phone {time.monotonic() - T0:7.2f}]", *a, flush=True)


class Session:
    """One responder session over the GATT link (BleServer.BleLink)."""

    def __init__(self, phone: Phone, device: str):
        self.phone = phone
        self.device = device
        self.reader = asyncio.StreamReader()
        self.closed = False
        self.task = asyncio.ensure_future(self.run())

    def write(self, data: bytes) -> None:
        if not self.closed:
            self.phone.notify(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self, why="closed") -> None:
        if self.closed:
            return
        self.closed = True
        say(f"session on {self.device} ends: {why}")
        self.task.cancel()
        asyncio.ensure_future(self.phone.disconnect(self.device))

    async def run(self):
        from orbit.daemon.timeconv import now_ms

        try:
            ch = await respond(FrameStream(self.reader, self), self.phone.identity, "VM Phone", "android")
            say(f"handshake done with {ch.peer.name} ({ch.peer.id})")
            await ch.send({"t": "ready", "paired": True, "now": now_ms()})
            await ch.recv()
            acts = [
                {"id": "media:x", "kind": "media", "title": "VM", "actions": [{"id": "media.pause", "label": "Pause"}]}
            ]
            await ch.send({"t": "snapshot", "now": now_ms(), "activities": acts})
            while True:
                msg = await ch.recv()
                if msg["t"] == "ping":
                    await ch.send({"t": "pong", "req": msg["req"]})
                elif msg["t"] == "action":
                    say(f"action {msg['action']} applied")
                    await ch.send({"t": "action_result", "req": msg["req"], "ok": True})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.close(f"{type(e).__name__}: {e}")


class Characteristic(ServiceInterface):
    def __init__(self, phone: Phone, path: str, uuid: str, flags: list[str]):
        super().__init__("org.bluez.GattCharacteristic1")
        self.phone, self.path, self._uuid, self._flags = phone, path, uuid, flags
        self._value = b""

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> s:  # noqa: N802,F722,F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> o:  # noqa: N802,F722,F821
        return APP + "/service0"

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802,F722,F821
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> ay:  # noqa: N802,F722,F821
        return self._value

    @method()
    def WriteValue(self, value: ay, options: "a{sv}"):  # noqa: N802,F722,F821
        device = options.get("device").value if "device" in options else "?"
        mtu = options.get("mtu").value if "mtu" in options else None
        if mtu:
            self.phone.mtu = mtu
        session = self.phone.sessions.get(device)
        if session is None and "?" in self.phone.sessions:  # subscribed before Connected was signalled
            session = self.phone.sessions[device] = self.phone.sessions.pop("?")
            session.device = device
        if session is None:
            say(f"write of {len(value)} bytes from {device} without a session: GATT_FAILURE")
            raise_failed()
        session.reader.feed_data(bytes(value))

    @method()
    def StartNotify(self):  # noqa: N802
        self.phone.on_cccd(True)

    @method()
    def StopNotify(self):  # noqa: N802
        self.phone.on_cccd(False)


def raise_failed():
    from dbus_fast import DBusError

    raise DBusError("org.bluez.Error.Failed", "no link")


class GattService(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> s:  # noqa: N802,F722,F821
        return SERVICE

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> b:  # noqa: N802,F722,F821
        return True


class Phone:
    def __init__(self, bus: MessageBus, adapter: str, identity: KeyPair):
        self.bus, self.adapter, self.identity = bus, adapter, identity
        self.sessions: dict[str, Session] = {}
        self.mtu = 23
        self.rx = Characteristic(self, APP + "/service0/rx", RX, ["write"])
        self.tx = Characteristic(self, APP + "/service0/tx", TX, ["notify"])
        self.connected: set[str] = set()

    def on_cccd(self, enable: bool) -> None:
        # BlueZ (server role) reports CCCD changes per characteristic, not per central: with one
        # central connected, that central is the one.
        devices = sorted(self.connected) or ["?"]
        say(f"TX notifications {'enabled' if enable else 'disabled'} (centrals {devices})")
        for d in devices:
            if old := self.sessions.pop(d, None):
                old.close("new subscription" if enable else "notifications disabled")
            if enable:
                self.sessions[d] = Session(self, d)

    def notify(self, data: bytes) -> None:
        step = max(20, min(244, self.mtu - 3))
        for i in range(0, len(data), step):
            self.tx._value = data[i : i + step]
            self.tx.emit_properties_changed({"Value": self.tx._value})

    async def disconnect(self, device: str) -> None:
        if device == "?":  # like Android's onDescriptorWriteRequest, which always knows the central
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            )
            up = [
                path
                for path, ifaces in reply.body[0].items()
                if path.startswith(f"/org/bluez/{self.adapter}/dev_")
                and ifaces.get("org.bluez.Device1", {}).get("Connected", Variant("b", False)).value
            ]
            if not up:
                return
            device = up[0]
        say(f"cancelConnection({device})")
        reply = await self.bus.call(
            Message(destination="org.bluez", path=device, interface="org.bluez.Device1", member="Disconnect")
        )
        if reply.error_name:
            say(f"  Disconnect: {reply.error_name} {reply.body}")

    async def start(self, prefix: bytes):
        self.service = GattService()
        self.bus.export(APP + "/service0", self.service)
        self.bus.export(self.rx.path, self.rx)
        self.bus.export(self.tx.path, self.tx)
        adapter_path = f"/org/bluez/{self.adapter}"
        for member, iface, path in (("RegisterApplication", "org.bluez.GattManager1", APP),):
            reply = await self.bus.call(
                Message(
                    destination="org.bluez",
                    path=adapter_path,
                    interface=iface,
                    member=member,
                    signature="oa{sv}",
                    body=[path, {}],
                )
            )
            say(member, reply.error_name or "ok", reply.body if reply.error_name else "")
        self.bus.add_message_handler(self._signal)
        await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=["type='signal',sender='org.bluez',member='PropertiesChanged'"],
            )
        )

    def _signal(self, msg: Message):
        if msg.member != "PropertiesChanged" or not (msg.path or "").startswith(f"/org/bluez/{self.adapter}/dev_"):
            return
        iface, changed, _ = msg.body
        if iface == "org.bluez.Device1" and "Connected" in changed:
            up = changed["Connected"].value
            say(f"central {msg.path.rsplit('/', 1)[-1]} {'connected' if up else 'disconnected'}")
            if up:
                self.connected.add(msg.path)
            else:
                self.connected.discard(msg.path)
                if s := self.sessions.pop(msg.path, None):
                    s.close("central disconnected")


def advertise(btmgmt: str, adapter: str, prefix: bytes) -> None:
    """Like BleServer: the service UUID in the advertisement, the id prefix as service data in the
    scan response (BlueZ's LEAdvertisement1 cannot set a scan response, so via mgmt directly)."""
    import subprocess
    import uuid

    le = uuid.UUID(SERVICE).bytes[::-1]
    adv = bytes([17, 0x07]) + le
    rsp = bytes([1 + 16 + len(prefix), 0x21]) + le + prefix
    index = adapter.removeprefix("hci")
    cmd = f"{btmgmt} --index {index} add-adv -c -g -d {adv.hex()} -s {rsp.hex()} 1"
    out = subprocess.run(  # btmgmt only exits after a command when it runs on a terminal
        ["script", "-qc", cmd, "/dev/null"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=5,
    )
    say("add-adv:", (out.stdout + out.stderr).strip().splitlines()[-1:])


async def main(args, identity: KeyPair):
    pid = device_id(identity.public)
    say(f"phone id {pid}")
    advertise(args.btmgmt, args.adapter, bytes.fromhex(pid[:16]))
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    phone = Phone(bus, args.adapter, identity)
    await phone.start(bytes.fromhex(pid[:16]))
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    p = argparse.ArgumentParser()
    p.add_argument("--adapter", default="hci0")
    p.add_argument("--identity", required=True)
    p.add_argument("--btmgmt", default="btmgmt")
    args = p.parse_args()
    path = Path(args.identity)
    if not path.exists():
        path.write_bytes(KeyPair.generate().to_pem())
    asyncio.run(main(args, KeyPair.from_pem(path.read_bytes())))
