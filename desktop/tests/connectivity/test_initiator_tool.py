# ruff: noqa: E501
"""android/companion/tools/orbit_initiator.py against the Kotlin responder, through faults."""

import asyncio
import json
import sys
import time

from .harness import MEDIA, REPO, log

TOOL = REPO / "android/companion/tools/orbit_initiator.py"


async def run_tool(port, state, *args, timeout=60):
    p = await asyncio.create_subprocess_exec(
        sys.executable,
        str(TOOL),
        "--tcp",
        f"127.0.0.1:{port}",
        "--state",
        str(state),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(p.communicate(), timeout)
    lines = [json.loads(x) for x in out.decode().splitlines() if x.startswith("{")]
    return p.returncode, lines, out.decode()


async def test_initiator_tool_pair_action_fragmented(rig, workdir):
    phone, proxy, _ = await rig(phone_opts={"auto_accept": True}, laptop=False)
    await phone.cmd("upsert", activity=MEDIA)
    for d in (proxy.up, proxy.down):
        d.set(chunk=1, latency=0.05)
    t0 = time.monotonic()
    rc, lines, raw = await run_tool(
        proxy.port,
        workdir / "init",
        "--duration",
        "6",
        "--action",
        MEDIA["id"],
        "media.pause",
        "--action-delay",
        "0.5",
        "--bye",
    )
    evs = [(x["ev"], x.get("t")) for x in lines]
    result = next((x for x in lines if x.get("t") == "action_result"), None)
    log(f"initiator tool (1-byte chunks, 50 ms): rc {rc}, events {evs[:8]}..., result {result}")
    assert rc == 0 and result and result["ok"] is True
    closed = await phone.wait_event("session_closed", 5, t0)
    assert closed["reason"] == "peer said bye"
    rc2, lines2, _ = await run_tool(proxy.port, workdir / "init", "--duration", "5", "--exit-after-snapshot")
    snap = next((x for x in lines2 if x.get("t") == "snapshot"), None)
    log(
        f"initiator tool second run (pinned): rc {rc2}, snapshot activities {len(snap['activities']) if snap else None}"
    )
    assert rc2 == 0 and snap and not any(x.get("ev") == "pairing" for x in lines2)


async def test_initiator_tool_survives_rst(rig, workdir):
    phone, proxy, _ = await rig(phone_opts={"auto_accept": True}, laptop=False)
    await phone.cmd("upsert", activity=MEDIA)
    task = asyncio.create_task(run_tool(proxy.port, workdir / "init2", "--duration", "10"))
    await phone.wait_event("session_state", 10, 0, lambda e: e["state"] == "PAIRED")
    await asyncio.sleep(0.5)
    proxy.rst_all()
    rc, lines, raw = await task
    closed = next((x for x in lines if x.get("ev") == "closed"), None)
    log(f"initiator tool after RST: rc {rc}, closed event {closed}")
    assert rc == 0 and closed
