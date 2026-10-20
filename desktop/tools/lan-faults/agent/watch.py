"""Subscribe to orbitd and log every event as a compact JSON line with wall-clock time; reconnects if orbitd restarts."""

import asyncio
import json
import sys
import time

from orbit.client import LocalClient


def summarize(event, data):
    if event == "devices.changed":
        return {
            "devs": {
                d["id"][:8]: {k: d[k] for k in ("state", "connected", "transport", "address", "discovered", "paired")}
                for d in data["devices"]
            }
        }
    if event == "activities.changed":
        acts = data["activities"]
        return {"n": len(acts), "stale": sum(1 for a in acts if a.get("stale")), "ids": sorted(a["id"] for a in acts)}
    if event == "pairing.changed":
        return {"pairings": len(data["pairings"])}
    return {}


async def main(sock, out):
    with open(out, "a") as f:
        while True:
            try:
                c = await LocalClient.connect(sock)
            except OSError:
                await asyncio.sleep(0.2)
                continue
            await c.call("subscribe")
            async for event, data in c.events():
                f.write(json.dumps({"t": time.time(), "ev": event.split(".")[0], **summarize(event, data)}) + "\n")
                f.flush()
            f.write(json.dumps({"t": time.time(), "ev": "api_lost"}) + "\n")
            f.flush()


asyncio.run(main(sys.argv[1], sys.argv[2]))
