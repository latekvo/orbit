#!/bin/bash
# Inside the VM: vm_e2e.sh for <total> s while powering orbitd's adapter (hci1) off at <off_at> s
# and back on <off_for> s later. Usage: vm_power_cycle.sh <total> <off_at> <off_for>
# The same D-Bus property the desktop's Bluetooth toggle sets.
power() { sudo busctl set-property org.bluez /org/bluez/hci1 org.bluez.Adapter1 Powered b "$1" && echo ok; }
(
  sleep "$2"; echo "[cycle $(date +%T.%3N)] power off: $(power false)"
  sleep "$3"; echo "[cycle $(date +%T.%3N)] power on: $(power true)"
) &
ORBIT_SKIP_ACQUIRE_MTU=1 ORBIT_BLE_LINK_TIMEOUT=${ORBIT_BLE_LINK_TIMEOUT:-25} bash ~/orbit/tools/ble-faults/vm_e2e.sh "$1"
wait
