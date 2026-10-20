"""LAN situations no other probe ran: laptop VPN, two phones with the same name, a phone advertising
another protocol version, a host firewall on the laptop."""

import time


def _alive(r):
    return bool(r.lab.sh(r.laptop, "pgrep -f [/]harness/orbitd.py", check=False).strip())


def test_laptop_vpn_captures_lan_mid_session(rig, record):
    """A full-tunnel VPN without 'allow LAN' comes up on the laptop while connected, then goes down after 60 s."""
    r = rig()
    t0 = r.lab.now(r.laptop)
    r.lab.sh(r.laptop, f"ip link add vpn0 type dummy && ip link set vpn0 up && ip route add {r.phone_ip}/32 dev vpn0")
    act = r.action()  # pressed right after the VPN came up
    r.wait(lambda: r.first_state(t0, "offline") is not None, 60, what="offline")
    dev_offline = r.device()
    time.sleep(10)
    t_down = r.lab.now(r.laptop)
    r.lab.sh(r.laptop, "ip link del vpn0")
    rec = r.wait(lambda: r.first_state(t_down, "connected"), 90, interval=0.25, what="reconnect after VPN down")
    record(action_after_vpn_up=act, transitions=r.transitions(t0), discovered_while_vpn=dev_offline["discovered"],
           reconnect_after_vpn_down_s=rec, closes=[line.split("INFO ")[-1] for line in r.orbitd_log().splitlines()
                                                   if "closed" in line or "retry" in line][-6:])
    assert rec <= 10, f"reconnected {rec} s after the VPN went down"


def test_two_phones_same_name_swap(rig, record):
    """Two phones of the same model ("Pixel 8"); both die without a goodbye and come back in the other order,
    so the mDNS instance names swap ("Pixel 8" <-> "Pixel 8 (2)")."""
    r = rig(phone_args='--name "Pixel 8"')
    lab = r.lab
    p2 = lab.container("phone2", lab.nets[0], 4)
    lab.bg(p2, 'exec python /harness/phone.py --config-dir /tmp/phonecfg --name "Pixel 8" >> /tmp/phone.log 2>&1')
    r.wait(lambda: len([d for d in r.call("devices.list")["result"]]) >= 2, 20, what="second phone discovered")
    before = {d["id"][:8]: (d["name"], d["discovered"]) for d in r.call("devices.list")["result"]}
    ok_before = r.action()
    # Both killed (-9: no goodbye); B returns first and takes "Pixel 8", A gets "Pixel 8 (2)".
    r.signal_phone("KILL")
    lab.sh(p2, "pkill -KILL -f [/]harness/phone.py", check=False)
    time.sleep(1)
    lab.bg(p2, 'exec python /harness/phone.py --config-dir /tmp/phonecfg --name "Pixel 8" >> /tmp/phone.log 2>&1')
    time.sleep(4)
    t_a = lab.now(r.phone)
    lab.sh(r.phone, "rm -f /tmp/phone.ctl")
    r.start_phone(r.phone_args)
    rec = r.wait(lambda: r.first_state(t_a, "connected"), 90, interval=0.25, what="paired phone A reconnected")
    act = r.action()
    after = {d["id"][:8]: (d["name"], d["discovered"], d["state"]) for d in r.call("devices.list")["result"]}
    inst = r.stats()[-1]["devs"]
    record(before=before, action_before=ok_before, reconnect_s=rec, action_after=act, after=after,
           lan_records={k: list(v["lan"].keys()) for k, v in inst.items()})
    assert "result" in act and act["result"]["ok"], act
    assert rec <= 10, rec


def test_phone_advertises_newer_protocol_version(rig, record):
    """The phone app updates first and advertises TXT v=2 (still able to speak v1 on the wire)."""
    r = rig()
    t0 = r.lab.now(r.phone)
    r.stop_phone()
    r.start_phone("--txt-v 2")
    time.sleep(40)
    dev = r.device()
    dbg = [line for line in r.orbitd_log().splitlines() if "TXT" in line or "ignoring" in line][-2:]
    record(device_after_40s=dev, transitions=r.transitions(t0), log=dbg)
    assert dev and dev["state"] == "connected", "a paired phone that advertises v=2 is never reconnected and nothing says why"


def test_laptop_host_firewall_blocks_inbound_mdns(rig, record):
    """ufw-style laptop firewall: INPUT default DROP, only ESTABLISHED/RELATED allowed (mDNS not opened)."""

    def fw(r):
        r.lab.sh(r.laptop, "iptables -A INPUT -i lo -j ACCEPT; iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED"
                           " -j ACCEPT; iptables -P INPUT DROP")  # fmt: skip

    r = rig(pair=False, setup=fw)
    time.sleep(20)
    dev = r.device()
    status = r.call("status")["result"]
    manual = r.call("devices.connect", {"host": r.phone_ip, "port": 47820})
    record(device_after_20s=dev, status=status, manual_connect=manual)
    assert "result" in manual


def test_two_phones_same_mdns_hostname(rig, record):
    """Older Android NSD names every phone's host "Android.local"; two such phones in one home. Phone B
    comes up claiming the same host name with its own address; the paired phone A then drops (RST)."""
    r = rig(phone_args="--server Android.local.", phone_host=5)  # B (.4) sorts before A (.5)
    lab = r.lab
    p2 = lab.container("phone2", lab.nets[0], 4)
    lab.bg(p2, "exec python /harness/phone.py --config-dir /tmp/phonecfg --name 'Pixel 7' --server Android.local. "
               ">> /tmp/phone.log 2>&1")  # fmt: skip
    r.wait(lambda: len(r.call("devices.list")["result"]) >= 2, 20, what="second phone discovered")
    time.sleep(2)
    lan = {k: v["lan"] for k, v in r.stats()[-1]["devs"].items()}
    t = r.reset()
    rec = r.wait(lambda: r.reconnected_at(t), 60, interval=0.25, what="A reconnected")
    act = r.action()
    tries = [line.split("INFO ")[-1] for line in r.orbitd_log().splitlines() if "expected" in line or "retrying" in line]
    b_sessions = [e for e in lab.jsonl(p2, "/tmp/phone-events.jsonl") if e["kind"].startswith("session")]
    record(lan_records=lan, reconnect_s=rec, action=act, wrong_device_attempts=tries[-4:],
           sessions_on_phone_b=len(b_sessions), phone_b_events=b_sessions[:4])
    a_addrs = [a for recs in lan[r.id8].values() for a in recs]
    assert all(h == r.phone_ip for h, _ in a_addrs), f"phone A's record lists another phone's address: {a_addrs}"
    assert rec <= 5, rec
