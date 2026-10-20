"""LAN fault scenarios against real orbitd + orbit-fakephone processes in Docker network namespaces.

    cd desktop && uv run --with pytest-xdist pytest tools/lan-faults -n 6 -v -s

Every test records what a user would observe (results/<test>.json) and asserts what
spec/LOCAL_API.md promises or a user can reasonably expect. A failing test is a finding.
"""

import time

import pytest

from orbit.daemon.core import RECONNECT_MAX_DELAY, RECONNECT_SOON_FOR

MEDIA = "media:com.example.music"
DEAD_AFTER = 30.0  # spec/PROTOCOL.md 5: close after 30 s without receiving anything
GRACE = 15.0  # spec/LOCAL_API.md: "reconnecting" for up to 15 s
SLACK = 3.0
# What a user can reasonably expect once the network is back: the phone shows up again within a
# few seconds (no spec number exists; the connector's backoff grows to RECONNECT_MAX_DELAY).
RECOVER_WITHIN = 10.0
ACTION_SILENCE = 5.0  # spec/PROTOCOL.md: closed when nothing arrives within 5 s of an action


def honest(res, applied):
    """A failed action says it may have been applied, or that it was not sent and indeed was not."""
    err = res.get("error") or {}
    if err.get("code") == "not_connected":
        return applied == 0
    return err.get("code") in ("timeout", "failed") and "may" in err.get("message", "")


def wait_connected(rig, timeout):
    t0 = time.time()
    rig.wait(lambda: rig.state() == "connected", timeout, interval=0.25, what="reconnected")
    return round(time.time() - t0, 2)


# ---- silent blackhole ------------------------------------------------------------------------------


def test_blackhole_idle_detect_and_recover(rig, record):
    """DROP every TCP packet between phone and laptop, then heal after 60 s."""
    r = rig()
    t_f = r.blackhole()
    r.wait(lambda: r.state() == "offline", DEAD_AFTER + GRACE + 10, interval=0.5, what="offline")
    acts_offline = len(r.activities())
    time.sleep(max(0, t_f + 60 - time.time()))
    t_h = r.blackhole(False)
    recover = wait_connected(r, 90)
    record(
        transitions=r.transitions(t_f),
        stale_at=r.first(t_f, lambda x: x["ev"] == "activities" and x["stale"] > 0),
        removed_at=r.first(t_f, lambda x: x["ev"] == "activities" and x["n"] == 0),
        activities_when_offline=acts_offline,
        recover_after_heal_s=recover,
        heal_at=round(t_h - t_f, 1),
    )
    det = r.first_state(t_f, "reconnecting")
    assert det is not None and det <= DEAD_AFTER + SLACK
    assert r.first_state(t_f, "offline") - det == pytest.approx(GRACE, abs=SLACK)
    assert acts_offline == 0
    assert recover <= RECOVER_WITHIN


@pytest.mark.slow
def test_blackhole_5min_recover(rig, record):
    """Wi-Fi gone for 5 minutes, then back: how long until the phone is connected again?"""
    r = rig()
    t_f = r.blackhole()
    time.sleep(300)
    t_h = r.blackhole(False)
    recover = wait_connected(r, 120)
    attempts = r.orbitd_log().count("connecting to Fake Phone")
    record(transitions=r.transitions(t_f), recover_after_heal_s=recover, failed_attempts=attempts,
           heal_at=round(t_h - t_f, 1))  # fmt: skip
    assert recover <= RECOVER_WITHIN


def test_blackhole_action_in_flight(rig, record):
    """Action sent into a silent blackhole: bounded error, never applied; and once healed, works again."""
    r = rig()
    t_f = r.blackhole()
    res = r.action(action="media.next")
    # Heal while the dead session is still open on both sides (before 30 s): TCP retransmits the action.
    time.sleep(max(0, t_f + 14 - time.time()))
    r.blackhole(False)
    time.sleep(4)
    applied = r.phone_actions(t_f)
    res2 = r.action(action="media.next")
    record(first=res, applied_after_heal=[(round(a["t"] - t_f, 1), a["action"]) for a in applied], second=res2,
           transitions=r.transitions(t_f))  # fmt: skip
    assert res["dt"] <= 12.5 and honest(res, len(applied))
    assert res2.get("result", {}).get("ok") is True


def test_blackhole_mid_frame(rig, record):
    """Silent blackhole while a ~900 KB art frame is half-way through a 1 Mbit/s link."""
    r = rig()
    r.lab.sh(r.phone, "tc qdisc add dev eth0 root tbf rate 1mbit burst 32kbit latency 400ms")
    r.phone_ctl({"cmd": "big", "size": 650_000})  # base64 -> ~870 KB frame, ~7 s at 1 Mbit/s
    time.sleep(2.5)
    t_f = r.blackhole()
    r.wait(lambda: r.state() != "connected", 40, interval=0.25, what="drop")
    det = r.first_state(t_f, "reconnecting")
    log = r.orbitd_log()
    cause = [ln for ln in log.splitlines() if "session_closed" in ln][-1:]
    record(detect_s=det, cause=cause)
    # Mid-frame stall: 10 s after the last byte (PROTOCOL.md keepalive, stall_after).
    assert det is not None and det <= 10 + SLACK
    assert "mid-frame" in cause[0]


# ---- RST / half-open / asymmetric ---------------------------------------------------------------------


def test_rst_mid_session(rig, record):
    """A middlebox resets the connection (forged RST phone -> laptop): immediate detection and reconnect."""
    r = rig()
    time.sleep(31)  # a stable session: the connector reconnects at once
    t_f = r.lab.now(r.phone)
    r.lab.sh(r.phone, f"python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}")
    recover = r.wait_reconnect(t_f, 20)
    res = r.action(action="media.next")
    record(transitions=r.transitions(t_f), detect_s=r.first_state(t_f, "reconnecting"), recover_s=recover, action=res)
    assert r.first_state(t_f, "reconnecting") <= 1.0
    assert recover <= 3.0
    assert res["result"]["ok"] is True


def test_rst_reject_new_connections(rig, record):
    """Phone sends RST to every packet from the laptop (REJECT --reject-with tcp-reset) for 20 s."""
    r = rig()
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, f"-A INPUT -s {r.laptop_ip} -p tcp -j REJECT --reject-with tcp-reset")
    r.wait(lambda: r.state() != "connected", 15, what="drop")
    time.sleep(max(0, t_f + 20 - time.time()))
    t_h = r.lab.now(r.phone)
    r.ipt(r.phone, f"-D INPUT -s {r.laptop_ip} -p tcp -j REJECT --reject-with tcp-reset")
    recover = wait_connected(r, 90)
    record(transitions=r.transitions(t_f), detect_s=r.first_state(t_f, "reconnecting"), recover_after_heal_s=recover,
           heal_at=round(t_h - t_f, 1))  # fmt: skip
    # The laptop pings within 10 s of idle; the phone ticks every second, so the laptop is idle... the RST
    # arrives on the laptop's next ping (<= 10 s).
    assert r.first_state(t_f, "reconnecting") <= 10 + SLACK
    assert recover <= RECOVER_WITHIN


def test_half_open_phone_vanished(rig, record):
    """The phone leaves the network without a FIN (interface gone); the laptop keeps sending."""
    r = rig()
    net = r.lab.nets[0]
    t_f = r.lab.now(r.laptop)
    r.lab.sh(r.laptop, "true")
    from harness import run

    run("docker", "network", "disconnect", net.name, r.phone)
    res = r.action(action="media.next")
    r.wait(lambda: r.state() == "offline", DEAD_AFTER + GRACE + 10, interval=0.5, what="offline")
    t_back = r.lab.now(r.laptop)
    run("docker", "network", "connect", "--ip", r.phone_ip, net.name, r.phone)
    recover = wait_connected(r, 120)
    record(transitions=r.transitions(t_f), action=res, recover_after_back_s=recover, back_at=round(t_back - t_f, 1),
           applied=len(r.phone_actions(t_f)))  # fmt: skip
    assert r.first_state(t_f, "reconnecting") <= ACTION_SILENCE + SLACK
    assert res["dt"] <= 12.5 and honest(res, len(r.phone_actions(t_f)))
    assert recover <= RECOVER_WITHIN


@pytest.mark.parametrize("direction", ["laptop_to_phone", "phone_to_laptop"])
def test_asymmetric_loss(rig, record, direction):
    """100 % loss in one direction only, healed after 50 s."""
    r = rig()
    rule = f"INPUT -s {r.laptop_ip} -p tcp -j DROP" if direction == "laptop_to_phone" else f"OUTPUT -d {r.laptop_ip} -p tcp -j DROP"
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, "-A " + rule)
    res = r.action(action="media.next")
    r.wait(lambda: r.state() != "connected", DEAD_AFTER + 10, interval=0.5, what="drop")
    time.sleep(max(0, t_f + 50 - time.time()))
    t_h = r.lab.now(r.phone)
    r.ipt(r.phone, "-D " + rule)
    recover = wait_connected(r, 90)
    applied = r.phone_actions(t_f)
    record(transitions=r.transitions(t_f), action=res, applied=len(applied), recover_after_heal_s=recover,
           heal_at=round(t_h - t_f, 1), causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines()
                                                if "session_closed" in ln])  # fmt: skip
    assert honest(res, len(applied))
    assert r.first_state(t_f, "reconnecting") <= ACTION_SILENCE + SLACK
    assert recover <= RECOVER_WITHIN


# ---- latency / loss ---------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    "delay_ms,loss",
    [
        (1000, 0),
        (1500, 0),
        (3000, 0),
        (0, 20),
        (0, 30),
        # Two losses in a row cost TCP 9-21 s at this RTT, past the liveness deadlines (spec 5).
        pytest.param(1000, 20, marks=pytest.mark.xfail(reason="beyond the liveness deadlines", strict=False)),
    ],
)
def test_latency_loss(rig, record, delay_ms, loss):
    """netem on both sides (RTT = 2 x delay): keepalives must not misfire; actions and reconnects must work."""
    r = rig()
    spec = (f"delay {delay_ms}ms " if delay_ms else "") + (f"loss {loss}% " if loss else "")
    for c in (r.laptop, r.phone):
        r.lab.sh(c, f"tc qdisc add dev eth0 root netem {spec} limit 100000")
    t0 = r.lab.now(r.laptop)
    actions = []
    for _ in range(6):
        time.sleep(15)
        a = r.action(action="media.next")
        actions.append((a["dt"], a.get("result", a.get("error"))))
    drops = [x for x in r.transitions(t0) if x[1] != "connected"]
    # A fresh connection under the same conditions (the user clicks Disconnect, then Connect). It
    # takes three round trips (TCP, commit/hello, hello/ready): at an RTT of 6 s that is past
    # devices.connect's 12 s answer (spec/LOCAL_API.md), so then it must connect within four.
    r.call("devices.disconnect", {"id": r.phone_id})
    t_c = time.time()
    c = r.call("devices.connect", {"id": r.phone_id})
    r.wait(lambda: r.state() == "connected", 30, interval=0.2, what="connected")
    connected_s = round(time.time() - t_c, 1)
    record(netem=spec, drops=drops, actions=actions, reconnect=c, connected_s=connected_s,
           applied=len(r.phone_actions(t0)),
           causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines() if "session_closed" in ln])  # fmt: skip
    assert not drops, "keepalive misfired"
    assert all(isinstance(res, dict) and res.get("ok") for _, res in actions)
    rtt = 2 * delay_ms / 1000
    assert "result" in c or (3 * rtt > 12 and connected_s <= 4 * rtt), c


@pytest.mark.parametrize("netem", ["duplicate 25%", "delay 30ms 20ms reorder 25% 50%", "corrupt 1%"])
def test_netem_dup_reorder_corrupt(rig, record, netem):
    r = rig()
    for c in (r.laptop, r.phone):
        r.lab.sh(c, f"tc qdisc add dev eth0 root netem {netem}")
    t0 = r.lab.now(r.laptop)
    r.phone_ctl({"cmd": "big", "size": 200_000})
    acts = []
    for _ in range(4):
        time.sleep(10)
        a = r.action(action="media.next")
        acts.append((a["dt"], a.get("result", a.get("error"))))
    drops = [x for x in r.transitions(t0) if x[1] != "connected"]
    record(netem=netem, drops=drops, actions=acts, applied=len(r.phone_actions(t0)))
    assert not drops
    assert len(r.phone_actions(t0)) == 4  # never double-applied
    assert all(res.get("ok") for _, res in acts)


def test_rate_limited_art_burst_action(rig, record):
    """Album-art burst over a slow link (256 kbit/s): an action sent during the burst."""
    r = rig()
    r.lab.sh(r.phone, "tc qdisc add dev eth0 root tbf rate 256kbit burst 16kbit latency 2s")
    t0 = r.lab.now(r.laptop)
    r.phone_ctl({"cmd": "burst", "n": 12, "size": 40_000})  # ~640 KB on the wire, ~20 s
    time.sleep(1)
    a = r.action(action="media.next")
    applied = r.wait(lambda: r.phone_actions(t0), 40, what="applied")
    drops = [x for x in r.transitions(t0) if x[1] != "connected"]
    record(action=a, applied_at=round(applied[0]["t"] - t0, 1), drops=drops)
    assert not drops
    assert a.get("result", {}).get("ok") is True, a


@pytest.mark.parametrize("mtu", [1280, 576])
def test_mtu_small(rig, record, mtu):
    """Both ends on a small MTU: a large art frame still arrives."""
    r = rig()
    for c in (r.laptop, r.phone):
        r.lab.sh(c, f"ip link set dev eth0 mtu {mtu}")
    t0 = r.lab.now(r.laptop)
    key = r.phone_ctl({"cmd": "big", "size": 300_000})["key"]
    got = r.wait(lambda: "result" in r.call("art.get", {"device": r.phone_id, "key": key}), 20, what="art")
    drops = [x for x in r.transitions(t0) if x[1] != "connected"]
    record(mtu=mtu, art=bool(got), drops=drops)
    assert not drops


def test_pmtu_blackhole(rig, record):
    """Packets > 1000 bytes phone -> laptop silently dropped (PMTU blackhole, e.g. VPN/tunnel)."""
    r = rig()
    t0 = r.lab.now(r.laptop)
    r.ipt(r.phone, f"-A OUTPUT -d {r.laptop_ip} -p tcp -m length --length 1000:65535 -j DROP")
    r.phone_ctl({"cmd": "big", "size": 20_000})
    time.sleep(100)
    tr = r.transitions(t0)
    record(transitions=tr, causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines() if "session_closed" in ln])
    # Not fixable by the app; recorded to document the symptom (flapping every ~30-40 s).


# ---- phone address / port / mDNS changes -------------------------------------------------------------------


def _move_phone(r, host):
    from harness import run

    net = r.lab.nets[0]
    run("docker", "network", "disconnect", net.name, r.phone)
    run("docker", "network", "connect", "--ip", net.ip(host), net.name, r.phone)
    r.phone_ip = net.ip(host)
    return r.lab.now(r.phone)


def test_phone_ip_change_reannounced(rig, record):
    """DHCP renew / roam: the phone gets a new IP and re-announces it via mDNS at once."""
    r = rig()
    t_f = _move_phone(r, 33)
    r.phone_ctl({"cmd": "advertise", "addresses": []})
    recover = wait_connected(r, 90) if r.state() != "connected" else None
    r.wait(lambda: (d := r.device()) and d["state"] == "connected" and d["address"].startswith(r.phone_ip), 90,
           what="connected to new address")  # fmt: skip
    t_ok = time.time()
    record(transitions=r.transitions(t_f), new_address_connected_after_s=round(t_ok - t_f, 1),
           causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines() if "session_closed" in ln])  # fmt: skip
    assert t_ok - t_f <= RECOVER_WITHIN


def test_phone_ip_change_announcement_lost(rig, record):
    """The phone re-announces its new IP but the multicast announcements are lost (common on Wi-Fi)."""
    r = rig()
    t_f = _move_phone(r, 34)
    r.ipt(r.phone, "-A OUTPUT -p udp --dport 5353 -j DROP")
    r.phone_ctl({"cmd": "advertise", "addresses": []})
    time.sleep(5)
    r.ipt(r.phone, "-D OUTPUT -p udp --dport 5353 -j DROP")
    ok = True
    try:
        r.wait(lambda: (d := r.device()) and d["state"] == "connected" and d["address"].startswith(r.phone_ip), 180,
               interval=1, what="connected to new address")  # fmt: skip
    except AssertionError:
        ok = False
    lan = r.stats()[-1]["devs"][r.id8]["lan"]
    record(transitions=r.transitions(t_f), recovered=ok, after_s=round(time.time() - t_f, 1), daemon_lan_record=lan)
    assert ok, "never found the phone at its new address although its responder answers queries with it"


def test_phone_ip_change_no_reannounce(rig, record):
    """The phone changes IP and its mDNS responder keeps advertising the old one: recorded only."""
    r = rig()
    t_f = _move_phone(r, 35)
    time.sleep(90)
    record(transitions=r.transitions(t_f), state=r.state(), lan=r.stats()[-1]["devs"][r.id8]["lan"])


def test_unreachable_address_first(lab, record):
    """mDNS lists four dead IPv4 addresses that sort before the real one (e.g. stale/other-interface)."""
    from harness import Rig

    lb = lab()
    p = lb.nets[0].prefix
    dead = [f"{p}.{h}" for h in (200, 201, 202, 203)]
    r = Rig(lb, phone_args=f"--advertise {','.join(dead + [f'{p}.30'])}", phone_host=30)
    t0 = time.time()
    r.wait(lambda: r.device() is not None, 15, what="discovered")
    c = r.call("devices.connect", {"id": r.phone_id})
    record(connect=c, addresses=r.stats()[-1]["devs"][r.id8]["lan"])
    assert "result" in c, "devices.connect failed although one advertised address works"


def test_unreachable_address_first_reconnect(lab, record):
    """Same, paired: how long does an automatic reconnect take after a reset?"""
    from harness import Rig

    lb = lab()
    p = lb.nets[0].prefix
    dead = [f"{p}.{h}" for h in (200, 201)]
    r = Rig(lb, phone_args=f"--advertise {','.join(dead + [f'{p}.30'])}", phone_host=30)
    t0 = time.time()
    r.pair()
    c = round(time.time() - t0, 1)
    time.sleep(31)
    t_f = r.lab.now(r.phone)
    r.lab.sh(r.phone, f"python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}")
    rec = r.wait_reconnect(t_f, 60)
    res = r.action(action="media.next")
    record(pair_s=c, reconnect_s=rec, action=res, transitions=r.transitions(t_f))
    assert rec <= RECOVER_WITHIN


def test_ipv6_and_link_local(lab, record):
    """Phone advertises IPv4 + ULA IPv6 + fe80 link-local; IPv4 TCP is then blocked: does IPv6 take over?"""
    from harness import Rig

    lb = lab(ipv6=True)
    r = Rig(lb)
    r.pair()
    v6 = r.lab.sh(r.phone, "ip -6 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1").split()
    r.stop_phone()
    r.start_phone("--host '' --advertise " + ",".join([r.phone_ip] + v6))
    r.wait(lambda: r.state() == "connected", 30, what="connected")
    lan = r.stats()[-1]["devs"][r.id8]["lan"]
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, f"-A INPUT -s {r.laptop_ip} -p tcp -j REJECT --reject-with tcp-reset")
    time.sleep(1)
    r.lab.sh(r.phone, f"python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}", check=False, timeout=40)
    ok = True
    try:
        r.wait(lambda: (d := r.device()) and d["state"] == "connected" and ":" in d["address"].split("]")[0], 40,
               what="connected via IPv6")  # fmt: skip
    except AssertionError:
        ok = False
    log = [ln for ln in r.orbitd_log().splitlines() if "failed" in ln][-3:]
    record(advertised=[r.phone_ip] + v6, daemon_addresses=lan, v6_took_over=ok, address=(r.device() or {}).get("address"),
           log=log, transitions=r.transitions(t_f))  # fmt: skip
    assert ok


@pytest.mark.parametrize("graceful", [False, True])
def test_phone_restart_new_port(rig, record, graceful):
    """Phone app restarts on a different port (47820 taken): killed (no goodbye) or graceful (goodbye)."""
    r = rig()
    time.sleep(31)
    t_f = r.lab.now(r.phone)
    r.stop_phone("TERM" if graceful else "KILL")
    r.lab.bg(r.phone, "exec python -c 'import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((\"0.0.0.0\", 47820)); s.listen(); time.sleep(10**6)'")
    time.sleep(1)
    r.start_phone()
    port = r.phone_status()["port"]
    r.wait_reconnect(t_f, 90)
    d = r.device()
    record(new_port=port, reconnect_s=round(time.time() - t_f, 1), address=d["address"], transitions=r.transitions(t_f))
    assert port != 47820
    assert time.time() - t_f <= RECOVER_WITHIN + 2


def test_mdns_goodbye_then_back(rig, record):
    """Phone app stops (mDNS goodbye), comes back 20 s later on the same port."""
    r = rig()
    t_f = r.lab.now(r.phone)
    r.stop_phone("TERM")
    r.wait(lambda: (d := r.device()) and d["discovered"] == [] and d["state"] != "connected", 20, what="goodbye")
    gone = r.device()
    time.sleep(max(0, t_f + 20 - time.time()))
    t_b = r.lab.now(r.phone)
    r.start_phone()
    wait_connected(r, 60)
    record(after_goodbye=gone, back_after_s=round(time.time() - t_b, 1), transitions=r.transitions(t_f))
    assert time.time() - t_b <= RECOVER_WITHIN


def test_mdns_stale_record_phone_killed(rig, record):
    """Phone killed without goodbye and never returns: what does the device list say 3 minutes later?"""
    r = rig()
    t_f = r.lab.now(r.phone)
    r.stop_phone("KILL")
    time.sleep(180)
    d = r.device()
    record(device=d, transitions=r.transitions(t_f), connect_attempts=r.orbitd_log().count("connecting to Fake Phone"))
    # Recorded: "discovered": ["lan"] for a phone that is gone (PTR TTL 75 min).


@pytest.mark.parametrize("mode", ["DROP", "REJECT"])
def test_client_isolation(lab, record, mode):
    """mDNS works but TCP between clients is blocked (guest Wi-Fi client isolation): unpaired connect."""
    from harness import Rig

    r = Rig(lab())
    r.wait(lambda: r.device() is not None, 15, what="discovered")
    target = "DROP" if mode == "DROP" else "REJECT --reject-with tcp-reset"
    r.ipt(r.phone, f"-A INPUT -s {r.laptop_ip} -p tcp -j {target}")
    c = r.call("devices.connect", {"id": r.phone_id})
    record(connect=c)
    assert c["dt"] <= 12.5
    assert "error" in c


@pytest.mark.parametrize("mode", ["DROP", "REJECT"])
def test_client_isolation_paired(rig, record, mode):
    """Paired phone, then TCP isolated (moved to a guest network), mDNS still visible: state + message after 90 s."""
    r = rig()
    target = "DROP" if mode == "DROP" else "REJECT --reject-with tcp-reset"
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, f"-A INPUT -s {r.laptop_ip} -p tcp -j {target}")
    if mode == "REJECT":
        r.lab.sh(r.phone, f"python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}")
    time.sleep(90)
    c = r.call("devices.connect", {"id": r.phone_id})
    a = r.action(action="media.next")
    attempts = [ln.split("failed: ")[-1] for ln in r.orbitd_log().splitlines() if "connecting to Fake Phone" in ln]
    record(transitions=r.transitions(t_f), connect=c, action=a, attempts=len(attempts), last_attempt=attempts[-1:])
    assert c["dt"] <= 12.5


def test_multicast_blocked_daemon_restart(rig, record):
    """Paired via mDNS; the network then filters multicast (or the phone sleeps through mDNS queries) and
    the laptop restarts orbitd. TCP to the phone still works: does it reconnect?"""
    r = rig()
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, "-A OUTPUT -p udp --dport 5353 -j DROP")
    r.ipt(r.phone, "-A INPUT -p udp --dport 5353 -j DROP")
    r.signal_orbitd("TERM")
    time.sleep(2)
    r.start_orbitd()
    ok = True
    try:
        r.wait(lambda: r.state() == "connected", 60, what="reconnected")
    except AssertionError:
        ok = False
    record(reconnected=ok, device=r.device(), transitions=r.transitions(t_f))
    assert ok, "a paired, TCP-reachable phone is never reconnected without mDNS"


# ---- laptop sleep / phone freeze ------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [45, pytest.param(300, marks=pytest.mark.slow)])
def test_laptop_sigstop(rig, record, seconds):
    """orbitd SIGSTOPped (kernel and clocks keep running), then SIGCONT."""
    r = rig()
    t_f = r.lab.now(r.laptop)
    t_w = r.sleep_laptop(seconds, network_down=False, monotonic_frozen=False)
    a = r.action(action="media.next")
    back = r.wait(lambda: r.state() == "connected" and r.first_state(t_w, "connected") is not None, 60, what="back")
    rec = r.first(t_w, lambda x: x["ev"] == "devices" and x["devs"].get(r.id8, {}).get("state") == "connected"
                  and x["t"] > t_w)  # fmt: skip
    record(transitions=r.transitions(t_w), action_after_wake=a, connected_after_wake_s=rec,
           applied=len(r.phone_actions(t_w)), stats_end=r.stats()[-1])  # fmt: skip
    assert rec is not None and rec <= RECOVER_WITHIN
    assert a.get("result", {}).get("ok") is True, a


@pytest.mark.parametrize("seconds", [45, pytest.param(300, marks=pytest.mark.slow)])
def test_laptop_suspend(rig, record, seconds):
    """Realistic suspend: orbitd stopped, the laptop's network down, monotonic clock frozen; then wake."""
    r = rig(faketime=True)
    t_w = r.sleep_laptop(seconds, network_down=True, monotonic_frozen=True)
    # The user opens the lid and immediately presses "next".
    a = r.action(action="media.next")
    applied = r.phone_actions(t_w)
    fresh = r.wait_reconnect(t_w, 90)
    still_connected_for = next((t for t, st in r.transitions(t_w) if st != "connected"), None)
    rows = [x for x in r.watch() if x["ev"] == "activities" and x["t"] >= t_w]
    record(transitions=r.transitions(t_w), action_after_wake=a, applied=len(applied),
           shown_connected_after_wake_s=still_connected_for, fresh_session_after_s=fresh,
           stale_marked=any(x["stale"] for x in rows),
           causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines() if "session_closed" in ln])  # fmt: skip
    # On wake the session is long dead on the phone; "connected" with live-looking cards is a lie.
    assert still_connected_for is not None and still_connected_for <= 3.0
    assert fresh <= RECOVER_WITHIN
    # An action pressed right after wake: either applied, or an honest error (not "may have been applied"
    # for an action the phone can never have received).
    assert a.get("result", {}).get("ok") is True or "may have been applied" not in a["error"]["message"], a


@pytest.mark.parametrize("seconds", [20, 90])
def test_phone_frozen(rig, record, seconds):
    """Phone process SIGSTOPped (kernel still ACKs): action during the freeze, then SIGCONT."""
    r = rig()
    t_f = r.lab.now(r.phone)
    r.signal_phone("STOP")
    time.sleep(3)
    a = r.action(action="media.next")
    time.sleep(max(0, t_f + seconds - time.time()))
    t_c = r.lab.now(r.phone)
    r.signal_phone("CONT")
    time.sleep(5)
    if seconds > DEAD_AFTER:
        r.wait(lambda: r.state() == "connected", 90, what="back")
    t_ok = time.time()
    a2 = r.action(action="media.next")
    applied = [(round(x["t"] - t_f, 1), x["session"]) for x in r.phone_actions(t_f)]
    record(transitions=r.transitions(t_f), action_during=a, action_after=a2, applied=applied,
           connected_after_cont_s=round(t_ok - t_c, 1))  # fmt: skip
    # The action draws no answer: the link is dropped 5 s later and is back once the phone thaws.
    assert a["dt"] <= 12.5 and honest(a, len([x for x in applied if x[0] < t_ok - t_f]))
    assert r.first_state(t_f, "reconnecting") <= 3 + ACTION_SILENCE + SLACK
    assert t_ok - t_c <= RECOVER_WITHIN
    assert a2.get("result", {}).get("ok") is True


# ---- flapping / leaks -------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["rst", "blackhole"])
def test_flapping_leaks(rig, record, kind):
    """Connection reset every 4 s (rst) or blackholed 6 s of every 12 s (blackhole) for 4 minutes."""
    r = rig()
    time.sleep(3)
    s0 = r.stats()[-1]
    t0 = r.lab.now(r.phone)
    end = time.time() + 240
    n = 0
    while time.time() < end:
        if kind == "rst":
            r.lab.sh(r.phone, f"timeout 5 python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}", check=False)
            time.sleep(4)
        else:
            r.blackhole()
            time.sleep(6)
            r.blackhole(False)
            time.sleep(6)
        n += 1
    time.sleep(40)
    rows = r.stats()
    s1 = rows[-1]
    peak = {k: max(x[k] for x in rows if x["t"] >= t0) for k in ("fds", "tasks", "rss_kb", "sessions")}
    sessions_closed = r.orbitd_log().count("session_closed")
    phone_sessions = len(r.phone_status()["sessions"])
    record(kind=kind, cycles=n, start={k: s0[k] for k in ("fds", "tasks", "rss_kb", "sessions", "bg")},
           end={k: s1[k] for k in ("fds", "tasks", "rss_kb", "sessions", "bg")}, peak=peak,
           sessions_closed=sessions_closed, phone_sessions_end=phone_sessions, state_end=r.state())  # fmt: skip
    assert s1["fds"] <= s0["fds"] + 2
    assert s1["tasks"] <= s0["tasks"] + 2
    assert s1["sessions"] == 1 and phone_sessions == 1
    assert r.state() == "connected"


# ---- laptop interfaces -------------------------------------------------------------------------------------


def test_two_laptop_interfaces(lab, record):
    """Laptop on two networks (phone on one); then the other interface goes away and comes back."""
    from harness import Rig, run

    lb = lab(2)
    r = Rig(lb, laptop_nets=[(lb.nets[1], 2)])
    r.pair()
    t0 = r.lab.now(r.laptop)
    run("docker", "network", "disconnect", lb.nets[1].name, r.laptop)
    time.sleep(8)
    run("docker", "network", "connect", "--ip", lb.nets[1].ip(2), lb.nets[1].name, r.laptop)
    time.sleep(8)
    alive = "result" in r.call("status")
    a = r.action(action="media.next")
    errs = [ln for ln in r.orbitd_log().splitlines() if "ERROR" in ln or "Traceback" in ln or "WARNING" in ln]
    record(alive=alive, transitions=r.transitions(t0), action=a, log_errors=errs[:10])
    assert alive and a.get("result", {}).get("ok") is True


def test_laptop_network_comes_up_after_orbitd(lab, record):
    """orbitd starts before Wi-Fi is up (login item at boot), then the laptop joins the phone's network."""
    from harness import Rig, run

    lb = lab(2)
    r = Rig(lb, laptop_nets=[(lb.nets[1], 2)])
    r.pair()
    # Restart orbitd while the laptop is only on the other network.
    run("docker", "network", "disconnect", lb.nets[0].name, r.laptop)
    r.signal_orbitd("TERM")
    time.sleep(1)
    r.start_orbitd()
    time.sleep(3)
    t_up = r.lab.now(r.laptop)
    run("docker", "network", "connect", "--ip", lb.nets[0].ip(2), lb.nets[0].name, r.laptop)
    # Restart the phone on another port so only fresh mDNS can find it.
    r.stop_phone("TERM")
    r.start_phone("--port 47999")
    ok = True
    try:
        r.wait(lambda: r.state() == "connected", 60, what="connected")
    except AssertionError:
        ok = False
    control = r.lab.sh(r.laptop, "python /harness/browse.py 4")
    record(connected=ok, after_s=round(time.time() - t_up, 1), device=r.device(), zc=r.stats()[-1]["zc_ifaces"],
           fresh_zeroconf_in_same_netns_sees=control,
           errors=[ln for ln in r.orbitd_log().splitlines() if "WARNING" in ln or "ERROR" in ln][:6])  # fmt: skip
    assert ok, "never discovered the phone on a network that came up after orbitd started"


def test_laptop_ip_change(lab, record):
    """Laptop roams / DHCP gives it a new IP; then the phone restarts on a new port (only mDNS can tell)."""
    from harness import Rig, run

    lb = lab()
    r = Rig(lb)
    r.pair()
    t0 = r.lab.now(r.laptop)
    run("docker", "network", "disconnect", lb.nets[0].name, r.laptop)
    run("docker", "network", "connect", "--ip", lb.nets[0].ip(22), lb.nets[0].name, r.laptop)
    r.laptop_ip = lb.nets[0].ip(22)
    back = True
    try:
        r.wait_reconnect(t0, 60)
    except AssertionError:
        back = False
    r.stop_phone("TERM")
    r.start_phone("--port 47999")
    ok = True
    try:
        r.wait(lambda: (d := r.device()) and d["state"] == "connected" and d["address"].endswith(":47999"), 60,
               what="found on new port")  # fmt: skip
    except AssertionError:
        ok = False
    alive = "result" in r.call("status")
    control = r.lab.sh(r.laptop, "python /harness/browse.py 4")
    record(reconnected_after_ip_change=back, found_new_port=ok, alive=alive, transitions=r.transitions(t0),
           fresh_zeroconf_in_same_netns_sees=control,
           zc=r.stats()[-1]["zc_ifaces"],
           errors=[ln for ln in r.orbitd_log().splitlines() if "WARNING" in ln or "ERROR" in ln][:6])  # fmt: skip
    assert alive and back
    assert ok, "mDNS discovery dead after the laptop's IP changed"


def test_all_interfaces_gone(lab, record):
    """The laptop's only network goes away for 20 s (Wi-Fi off / airplane mode), then returns."""
    from harness import Rig, run

    lb = lab()
    r = Rig(lb)
    r.pair()
    t0 = r.lab.now(r.laptop)
    run("docker", "network", "disconnect", lb.nets[0].name, r.laptop)
    time.sleep(20)
    alive_mid = "result" in r.call("status")
    run("docker", "network", "connect", "--ip", r.laptop_ip, lb.nets[0].name, r.laptop)
    t_b = r.lab.now(r.laptop)
    # 20 s < 30 s dead window: the TCP session may survive; either way it must be connected soon.
    ok = True
    try:
        r.wait(lambda: r.state() == "connected", 60, what="connected")
    except AssertionError:
        ok = False
    back_s = round(time.time() - t_b, 1)
    r.stop_phone("TERM")
    r.start_phone("--port 47999")
    found = True
    try:
        r.wait(lambda: (d := r.device()) and d["state"] == "connected" and d["address"].endswith(":47999"), 60,
               what="found on new port")  # fmt: skip
    except AssertionError:
        found = False
    control = r.lab.sh(r.laptop, "python /harness/browse.py 4")
    record(alive_while_down=alive_mid, session_survived=ok, back_s=back_s, mdns_after=found, transitions=r.transitions(t0),
           fresh_zeroconf_in_same_netns_sees=control,
           errors=[ln for ln in r.orbitd_log().splitlines() if "WARNING" in ln or "ERROR" in ln][:8])  # fmt: skip
    assert alive_mid and ok
    assert back_s <= RECOVER_WITHIN
    assert found, "mDNS discovery dead after the interface came back"


def test_link_local_only_fallback(lab, record):
    """Phone advertises IPv4 + fe80 link-local only; IPv4 TCP blocked: fe80 must be usable (needs a scope id)."""
    from harness import Rig

    lb = lab(ipv6=True)
    r = Rig(lb)
    r.pair()
    ll = [a for a in r.lab.sh(r.phone, "ip -6 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1").split()
          if a.startswith("fe80")]  # fmt: skip
    r.stop_phone()
    r.start_phone("--host '' --advertise " + ",".join([r.phone_ip] + ll))
    r.wait(lambda: r.state() == "connected", 30, what="connected")
    t_f = r.lab.now(r.phone)
    r.ipt(r.phone, f"-A INPUT -s {r.laptop_ip} -p tcp -j REJECT --reject-with tcp-reset")
    r.lab.sh(r.phone, f"python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}", check=False, timeout=40)
    ok = True
    try:
        r.wait(lambda: (d := r.device()) and d["state"] == "connected" and "fe80" in d["address"], 20, what="via fe80")
    except AssertionError:
        ok = False
    errs = [ln.split("failed: ", 1)[-1] for ln in r.orbitd_log().splitlines() if "connecting to Fake Phone" in ln][-1:]
    record(advertised=[r.phone_ip] + ll, daemon_addresses=r.stats()[-1]["devs"][r.id8]["lan"], via_fe80=ok, errors=errs)
    assert ok


def test_repeated_drops_backoff(rig, record):
    """Flaky Wi-Fi: the connection is reset every 20 s; every reconnect attempt succeeds at once.

    How long is the phone gone after each drop?"""
    r = rig()
    gaps = []
    for _ in range(6):
        t_f = r.reset()
        gaps.append(r.wait_reconnect(t_f, 90))
        time.sleep(max(0, t_f + 20 - time.time()))
    offline = [ln for ln in r.orbitd_log().splitlines() if "dropping its activities" in ln]
    record(reconnect_gaps_s=gaps, activities_dropped=len(offline))
    assert max(gaps) <= RECOVER_WITHIN, gaps


@pytest.mark.slow
def test_blackhole_worst_case_recover(rig, record):
    """Outage long enough for the backoff to reach its cap; healed right after a failed attempt."""
    r = rig()
    t_f = r.blackhole()
    at_cap = f"retrying in {RECONNECT_MAX_DELAY:g} s"
    r.wait(lambda: at_cap in r.orbitd_log(), RECONNECT_SOON_FOR + 120, interval=1, what="backoff at cap")
    n = r.orbitd_log().count(at_cap)
    r.wait(lambda: r.orbitd_log().count(at_cap) > n, RECONNECT_MAX_DELAY + 30, interval=0.2, what="next failed attempt")
    t_h = r.blackhole(False)
    rec = r.wait(lambda: r.state() == "connected", 120, interval=0.5, what="back") and time.time() - t_h
    record(outage_s=round(t_h - t_f, 1), recover_after_heal_s=round(rec, 1))
    assert rec <= RECOVER_WITHIN
