"""mDNS-discovered phone in a separate Linux network namespace (Docker): restart, IP change, reinstall.

Needs Docker and the image (built from desktop/):

    docker build -t orbit-conn-lifecycle -f tests/lifecycle/docker/Dockerfile .
    ORBIT_LIFECYCLE=1 uv run pytest tests/lifecycle/test_docker_mdns.py -s -v
"""

import asyncio
import json
import secrets
import shutil
import subprocess
import time

import pytest

IMAGE = "orbit-conn-lifecycle"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")

BRIDGE = r"""
import asyncio, json, sys, time
from orbit.client import LocalClient, ApiError
async def main():
    for _ in range(100):
        try:
            c = await LocalClient.connect("/tmp/o.sock"); break
        except OSError:
            await asyncio.sleep(0.1)
    await c.call("subscribe")
    out = lambda o: print(json.dumps(o), flush=True)
    async def events():
        async for ev, data in c.events():
            out({"event": ev, "data": data})
    asyncio.create_task(events())
    loop = asyncio.get_running_loop()
    r = asyncio.StreamReader(); await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(r), sys.stdin)
    async def one(req):
        try:
            out({"id": req["id"], "result": await c.call(req["method"], req.get("params") or {})})
        except ApiError as e:
            out({"id": req["id"], "error": {"code": e.code, "message": e.message}})
    while line := await r.readline():
        asyncio.create_task(one(json.loads(line)))
asyncio.run(main())
"""


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


class Net:
    def __init__(self):
        self.tag = secrets.token_hex(3)
        self.net = f"lcnet-{self.tag}"
        self.octet = 100 + secrets.randbelow(100)
        self.subnet = f"172.31.{self.octet}.0/24"
        self.containers = []

    def ip(self, host):
        return f"172.31.{self.octet}.{host}"

    def __enter__(self):
        sh("docker", "network", "create", "--subnet", self.subnet, self.net)
        return self

    def run(self, role, ip):
        name = f"lc-{role}-{self.tag}"
        sh(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.net,
            "--ip",
            ip,
            "--cap-add",
            "NET_ADMIN",
            IMAGE,
            "sleep",
            "infinity",
        )
        self.containers.append(name)
        return name

    def __exit__(self, *exc):
        for c in self.containers:
            sh("docker", "rm", "-f", c, check=False)
        sh("docker", "network", "rm", self.net, check=False)


class Bridge:
    def __init__(self, container):
        self.container = container
        self.events = []
        self._pending = {}
        self._id = 0

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self.container,
            "python",
            "-u",
            "-c",
            BRIDGE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=1 << 24,
        )
        self._task = asyncio.create_task(self._read())

    async def _read(self):
        while line := await self.proc.stdout.readline():
            msg = json.loads(line)
            if "event" in msg:
                self.events.append((time.monotonic(), msg["event"], msg["data"]))
            else:
                self._pending.pop(msg["id"]).set_result(msg)

    async def call(self, method, params=None):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        self.proc.stdin.write((json.dumps({"id": self._id, "method": method, "params": params or {}}) + "\n").encode())
        msg = await asyncio.wait_for(fut, 40)
        if "error" in msg:
            raise RuntimeError(f"{msg['error']['code']}: {msg['error']['message']}")
        return msg["result"]

    def states(self, since=0.0):
        out = []
        for t, ev, data in self.events:
            if ev != "devices.changed" or t < since:
                continue
            snap = tuple(
                sorted((d["id"][:8], d["state"], d["paired"], tuple(d["discovered"])) for d in data["devices"])
            )
            if not out or out[-1][1] != snap:
                out.append((t, snap))
        return out

    async def wait(self, pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            devs = await self.call("devices.list")
            if r := pred(devs):
                return time.monotonic(), r
            await asyncio.sleep(0.1)
        raise TimeoutError(f"condition not met; states {self.states()[-5:]}")

    async def close(self):
        self.proc.stdin.close()
        self.proc.kill()
        await self.proc.wait()


def start_daemon(c):
    sh(
        "docker",
        "exec",
        "-d",
        c,
        "sh",
        "-c",
        "python /orbit/wrap.py daemon -- --name Lap --config-dir /tmp/d --socket /tmp/o.sock --no-ble -v"
        " > /tmp/d.log 2>&1",
    )


def start_phone(c, cfg="/tmp/p"):
    sh(
        "docker",
        "exec",
        "-d",
        c,
        "sh",
        "-c",
        f"python /orbit/wrap.py phone -- --name Pixel --config-dir {cfg} --auto-accept -v >> /tmp/p.log 2>&1",
    )


def kill_phone(c):
    sh("docker", "exec", c, "pkill", "-9", "-f", "wrap.py phone", check=False)


def phone_listening(c, n):
    out = sh("docker", "exec", c, "sh", "-c", "grep -c 'mdns registered' /tmp/p.log || true").stdout.strip()
    return int(out or 0) >= n


async def wait_phone_up(c, n, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if phone_listening(c, n):
            return time.monotonic()
        await asyncio.sleep(0.1)
    raise TimeoutError("phone did not come up")


async def paired_pair(net):
    lap = net.run("lap", net.ip(2))
    ph = net.run("phone", net.ip(10))
    start_phone(ph)
    await wait_phone_up(ph, 1)
    start_daemon(lap)
    b = Bridge(lap)
    await b.start()
    _, dev = await b.wait(lambda ds: next((d["id"] for d in ds if d["name"] == "Pixel"), None), 20)
    await b.call("devices.connect", {"id": dev})
    [p] = await b.call("pairing.list")
    await b.call("pairing.respond", {"session": p["session"], "accept": True})
    await b.wait(lambda ds: any(d["id"] == dev and d["paired"] and d["connected"] for d in ds), 10)
    return lap, ph, b, dev


def connected(dev):
    return lambda ds: any(d["id"] == dev and d["state"] == "connected" for d in ds)


@pytest.mark.parametrize("down", [20, 45])
async def test_mdns_phone_restart_reconnect_lag(down):
    with Net() as net:
        lap, ph, b, dev = await paired_pair(net)
        await asyncio.sleep(31)
        t0 = time.monotonic()
        kill_phone(ph)
        await asyncio.sleep(down)
        start_phone(ph)
        t_up = await wait_phone_up(ph, 2)
        t_conn, _ = await b.wait(connected(dev), 90)
        log = sh("docker", "exec", lap, "grep", "-E", "discovered|failed|connected to", "/tmp/d.log").stdout
        print(
            f"\nmDNS phone down {down}s: reachable+announced at {t_up - t0:.1f}s, connected at {t_conn - t0:.1f}s "
            f"(lag {t_conn - t_up:.1f}s)\n{log[-1500:]}"
        )
        await b.close()
        assert t_conn - t_up < 5


async def test_mdns_phone_ip_change_while_connected():
    """Phone moves to a new IP (DHCP renew / AP roam); the companion re-registers on the new address."""
    with Net() as net:
        lap, ph, b, dev = await paired_pair(net)
        await asyncio.sleep(31)
        t0 = time.monotonic()
        sh("docker", "network", "disconnect", net.net, ph)
        sh("docker", "network", "connect", "--ip", net.ip(20), net.net, ph)
        kill_phone(ph)  # the fake phone advertises fixed addresses: restart it to announce the new one
        start_phone(ph)
        t_up = await wait_phone_up(ph, 2)
        t_rec, _ = await b.wait(lambda ds: any(d["id"] == dev and d["state"] != "connected" for d in ds), 60)
        t_conn, _ = await b.wait(connected(dev), 60)
        devs = await b.call("devices.list")
        addr = next(d["address"] for d in devs if d["id"] == dev)
        log = sh(
            "docker", "exec", lap, "grep", "-E", "discovered|failed|session_closed|connected to", "/tmp/d.log"
        ).stdout
        print(
            f"\nIP change: new address announced at {t_up - t0:.1f}s; laptop noticed drop at {t_rec - t0:.1f}s; "
            f"connected again at {t_conn - t0:.1f}s via {addr}\n{log[-1500:]}"
        )
        await b.close()
        assert t_conn - t_up < 5, f"phone reachable at its new address but laptop needed {t_conn - t_up:.0f}s"


async def test_mdns_phone_reinstalled():
    with Net() as net:
        lap, ph, b, dev = await paired_pair(net)
        kill_phone(ph)
        sh("docker", "exec", ph, "rm", "-rf", "/tmp/p")
        start_phone(ph)
        await wait_phone_up(ph, 2)
        await asyncio.sleep(20)
        devs = await b.call("devices.list")
        view = [(d["id"][:8], d["name"], d["paired"], d["state"], d["discovered"]) for d in devs]
        log = sh("docker", "exec", lap, "grep", "-E", "failed|discovered", "/tmp/d.log").stdout
        handshakes = sh("docker", "exec", ph, "grep", "-c", "session s.* connected", "/tmp/p.log", check=False).stdout
        print(
            f"\nreinstalled phone (mDNS): laptop lists {view}; handshakes on phone {handshakes.strip()}\n{log[-1200:]}"
        )
        await b.close()
        same = [v for v in view if v[1] == "Pixel"]
        assert len(same) == 1, "two 'Pixel' entries: the stale paired one is offline forever"
