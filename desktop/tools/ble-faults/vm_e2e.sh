#!/bin/bash
# Inside the Linux VM after vm_bt_up.sh: phone (bluez_phone.py) on hci0, real orbitd + bleak on hci1,
# btmon capturing both. Logs land in /tmp/e2e. Usage: [ORBIT_BLE_LINK_TIMEOUT=25] vm_e2e.sh <seconds>
# Expects ~/orbit to hold a copy of desktop/ with a .venv (bleak, dbus-fast, the package itself).
set -u
cd ~/orbit
SECS=${1:-30}
W=/tmp/e2e
sudo rm -rf $W
mkdir -p $W/cfg
.venv/bin/python -c "
from pathlib import Path
from orbit.proto import KeyPair, device_id
from orbit.store import PeerStore, PinnedPeer
k = KeyPair.generate(); Path('$W/phone.pem').write_bytes(k.to_pem())
PeerStore(Path('$W/cfg')).pin(PinnedPeer(device_id(k.public), 'VM Phone', 'android', k.public, None))
"
sudo sh -c "nohup btmon -w $W/trace.btsnoop > /dev/null 2>&1 & echo \$! > $W/btmon.pid"
sleep 0.5
sudo sh -c "nohup $HOME/orbit/.venv/bin/python tools/ble-faults/bluez_phone.py --adapter hci0 --identity $W/phone.pem --btmgmt $HOME/src/bluez-5.72/tools/btmgmt > $W/phone.log 2>&1 & echo \$! > $W/phone.pid"
sleep 2
sudo sh -c "ORBIT_BLE_LINK_TIMEOUT=${ORBIT_BLE_LINK_TIMEOUT:-} ORBIT_SKIP_ACQUIRE_MTU=${ORBIT_SKIP_ACQUIRE_MTU:-} nohup $HOME/orbit/.venv/bin/python tools/ble-faults/orbitd_on.py hci1 -v --no-mdns --name vm-laptop --config-dir $W/cfg --socket $W/s.sock > $W/orbitd.log 2>&1 & echo \$! > $W/orbitd.pid"
sleep "$SECS"
sudo kill -INT "$(cat $W/orbitd.pid)"
sleep 2
sudo kill "$(cat $W/phone.pid)" "$(cat $W/btmon.pid)"
