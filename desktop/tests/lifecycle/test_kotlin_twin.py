"""Nearest twin: the same drop scenarios against the shipping Kotlin ResponderSession (JVM, real TCP).

Starts ``KotlinPhoneRunner`` through Gradle; needs JAVA_HOME (Android Studio's JBR works).
"""

import asyncio
import os
import re
import time
from pathlib import Path

import pytest
from harness import Proc, Proxy, pair

ANDROID = Path(__file__).resolve().parents[3] / "android" / "companion"
JBR = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"

pytestmark = pytest.mark.skipif(not (ANDROID / "gradlew").exists(), reason="no android tree")


class KPhone:
    def __init__(self, root: Path, seconds: int) -> None:
        self.log = root / "kphone.log"
        env = {"ORBIT_KPHONE_LOG": str(self.log), "ORBIT_KPHONE_SECONDS": str(seconds)}
        if "JAVA_HOME" not in os.environ and Path(JBR).exists():
            env["JAVA_HOME"] = JBR
        self.proc = Proc(
            "gradle",
            [
                str(ANDROID / "gradlew"),
                "-p",
                str(ANDROID),
                "--offline",
                "-q",
                ":protocol:test",
                "--tests",
                "*KotlinPhoneRunner*",
                "--rerun-tasks",
            ],
            root / "gradle.log",
            env,
        )

    def lines(self, pattern=""):
        if not self.log.exists():
            return []
        rx = re.compile(pattern)
        return [s for s in self.log.read_text().splitlines() if rx.search(s)]

    async def start(self):
        await self.proc.start()
        deadline = time.monotonic() + 180
        while not self.lines("LISTENING"):
            if time.monotonic() > deadline:
                raise TimeoutError("kotlin phone did not start")
            await asyncio.sleep(0.3)
        _, _, port, self.id = self.lines("LISTENING")[0].split()
        self.port = int(port)

    def executed(self, text):
        return len(self.lines(rf"ACTION {re.escape(text)}$"))


def reply(dev, text):
    return {"device": dev, "activity": "msg:chat", "action": "notif.0", "value": text}


async def test_kotlin_phone_drop_scenarios(lab):
    k = KPhone(lab.root, 200)
    lab.procs.append(k.proc)
    await k.start()
    d = await lab.daemon()
    api = await lab.api(d)
    pr = Proxy(k.port)
    await pr.__aenter__()
    dev = await pair(api, k, port=pr.port)
    assert dev == k.id
    await api.wait_fresh_activities(dev, 5)
    await asyncio.sleep(31)
    out = {}
    # 1. result lost (RST right after the action reached the phone)
    pr.live[0].lose_reply_after = 0.3
    _, res = await api.timed("activities.action", reply(dev, "k-lost"))
    await api.wait_state(dev, "connected", 15, since=time.monotonic() - 0.4)
    await asyncio.sleep(3)
    out["result lost"] = (str(res), k.executed("k-lost"))
    # 2. burst across a drop: honesty
    texts = [f"k-burst-{i}" for i in range(20)]
    tasks = [asyncio.create_task(api.timed("activities.action", reply(dev, t))) for t in texts]
    await asyncio.sleep(0.02)
    for c in pr.live:
        c.rst()
    results = await asyncio.gather(*tasks)
    await asyncio.sleep(4)
    bad = []
    for t, (_, r) in zip(texts, results, strict=True):
        n = k.executed(t)
        if n > 1 or (r == {"ok": True} and n != 1) or (getattr(r, "code", "") == "not_connected" and n):
            bad.append((t, str(r), n))
    out["burst"] = {"bad": bad, "kinds": sorted({str(r) for _, r in results})}
    # 3. laptop vanishes (no FIN reaches the phone): the phone keeps the old session until idle timeout
    await asyncio.sleep(31)
    t0 = time.monotonic()
    pr.live[0].rst_laptop_side_only()
    await api.wait_state(dev, "connected", 10, since=t0 + 0.01)
    _, r3 = await api.timed("activities.action", reply(dev, "k-after-vanish"))
    await asyncio.sleep(1)
    peak = max(int(s.split("live=")[1]) for s in k.lines("SESSION"))
    await asyncio.sleep(32)
    idle = k.lines("idle timeout")
    out["vanish"] = (
        str(r3),
        k.executed("k-after-vanish"),
        f"peak live sessions {peak}",
        idle[-1][-60:] if idle else None,
    )
    print("\nkotlin twin:", out)
    assert out["result lost"][1] == 1 and not bad and out["vanish"][1] == 1 and idle
    await pr.close()
