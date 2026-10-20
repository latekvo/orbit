"""Run orbitd with bleak pinned to one BlueZ adapter (bleak picks an arbitrary powered one).

    python orbitd_on.py hci1 [orbitd args...]

ORBIT_BLE_LINK_TIMEOUT overrides the 4 s link timeout: btvirt only reports advertisements when a
scan (re)starts, so the kernel's scan-before-connect can take ~10 s there.
ORBIT_SKIP_ACQUIRE_MTU=1 makes bleak's BlueZ _acquire_mtu a no-op (control for the CCCD toggle it causes).
"""

import os
import sys

from bleak.backends.bluezdbus.manager import BlueZManager

from orbit.daemon import core
from orbit.daemon.main import main

adapter = f"/org/bluez/{sys.argv.pop(1)}"
BlueZManager.get_default_adapter = lambda self: adapter
if os.environ.get("ORBIT_SKIP_ACQUIRE_MTU"):
    from bleak.backends.bluezdbus.client import BleakClientBlueZDBus

    async def _no_acquire(self):
        pass

    BleakClientBlueZDBus._acquire_mtu = _no_acquire
if timeout := os.environ.get("ORBIT_BLE_LINK_TIMEOUT"):
    init = core.Daemon.__init__

    def patched(self, *a, **kw):
        init(self, *a, **kw)
        self.ble_link_timeout = float(timeout)
        self.connect_timeout = max(self.connect_timeout, float(timeout) + 5)

    core.Daemon.__init__ = patched
main()
