"""LAN faults with the phone side being the real Kotlin ResponderSession (android/companion/protocol).

    desktop/tools/lan-faults/jvm/build.sh          # once, and after Kotlin changes
    cd desktop && uv run --with pytest-xdist pytest tools/lan-faults/test_kotlin_phone.py -n 4 -v
"""

import time
from pathlib import Path

import pytest
from harness import JvmRig, run

from test_lan_faults import ACTION_SILENCE, DEAD_AFTER, RECOVER_WITHIN, SLACK, honest

pytestmark = pytest.mark.skipif(not (Path(__file__).parent / "jvm" / "cp").is_dir(), reason="run jvm/build.sh")


@pytest.fixture
def jrig(lab):
    def make(**kw):
        r = JvmRig(lab(), **kw)
        r.pair()
        return r

    return make


def test_k_blackhole_recover(jrig, record):
    r = jrig()
    t_f = r.blackhole()
    r.wait(lambda: r.state() == "offline", DEAD_AFTER + 30, interval=0.5, what="offline")
    time.sleep(max(0, t_f + 60 - time.time()))
    t_h = r.blackhole(False)
    rec = r.wait_reconnect(t_f, 90) - (t_h - t_f)
    closes = [(round(e["t"] - t_f, 1), e["reason"]) for e in r.phone_events() if e["kind"] == "session_closed"]
    record(transitions=r.transitions(t_f), phone_closed=closes, recover_after_heal_s=round(rec, 2))
    assert closes and closes[0][0] <= DEAD_AFTER + SLACK
    assert rec <= RECOVER_WITHIN


@pytest.mark.parametrize("seconds", [45, pytest.param(300, marks=pytest.mark.slow)])
def test_k_laptop_suspend(jrig, record, seconds):
    r = jrig(faketime=True)
    t_w = r.sleep_laptop(seconds)
    a = r.action()
    fresh = r.wait_reconnect(t_w, 90)
    first_drop = next((t for t, st in r.transitions(t_w) if st != "connected"), None)
    record(transitions=r.transitions(t_w), action_after_wake=a, applied=len(r.phone_actions(t_w)),
           shown_connected_after_wake_s=first_drop, fresh_session_after_s=fresh,
           causes=[ln.split("cause=")[-1] for ln in r.orbitd_log().splitlines() if "session_closed" in ln])  # fmt: skip
    assert first_drop is not None and first_drop <= 3.0
    assert a.get("result", {}).get("ok") is True or "may have been applied" not in a["error"]["message"], a


def test_k_laptop_vanished(jrig, record):
    """The laptop leaves the network without FIN for 45 s (lid closed on Wi-Fi), then returns."""
    r = jrig()
    net = r.lab.nets[0]
    t_f = r.lab.now(r.phone)
    run("docker", "network", "disconnect", net.name, r.laptop)
    time.sleep(45)
    run("docker", "network", "connect", "--ip", r.laptop_ip, net.name, r.laptop)
    t_b = r.lab.now(r.phone)
    rec = r.wait_reconnect(t_f, 90) - (t_b - t_f)
    closes = [(round(e["t"] - t_f, 1), e["reason"]) for e in r.phone_events() if e["kind"] == "session_closed"]
    st = r.phone_stats()
    record(transitions=r.transitions(t_f), phone_closed=closes, recover_after_back_s=round(rec, 2),
           phone_threads_end=st[-1]["threads"], phone_sessions_end=st[-1]["sessions"])  # fmt: skip
    assert closes and closes[0][0] <= DEAD_AFTER + SLACK
    assert rec <= RECOVER_WITHIN


def test_k_phone_frozen(jrig, record):
    """JVM phone SIGSTOPped 20 s; action during the freeze."""
    r = jrig()
    t_f = r.lab.now(r.phone)
    r.signal_phone("STOP")
    time.sleep(3)
    a = r.action()
    time.sleep(max(0, t_f + 20 - time.time()))
    r.signal_phone("CONT")
    time.sleep(5)
    applied = [round(e["t"] - t_f, 1) for e in r.phone_actions(t_f)]
    record(action_during=a, applied_at=applied, transitions=r.transitions(t_f))
    # No answer to the action: the link is dropped 5 s later and is back once the phone thaws.
    assert honest(a, len(applied))
    assert r.first_state(t_f, "reconnecting") <= 3 + ACTION_SILENCE + 3
    assert r.state() == "connected"


@pytest.mark.slow
def test_k_flapping_rst(jrig, record):
    """Forged RST every 4 s for 4 minutes: thread / fd / session leaks on the Kotlin side and in orbitd."""
    r = jrig()
    time.sleep(3)
    p0, d0 = r.phone_stats()[-1], r.stats()[-1]
    end, n = time.time() + 240, 0
    while time.time() < end:
        r.lab.sh(r.phone, f"timeout 5 python /harness/rst.py {r.phone_ip} 47820 {r.laptop_ip}", check=False)
        time.sleep(4)
        n += 1
    time.sleep(40)
    p1, d1 = r.phone_stats()[-1], r.stats()[-1]
    keys = ("threads", "fds", "sessions", "heap_kb")
    record(cycles=n, phone_start={k: p0[k] for k in keys}, phone_end={k: p1[k] for k in keys},
           daemon_start={k: d0[k] for k in ("fds", "tasks", "rss_kb", "sessions")},
           daemon_end={k: d1[k] for k in ("fds", "tasks", "rss_kb", "sessions")}, state=r.state())  # fmt: skip
    assert p1["threads"] <= p0["threads"] + 2 and p1["fds"] <= p0["fds"] + 2 and p1["sessions"] == 1
    assert r.state() == "connected"
