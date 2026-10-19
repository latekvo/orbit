#!/bin/bash
# Inside a Linux VM (tested: Lima, Ubuntu 24.04, kernel 6.8 with hci_vhci) with BlueZ 5.72 built in
# ~/src/bluez-5.72: two linked virtual LE controllers (btvirt) and the distro bluetoothd with -E.
sudo systemctl mask bluetooth >/dev/null 2>&1   # socket activation restarts it otherwise
sudo systemctl stop bluetooth 2>/dev/null
sudo killall -q btvirt bluetoothd; sleep 1
sudo modprobe hci_vhci
sudo sh -c "nohup $HOME/src/bluez-5.72/emulator/btvirt -L -l2 > /tmp/btvirt.log 2>&1 &"
sleep 2
sudo sh -c 'nohup /usr/libexec/bluetooth/bluetoothd -n -E > /tmp/bluetoothd.log 2>&1 &'
sleep 2
for a in hci0 hci1; do sudo busctl set-property org.bluez /org/bluez/$a org.bluez.Adapter1 Powered b true; done
ls /sys/class/bluetooth
