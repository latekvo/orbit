"""A scripted radio around test_ble.FakeGattLink: advertisements, range, phone Bluetooth and the laptop adapter.

Models what CoreBluetooth/BlueZ do to orbitd, so connectivity faults can be replayed against the real
daemon (real sessions, real local API socket) without hardware:

- the phone advertises its Orbit service every ``adv_interval`` s while in range and its Bluetooth is on
  (macOS reports a peripheral every ~0.3 s; see tools/ble-faults/adv_rate.py);
- out of range, a connected link drops after ``supervision`` s and connect() to any address waits out
  bleak's timeout (CoreBluetooth never fails a connect by itself);
- ``rotate()`` changes the phone's private address (CoreBluetooth: a new peripheral identifier).
"""

import asyncio
import time
from types import SimpleNamespace

from test_ble import FakeGattLink


class Radio:
    def __init__(self, daemon, dev_id, address="ADDR-1", adv_interval=0.3, supervision=2.0):
        self.daemon = daemon
        self.prefix = dev_id[:16]
        self.address = address
        self.adv_interval = adv_interval
        self.supervision = supervision
        self.in_range = True
        self.phone_bt = True
        self.adverts = 0
        self._task = asyncio.ensure_future(self._advertise())
        self._addresses = {address}

    def _reachable(self):
        return self.in_range and self.phone_bt

    def _sync_gone(self):
        FakeGattLink.gone = set() if self._reachable() else set(self._addresses)

    async def _advertise(self):
        while True:
            if self._reachable():
                self.adverts += 1
                self.daemon.on_ble(self.prefix, SimpleNamespace(address=self.address))
            await asyncio.sleep(self.adv_interval)

    def rotate(self, address):
        self.address = address
        self._addresses.add(address)
        self._sync_gone()

    def links(self):
        return [i for i in FakeGattLink.instances if i.connected]

    async def leave(self):
        """Walk out of range: adverts stop at once, the link drops after the supervision timeout."""
        self.in_range = False
        self._sync_gone()
        await asyncio.sleep(self.supervision)
        for link in self.links():
            link.drop()

    def come_back(self):
        self.in_range = True
        self._sync_gone()

    async def phone_bluetooth_off(self):
        """GATT server gone: the phone closes every link at once (onConnectionStateChange)."""
        self.phone_bt = False
        self._sync_gone()
        for link in self.links():
            link.drop()

    def phone_bluetooth_on(self, address=None):
        if address:
            self.rotate(address)
        self.phone_bt = True
        self._sync_gone()

    def stop(self):
        self._task.cancel()


class StateLog:
    """Records (t, state) of one device and (t, [activity ids], stale) from the local API's events."""

    def __init__(self, api, dev_id):
        self.api = api
        self.dev_id = dev_id
        self.t0 = time.monotonic()
        self.states = []
        self.activities = []
        self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        await self.api.call("subscribe")
        async for event, data in self.api.events():
            t = round(time.monotonic() - self.t0, 2)
            if event == "devices.changed":
                for d in data["devices"]:
                    if d["id"] == self.dev_id and (not self.states or self.states[-1][1] != d["state"]):
                        self.states.append((t, d["state"]))
            elif event == "activities.changed":
                acts = [a for a in data["activities"] if a["device"] == self.dev_id]
                self.activities.append((t, [a["id"] for a in acts], any(a["stale"] for a in acts)))

    def mark(self):
        self.t0 = time.monotonic()
        self.states = self.states[-1:]
        self.activities = self.activities[-1:]

    def stop(self):
        self._task.cancel()


class _Everything:
    def __contains__(self, _item):
        return True


class Adapter:
    """The laptop's Bluetooth adapter, behind a stand-in for ``bleak.BleakScanner``.

    Powering off stops every scan and drops every link; powering on does not restart scans: both
    CoreBluetooth (scanForPeripherals after poweredOn) and BlueZ (StartDiscovery on the new/re-powered
    adapter) need the client to start scanning again. ``reason`` is what start() fails with while off.
    """

    def __init__(self):
        self.powered = True
        self.reason = "POWERED_OFF"
        self.scanners = []  # started and still receiving
        self.starts = 0
        adapter = self

        class Scanner:
            def __init__(self, detection_callback=None, service_uuids=None, **_):
                self.callback = detection_callback

            async def start(self):
                adapter.starts += 1
                if not adapter.powered:
                    from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason

                    messages = {
                        "POWERED_OFF": "Bluetooth device is turned off",
                        "DENIED_BY_USER": "Bluetooth access is denied by the user for the current application. "
                        "Check macOS privacy settings.",
                    }
                    raise BleakBluetoothNotAvailableError(
                        messages[adapter.reason], BleakBluetoothNotAvailableReason[adapter.reason]
                    )
                adapter.scanners.append(self)

            async def stop(self):
                if self in adapter.scanners:
                    adapter.scanners.remove(self)

        self.Scanner = Scanner

    def deliver(self, address, prefix):
        from orbit.daemon.ble import SERVICE_UUID

        adv = SimpleNamespace(service_data={SERVICE_UUID: bytes.fromhex(prefix)}, local_name=None)
        for s in list(self.scanners):
            s.callback(SimpleNamespace(address=address), adv)

    def power_off(self):
        self.powered = False
        self.scanners.clear()
        FakeGattLink.gone = _Everything()
        for link in [i for i in FakeGattLink.instances if i.connected]:
            link.drop()

    def power_on(self):
        self.powered = True
        FakeGattLink.gone = set()


class AdapterRadio(Radio):
    """A phone whose advertisements reach orbitd only through the (fake) adapter's running scans."""

    def __init__(self, daemon, dev_id, adapter, **kw):
        self.adapter = adapter
        super().__init__(daemon, dev_id, **kw)

    async def _advertise(self):
        while True:
            if self._reachable():
                self.adverts += 1
                self.adapter.deliver(self.address, self.prefix)
            await asyncio.sleep(self.adv_interval)
