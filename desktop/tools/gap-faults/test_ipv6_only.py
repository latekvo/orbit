"""IPv6-only Wi-Fi (no IPv4 on the link at all; NAT64/464XLAT networks, some enterprise SSIDs)."""

import json
import time


def v6_only(rig):
    """Strip IPv4 from both namespaces before anything starts; the phone advertises its ULA + link-local."""
    for ctr in (rig.laptop, rig.phone):
        rig.lab.sh(ctr, "ip -4 addr flush dev eth0; ip -4 route flush table main || true")
    out = rig.lab.sh(rig.phone, "ip -6 -o addr show dev eth0")
    ula = next(w.split("/")[0] for line in out.splitlines() for w in line.split() if w.startswith("fd32"))
    ll = next(w.split("/")[0] for line in out.splitlines() for w in line.split() if w.startswith("fe80"))
    rig.phone_v6, rig.phone_ll = ula, ll
    rig.phone_args = f"--host :: --advertise {ula},{ll}"


def test_ipv6_only_network_discovery(rig, record):
    r = rig(ipv6=True, pair=False, setup=v6_only)
    assert not r.lab.sh(r.laptop, "ip -4 -o addr show scope global").strip()
    # Control: a zeroconf that also listens on IPv6 sees the phone from the laptop's namespace.
    control = json.loads(r.lab.sh(r.laptop, "python /harness/browse.py 6 all").strip().splitlines()[-1])
    control_v4 = json.loads(r.lab.sh(r.laptop, "python /harness/browse.py 6 v4").strip().splitlines()[-1])
    t0 = time.time()
    seen = None
    while time.time() - t0 < 30:
        seen = r.device()
        if seen:
            break
        time.sleep(1)
    manual = r.call("devices.connect", {"host": r.phone_v6, "port": 47820})
    record(control_all=control, control_v4_only=control_v4, orbitd_device_after_30s=seen, manual_connect_v6=manual,
           zc_log=[line for line in r.orbitd_log().splitlines() if "zeroconf" in line.lower() or "mdns" in line.lower()][:5])
    assert control, "control browser (IPv4+IPv6) should see the phone"
    assert "result" in manual, manual
    assert seen is not None, "orbitd never discovered the phone on an IPv6-only network"
