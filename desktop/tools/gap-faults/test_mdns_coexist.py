"""orbitd's python-zeroconf next to other mDNS stacks on the laptop (avahi on every Linux desktop)."""

import time

AVAHI = (
    "sed -i 's/^#*enable-dbus=.*/enable-dbus=no/' /etc/avahi/avahi-daemon.conf; "
    "avahi-daemon --no-drop-root --no-chroot -D; sleep 1"
)

EXCLUSIVE_5353 = """cat > /tmp/hog.py <<'EOF'
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 5353))  # no SO_REUSEADDR / SO_REUSEPORT
print("holding 5353", flush=True)
time.sleep(10**6)
EOF
nohup python /tmp/hog.py > /tmp/hog.log 2>&1 &
sleep 0.5"""


def _discovered_within(r, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if r.device():
            return round(time.time() - t0, 1)
        time.sleep(0.5)
    return None


def test_avahi_running_on_laptop(rig, record):
    """Typical Linux desktop: avahi-daemon owns 5353 before orbitd starts."""
    r = rig(pair=False, setup=lambda r: r.lab.sh(r.laptop, AVAHI))
    found = _discovered_within(r, 20)
    avahi_view = r.lab.sh(r.laptop, "ss -ulpn | grep 5353", check=False)
    record(discovered_after_s=found, avahi_browse=avahi_view.strip().splitlines())
    assert found is not None
    r.pair()
    t = r.reset()
    rec = r.wait_reconnect(t, 20)
    record(reconnect_after_rst_s=rec)


def test_port_5353_held_exclusively(rig, record):
    """Another program bound UDP 5353 without SO_REUSEADDR/SO_REUSEPORT before orbitd started."""
    r = rig(pair=False, setup=lambda r: r.lab.sh(r.laptop, EXCLUSIVE_5353))
    time.sleep(3)
    alive = r.lab.sh(r.laptop, "pgrep -f [/]harness/orbitd.py || echo DEAD", check=False).strip()
    log = r.orbitd_log().strip().splitlines()
    status = r.lab.sh(r.laptop, "python /harness/call.py /tmp/conn-gaps/o.sock status 2>&1 | tail -1", check=False).strip()
    record(orbitd_pid_or_dead=alive, orbitd_log_tail=log[-3:], status_call=status)
    assert alive != "DEAD", "orbitd exits when UDP 5353 is taken; BLE and manual host:port are lost with it"
    manual = r.call("devices.connect", {"host": r.phone_ip, "port": 47820})
    record(manual_connect=manual)
    assert "result" in manual, manual
