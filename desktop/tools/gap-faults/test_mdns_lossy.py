"""Wi-Fi multicast is sent at the basic rate with no ACK/retry, and phones in power save answer late:
mDNS from the phone is lossy or delayed while unicast TCP is fine."""

import time

import pytest


def _netem_mdns(r, spec):
    # Only the phone's mDNS egress (UDP 5353) is impaired.
    r.lab.sh(r.phone, "tc qdisc add dev eth0 root handle 1: prio && "
                      f"tc qdisc add dev eth0 parent 1:3 handle 30: netem {spec} && "
                      "tc filter add dev eth0 protocol ip parent 1: prio 1 u32 match ip dport 5353 0xffff flowid 1:3")  # fmt: skip


@pytest.mark.parametrize("spec", ["loss 50%", "loss 80%", "delay 3500ms", "delay 6000ms"])
def test_discovery_with_lossy_or_late_mdns(rig, record, spec):
    r = rig(pair=False, setup=lambda r: _netem_mdns(r, spec))
    t0 = time.time()
    found = None
    while time.time() - t0 < 90:
        if r.device():
            found = round(time.time() - t0, 1)
            break
        time.sleep(0.5)
    record(spec=spec, discovered_after_s=found)
    assert found is not None and found <= 30, f"{spec}: discovered after {found} s"


@pytest.mark.slow
@pytest.mark.parametrize("trial", range(6))
@pytest.mark.parametrize("loss", ["60%", "80%"])
def test_discovery_with_lossy_mdns_distribution(rig, record, loss, trial):
    """Repeated trials: python-zeroconf's browser doubles its query interval (1, 2, 4 ... s), so early losses
    push the next chance minutes out. A fresh browser started alongside is the control."""
    r = rig(pair=False, setup=lambda r: _netem_mdns(r, f"loss {loss}"))
    t0 = time.time()
    found = None
    while time.time() - t0 < 150:
        if r.device():
            found = round(time.time() - t0, 1)
            break
        time.sleep(0.5)
    record(loss=loss, trial=trial, discovered_after_s=found)
    assert found is not None and found <= 30, f"loss {loss}: discovered after {found} s"
