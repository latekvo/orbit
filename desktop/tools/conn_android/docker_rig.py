"""The connectivity rig with the phone and orbitd in separate Linux network namespaces (Docker).

Faults are injected for real: iptables DROP (blackhole), tc netem (delay/loss), `docker network
disconnect` (the phone's interface disappears, like Wi-Fi off), `docker pause` (cgroup freezer:
the whole phone process frozen, like the ColorOS/Android app freezer or an emulator pause).
The rig talks to orbitd's API through a socat TCP bridge published on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from orbit.client import LINE_LIMIT, LocalClient
from rig import JVMPHONE, REPO, Rig, free_port

IMAGE = "orbit-conn-rig"
NET = "cad-net"
PHONE = "cad-phone"
LAPTOP = "cad-laptop"
PHONE_IP = "172.29.77.10"
LAPTOP_IP = "172.29.77.20"


def sh(*argv: str, check: bool = True, timeout: float = 120) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} -> {r.returncode}: {r.stderr.strip()}")
    return r.stdout


def dx(container: str, cmd: str, check: bool = True, detach: bool = False) -> str:
    return sh("docker", "exec", *(["-d"] if detach else []), container, "sh", "-c", cmd, check=check)


def setup() -> int:
    """Creates the network and both containers (idempotent); returns the host port of the API bridge."""
    if sh("docker", "network", "ls", "-q", "-f", f"name=^{NET}$").strip() == "":
        sh("docker", "network", "create", "--subnet", "172.29.77.0/24", NET)
    for name, ip in ((PHONE, PHONE_IP), (LAPTOP, LAPTOP_IP)):
        if sh("docker", "ps", "-aq", "-f", f"name=^{name}$").strip():
            sh("docker", "rm", "-f", name)
        extra = []
        if name == PHONE:
            extra = ["-v", f"{JVMPHONE.parent.parent}:/jvm:ro"]
        else:
            api_port = free_port()
            extra = ["-v", f"{REPO}/desktop:/src:ro", "-p", f"127.0.0.1:{api_port}:7000",
                     "-e", "UV_PROJECT_ENVIRONMENT=/venv"]
        sh("docker", "run", "-d", "--name", name, "--hostname", name, "--network", NET, "--ip", ip,
           "--cap-add", "NET_ADMIN", *extra, IMAGE, "sleep", "infinity")
    # A copy outside the read-only mount: uv writes into the project dir.
    dx(LAPTOP, "mkdir /app && cp -r /src/src /src/pyproject.toml /src/README* /app/ 2>/dev/null; "
       "uv venv -q /venv && uv pip install -q --python /venv /app")
    return api_port


def teardown() -> None:
    sh("docker", "rm", "-f", PHONE, LAPTOP, check=False)
    sh("docker", "network", "rm", NET, check=False)


class DockerRig(Rig):
    def __init__(self, api_port: int, action_delay_ms: int = 0, art_burst: int = 4) -> None:
        super().__init__(action_delay_ms=action_delay_ms)
        self.art_burst = art_burst
        self.api_port = api_port
        self.phone_host = PHONE_IP
        self.phone_port = 47820

    # phone
    def start_phone(self) -> None:
        dx(PHONE, f"ART_BURST={self.art_burst} exec /jvm/bin/jvmphone /state 47820 {self.action_delay_ms} >> /state.log 2>&1", detach=True)
        self.mark("phone_start")

    async def wait_phone_listening(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "listening" in self.phone_text():
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("phone not listening")

    def kill_phone(self, sig: str = "KILL") -> None:
        dx(PHONE, f"pkill -{sig} -f '[d]ev.orbit.jvmphone.MainKt'", check=False)

    def phone_text(self) -> str:
        return dx(PHONE, "cat /state.log 2>/dev/null || true", check=False)

    def phone_conns(self) -> str:
        return dx(PHONE, "ss -tnp state established '( sport = :47820 )' | tail -n +2", check=False)

    # laptop
    async def start_daemon(self) -> None:
        dx(LAPTOP, "rm -f /tmp/d.sock; exec /venv/bin/orbitd --no-ble --no-mdns "
           "--config-dir /cfg --socket /tmp/d.sock --name RigLaptop >> /orbitd.log 2>&1", detach=True)
        for _ in range(200):
            if "/tmp/d.sock" in dx(LAPTOP, "ls /tmp/d.sock 2>/dev/null || true", check=False):
                break
            await asyncio.sleep(0.1)
        dx(LAPTOP, "exec socat TCP-LISTEN:7000,fork,reuseaddr UNIX-CONNECT:/tmp/d.sock", detach=True)
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.api_port, limit=LINE_LIMIT)
                self.client = LocalClient(reader, writer)
                await asyncio.wait_for(self.client.call("subscribe"), 2)
                break
            except (OSError, asyncio.TimeoutError, ConnectionError):
                await asyncio.sleep(0.2)
        self._watch = asyncio.create_task(self._watcher())

    def daemon_text(self) -> str:
        return dx(LAPTOP, "cat /orbitd.log 2>/dev/null || true", check=False)

    # faults
    def blackhole(self, on: bool, direction: str = "both") -> None:
        op = "-A" if on else "-D"
        if direction in ("both", "in"):  # laptop -> phone dropped
            dx(PHONE, f"iptables {op} INPUT -s {LAPTOP_IP} -j DROP", check=on)
        if direction in ("both", "out"):  # phone -> laptop dropped
            dx(PHONE, f"iptables {op} OUTPUT -d {LAPTOP_IP} -j DROP", check=on)
        self.mark(f"blackhole {direction} {'on' if on else 'off'}")

    def netem(self, spec: str | None) -> None:
        if spec:
            dx(PHONE, f"tc qdisc replace dev eth0 root netem {spec}")
        else:
            dx(PHONE, "tc qdisc del dev eth0 root", check=False)
        self.mark(f"netem {spec}")

    def net_disconnect(self) -> None:
        sh("docker", "network", "disconnect", NET, PHONE)
        self.mark("phone network disconnect")

    def net_connect(self, ip: str = PHONE_IP) -> None:
        sh("docker", "network", "connect", "--ip", ip, NET, PHONE)
        self.mark(f"phone network connect {ip}")

    def freeze(self, on: bool) -> None:
        sh("docker", "pause" if on else "unpause", PHONE)
        self.mark("docker pause" if on else "docker unpause")

    async def close(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        sh("docker", "unpause", PHONE, check=False)
        dx(LAPTOP, "pkill -f '[o]rbitd --no-ble'; pkill socat; sleep 0.5; rm -rf /cfg /orbitd.log", check=False)
        dx(PHONE, "pkill -9 -f '[d]ev.orbit.jvmphone.MainKt'; iptables -F; tc qdisc del dev eth0 root; rm -rf /state /state.log", check=False)
