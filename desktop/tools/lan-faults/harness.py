"""Docker lab for LAN fault injection: real orbitd and orbit-fakephone in separate network namespaces.

Each Lab owns its networks (10.231.X.0/24, X allocated by trying until Docker accepts) and containers
(``orbitlan-<tag>-<role>``), and removes them on close. Faults are plain iptables / tc / docker
network commands, run with ``Lab.sh``. All timestamps are wall-clock seconds from inside the
containers (they share the Docker VM's clock).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent.parent
IMAGE = "orbitlan"
SOCK = "/tmp/conn-lan/o.sock"
CFG = "/tmp/conn-lan/cfg"


def run(*argv: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} -> {p.returncode}: {p.stderr.strip() or p.stdout.strip()}")
    return p


def build_image() -> None:
    run("docker", "build", "-q", "-t", IMAGE, "-f", str(HERE / "Dockerfile"), str(DESKTOP), timeout=900)


@dataclass
class Net:
    name: str
    prefix: str  # "10.231.X"
    v6prefix: str | None = None

    def ip(self, host: int) -> str:
        return f"{self.prefix}.{host}"


@dataclass
class Lab:
    tag: str
    nets: list[Net] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, tag: str, n_nets: int = 1, ipv6: bool = False) -> Lab:
        lab = cls(f"{tag}-{secrets.token_hex(2)}")
        for i in range(n_nets):
            lab.add_net(f"n{i}", ipv6=ipv6)
        return lab

    def add_net(self, suffix: str, ipv6: bool = False) -> Net:
        name = f"orbitlan-{self.tag}-{suffix}"
        for x in range(10, 250):
            argv = ["docker", "network", "create", "--subnet", f"10.231.{x}.0/24"]
            v6 = None
            if ipv6:
                v6 = f"fd31:{x:x}::"
                argv += ["--ipv6", "--subnet", f"{v6}/64"]
            p = run(*argv, name, check=False)
            if p.returncode == 0:
                net = Net(name, f"10.231.{x}", v6)
                self.nets.append(net)
                return net
            if "overlap" not in p.stderr and "already exists" not in p.stderr:
                raise RuntimeError(p.stderr)
        raise RuntimeError("no free 10.231.X.0/24 subnet")

    def container(self, role: str, net: Net, host: int, extra_nets: list[tuple[Net, int]] = ()) -> str:
        name = f"orbitlan-{self.tag}-{role}"
        run(
            "docker", "run", "-d", "--name", name, "--hostname", role,
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "--network", net.name, "--ip", net.ip(host),
            "-v", f"{DESKTOP / 'src'}:/src:ro", "-v", f"{HERE / 'agent'}:/harness:ro",
            "-v", f"{HERE / 'jvm' / 'cp'}:/jvm:ro",
            IMAGE, "sleep", "infinity",
        )  # fmt: skip
        self.containers.append(name)
        for n, h in extra_nets:
            run("docker", "network", "connect", "--ip", n.ip(h), n.name, name)
        return name

    def sh(self, ctr: str, cmd: str, check: bool = True, timeout: float = 120) -> str:
        return run("docker", "exec", ctr, "sh", "-c", cmd, check=check, timeout=timeout).stdout

    def bg(self, ctr: str, cmd: str) -> None:
        run("docker", "exec", "-d", ctr, "sh", "-c", cmd)

    def now(self, ctr: str) -> float:
        return float(self.sh(ctr, "date +%s.%N"))

    def jsonl(self, ctr: str, path: str) -> list[dict]:
        out = self.sh(ctr, f"cat {path} 2>/dev/null", check=False)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def close(self) -> None:
        for c in self.containers:
            run("docker", "rm", "-f", c, check=False)
        for n in self.nets:
            run("docker", "network", "rm", n.name, check=False)


class Rig:
    """One laptop (orbitd) + one phone (fakephone) on ``lab.nets[0]``, laptop .2 and phone .3."""

    def __init__(self, lab: Lab, phone_args: str = "", laptop_nets: list[tuple[Net, int]] = (),
                 phone_nets: list[tuple[Net, int]] = (), faketime: bool = False, phone_host: int = 3) -> None:  # fmt: skip
        self.lab = lab
        net = lab.nets[0]
        self.laptop = lab.container("laptop", net, 2, list(laptop_nets))
        self.phone = lab.container("phone", net, phone_host, list(phone_nets))
        self.phone_ip = net.ip(phone_host)
        self.laptop_ip = net.ip(2)
        self.faketime = faketime
        self.phone_args = phone_args
        self.start_orbitd()
        self.start_phone(phone_args)
        self.phone_id = self.phone_status()["id"]
        self.id8 = self.phone_id[:8]

    # -- processes -------------------------------------------------------------------------------

    def start_orbitd(self) -> None:
        pre = ""
        if self.faketime:
            self.lab.sh(self.laptop, "echo 0 > /tmp/sleep.offset")
            pre = "ORBIT_SLEEP_FILE=/tmp/sleep.offset "
        self.lab.sh(self.laptop, "mkdir -p /tmp/conn-lan")
        self.lab.bg(
            self.laptop,
            f"{pre}exec python /harness/orbitd.py --config-dir {CFG} --socket {SOCK} --no-ble --name Laptop "
            ">> /tmp/orbitd.log 2>&1",
        )
        self.wait(lambda: "ok" in self.lab.sh(self.laptop, f"test -S {SOCK} && echo ok", check=False), 10)
        self.lab.bg(self.laptop, f"exec python /harness/watch.py {SOCK} /tmp/watch.jsonl")

    def start_phone(self, args: str = "") -> None:
        self.lab.bg(self.phone, f"exec python /harness/phone.py --config-dir /tmp/phonecfg {args} >> /tmp/phone.log 2>&1")
        self.wait(lambda: "ok" in self.lab.sh(self.phone, "test -S /tmp/phone.ctl && echo ok", check=False), 10)
        time.sleep(0.3)

    def stop_phone(self, sig: str = "TERM") -> None:
        self.lab.sh(self.phone, f"pkill -{sig} -f [/]harness/phone.py; rm -f /tmp/phone.ctl", check=False)
        for _ in range(50):
            if not self.lab.sh(self.phone, "pgrep -f [/]harness/phone.py", check=False).strip():
                return
            time.sleep(0.1)

    def sleep_laptop(self, seconds: float, network_down: bool = True, monotonic_frozen: bool = True) -> float:
        """Suspend the laptop: orbitd stopped, (optionally) its network down, its monotonic clock frozen.

        Returns the wake time. Needs ``faketime=True`` for ``monotonic_frozen``.
        """
        self.signal_orbitd("STOP")
        if network_down:
            self.lab.sh(self.laptop, "iptables -A INPUT -j DROP; iptables -A OUTPUT -j DROP")
        time.sleep(seconds)
        if monotonic_frozen:
            prev = float(self.lab.sh(self.laptop, "cat /tmp/sleep.offset") or 0)
            self.lab.sh(self.laptop, f"echo {prev + seconds} > /tmp/sleep.offset")
        if network_down:
            self.lab.sh(self.laptop, "iptables -D INPUT -j DROP; iptables -D OUTPUT -j DROP")
        t = self.lab.now(self.laptop)
        self.signal_orbitd("CONT")
        return t

    def signal_orbitd(self, sig: str) -> None:
        self.lab.sh(self.laptop, f"pkill -{sig} -f [/]harness/orbitd.py")

    def signal_phone(self, sig: str) -> None:
        self.lab.sh(self.phone, f"pkill -{sig} -f [/]harness/phone.py")

    # -- API -------------------------------------------------------------------------------------

    def call(self, method: str, params: dict | None = None, timeout: float = 70) -> dict:
        out = self.lab.sh(
            self.laptop, f"python /harness/call.py {SOCK} {method} '{json.dumps(params or {})}'", timeout=timeout
        )
        return json.loads(out.strip().splitlines()[-1])

    def phone_ctl(self, cmd: dict) -> dict:
        return json.loads(self.lab.sh(self.phone, f"python /harness/ctl.py /tmp/phone.ctl '{json.dumps(cmd)}'"))

    def phone_status(self) -> dict:
        return self.phone_ctl({"cmd": "status"})

    def device(self) -> dict | None:
        r = self.call("devices.list").get("result") or []
        return next((d for d in r if d["id"] == self.phone_id), None)

    def state(self) -> str | None:
        d = self.device()
        return d and d["state"]

    def activities(self) -> list[dict]:
        return [a for a in self.call("activities.list")["result"] if a["device"] == self.phone_id]

    def action(self, activity: str = "media:com.example.music", action: str = "media.next", value=None) -> dict:
        params = {"device": self.phone_id, "activity": activity, "action": action}
        if value is not None:
            params["value"] = value
        return self.call("activities.action", params)

    def phone_actions(self, since: float = 0) -> list[dict]:
        return [e for e in self.lab.jsonl(self.phone, "/tmp/phone-events.jsonl") if e["kind"] == "action" and e["t"] >= since]

    def watch(self) -> list[dict]:
        return self.lab.jsonl(self.laptop, "/tmp/watch.jsonl")

    def stats(self) -> list[dict]:
        return self.lab.jsonl(self.laptop, "/tmp/orbitd-stats.jsonl")

    def orbitd_log(self) -> str:
        return self.lab.sh(self.laptop, "cat /tmp/orbitd.log")

    def phone_log(self) -> str:
        return self.lab.sh(self.phone, "cat /tmp/phone.log")

    # -- flows -----------------------------------------------------------------------------------

    def wait(self, pred, timeout: float, interval: float = 0.2, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while True:
            v = pred()
            if v:
                return v
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} not met within {timeout} s")
            time.sleep(interval)

    def pair(self) -> None:
        """Pair the way the applet does: connect to the mDNS-discovered device id, accept."""
        self.wait(lambda: self.device() is not None, 15, what="phone discovered via mDNS")
        r = self.call("devices.connect", {"id": self.phone_id})
        assert "result" in r, r
        p = self.wait(lambda: self.call("pairing.list")["result"], 10, what="pairing pending")
        self.call("pairing.respond", {"session": p[0]["session"], "accept": True})
        self.wait(lambda: (d := self.device()) and d["paired"] and d["state"] == "connected", 10, what="paired")
        self.wait(lambda: len(self.activities()) >= 3, 10, what="activities")

    def state_at(self, rows: list[dict], t: float) -> str | None:
        st = None
        for r in rows:
            if r["t"] > t:
                break
            if r["ev"] == "devices" and self.id8 in r["devs"]:
                st = r["devs"][self.id8]["state"]
        return st

    def transitions(self, since: float = 0) -> list[tuple[float, str]]:
        """(time - since, state) for each change of the phone's device state after ``since``."""
        out, last = [], None
        for r in self.watch():
            if r["ev"] != "devices":
                continue
            d = r["devs"].get(self.id8)
            st = d["state"] if d else "absent"
            if st != last:
                if r["t"] >= since:
                    out.append((round(r["t"] - since, 2), st))
                last = st
        return out

    def reconnected_at(self, since: float) -> float | None:
        """Seconds after ``since`` at which the phone was connected again after a drop seen after ``since``."""
        tr = self.transitions(since)
        dropped = next((i for i, (_, st) in enumerate(tr) if st != "connected"), None)
        if dropped is None:
            return None
        return next((t for t, st in tr[dropped:] if st == "connected"), None)

    def wait_reconnect(self, since: float, timeout: float) -> float:
        return self.wait(lambda: self.reconnected_at(since), timeout, interval=0.25, what="drop and reconnect")

    def first(self, since: float, pred) -> float | None:
        """Seconds after ``since`` of the first watch row matching ``pred``."""
        for r in self.watch():
            if r["t"] >= since and pred(r):
                return round(r["t"] - since, 2)
        return None

    def first_state(self, since: float, state: str) -> float | None:
        return self.first(since, lambda r: r["ev"] == "devices" and r["devs"].get(self.id8, {}).get("state") == state)

    # -- faults ----------------------------------------------------------------------------------

    def reset(self) -> float:
        """Forge a RST phone -> laptop (retried: a segment racing the sniffed one makes it out of window)."""
        for _ in range(5):
            t = self.lab.now(self.phone)
            self.lab.sh(self.phone, f"timeout 10 python /harness/rst.py {self.phone_ip} 47820 {self.laptop_ip}", check=False)
            try:
                self.wait(lambda: self.first(t, lambda x: x["ev"] == "devices"
                                             and x["devs"].get(self.id8, {}).get("state") != "connected") is not None,
                          2, what="reset took")  # fmt: skip
                return t
            except AssertionError:
                continue
        raise AssertionError("forged RST never took")

    def ipt(self, ctr: str, rule: str, v6: bool = False) -> None:
        self.lab.sh(ctr, f"{'ip6tables' if v6 else 'iptables'} {rule}")

    def blackhole(self, on: bool = True, ctr: str | None = None, port_only: bool = True) -> float:
        """Silently drop all TCP between laptop and phone (both directions), on the phone side."""
        ctr = ctr or self.phone
        op = "-A" if on else "-D"
        peer = self.laptop_ip if ctr == self.phone else self.phone_ip
        match = "-p tcp" if port_only else ""
        self.ipt(ctr, f"{op} INPUT -s {peer} {match} -j DROP")
        self.ipt(ctr, f"{op} OUTPUT -d {peer} {match} -j DROP")
        return self.lab.now(ctr)


class JvmRig(Rig):
    """Like Rig, but the phone is the Kotlin ResponderSession (jvm/build.sh first), paired via host:port."""

    STATE = "/tmp/jvmstate"

    def __init__(self, lab: Lab, faketime: bool = False) -> None:
        self.lab = lab
        net = lab.nets[0]
        self.laptop = lab.container("laptop", net, 2)
        self.phone = lab.container("phone", net, 3)
        self.phone_ip, self.laptop_ip = net.ip(3), net.ip(2)
        self.faketime = faketime
        self.start_orbitd()
        self.start_phone()
        out = self.wait(lambda: re.search(r"id ([0-9a-f]{32})", self.phone_log()), 30, what="jvm phone up")
        self.phone_id = out.group(1)
        self.id8 = self.phone_id[:8]

    def start_phone(self, args: str = "") -> None:
        self.lab.bg(self.phone, f"exec java -cp '/jvm/1:/jvm/2:/jvm/*' dev.orbit.protocol.lan.LanResponderMain 47820 "
                                f"{self.STATE} >> /tmp/phone.log 2>&1")  # fmt: skip

    def phone_log(self) -> str:
        return self.lab.sh(self.phone, "cat /tmp/phone.log", check=False)

    def signal_phone(self, sig: str) -> None:
        self.lab.sh(self.phone, f"pkill -{sig} -f [L]anResponderMain")

    def pair(self) -> None:
        r = self.call("devices.connect", {"host": self.phone_ip, "port": 47820})
        assert "result" in r, r
        p = self.wait(lambda: self.call("pairing.list")["result"], 10, what="pairing pending")
        self.call("pairing.respond", {"session": p[0]["session"], "accept": True})
        self.wait(lambda: (d := self.device()) and d["paired"] and d["state"] == "connected", 10, what="paired")
        self.wait(lambda: len(self.activities()) >= 2, 10, what="activities")

    def phone_events(self) -> list[dict]:
        return self.lab.jsonl(self.phone, f"{self.STATE}/events.jsonl")

    def phone_actions(self, since: float = 0) -> list[dict]:
        return [e for e in self.phone_events() if e["kind"] == "action" and e["t"] >= since]

    def phone_stats(self) -> list[dict]:
        return self.lab.jsonl(self.phone, f"{self.STATE}/stats.jsonl")


def parse_kv(line: str) -> dict:
    return dict(re.findall(r"(\w+)=('[^']*'|\S+)", line))


def env_flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")
