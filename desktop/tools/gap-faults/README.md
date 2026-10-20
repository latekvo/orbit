# Connectivity gap lab

Scenarios no earlier connectivity probe ran. Real `orbitd` and `orbit-fakephone` in separate Docker
network namespaces (lab adapted from `conn/lan`'s `tools/lan-faults`; image `orbitgap`, containers
`orbitgap-*`, networks 10.232.X.0/24).

```sh
cd desktop
uv run --with pytest-xdist pytest tools/gap-faults -n 6 -m "not slow"   # ~2 min
uv run --with pytest-xdist pytest tools/gap-faults -n 6 -m slow         # ~5 min (lossy mDNS trials)
ORBIT_GAPS=1 uv run --with pytest-xdist pytest tests/gaps -n 3                    # LAN isolated + BLE, in-process
```

Each test writes what a user would observe to `results/<test>.json`. Failing tests are findings:

| test | situation |
|---|---|
| `test_ipv6_only.py` | IPv6-only Wi-Fi: orbitd's zeroconf is IPv4-only |
| `test_mdns_coexist.py` | avahi-daemon on the laptop (passes); UDP 5353 held without SO_REUSEADDR (orbitd exits) |
| `test_foreign_service.py` | HTTP / silent / tarpit / closing service on the target port |
| `test_lan_gaps.py` | laptop VPN mid-session, two phones with one name, TXT `v=2`, laptop host firewall, shared mDNS host name |
| `test_mdns_lossy.py` | lossy or late multicast from the phone |
| `../../tests/gaps/test_lan_blocked_ble.py` | mDNS lists the phone, TCP black-holed (AP isolation / VPN), BLE in range |
