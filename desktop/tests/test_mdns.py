"""Real mDNS (multicast on this host): discovery, auto-connect of paired devices only. Set ORBIT_SKIP_MDNS=1 to skip."""

import asyncio
import os
import socket

import pytest
from daemon_helpers import RawPhone, pin_in, until
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from orbit.daemon import discovery
from orbit.daemon.discovery import SERVICE_TYPE
from orbit.daemon.timeconv import now_ms

pytestmark = pytest.mark.skipif(bool(os.environ.get("ORBIT_SKIP_MDNS")), reason="ORBIT_SKIP_MDNS set")


async def session(ch):
    await ch.send({"t": "ready", "paired": True, "now": now_ms()})
    await ch.recv()
    await ch.send({"t": "snapshot", "now": now_ms(), "activities": [{"id": "via-mdns", "kind": "generic"}]})
    await asyncio.sleep(30)


async def test_mdns_discovers_and_autoconnects_only_paired(make_daemon, short_tmp, api_for, monkeypatch):
    monkeypatch.setattr(discovery, "INTERFACE_POLL", 0.2)  # expiry checks run on this tick
    async with RawPhone(session, name="Paired Phone") as paired, RawPhone(session, name="Stranger") as stranger:
        azc = AsyncZeroconf()
        infos = []
        for phone in (paired, stranger):
            # Unique instance names so parallel runs on the LAN do not collide.
            info = AsyncServiceInfo(
                SERVICE_TYPE,
                f"{phone.name} {phone.id[:6]}.{SERVICE_TYPE}",
                port=phone.port,
                properties={"v": "1", "id": phone.id, "pf": "android"},
                addresses=[socket.inet_aton("127.0.0.1")],
                server=f"orbit-test-{phone.id[:8]}.local.",
            )
            await (await azc.async_register_service(info))
            infos.append(info)
        try:
            pin_in(short_tmp / "daemon", paired.identity, name="Paired Phone")  # no remembered address
            daemon = await make_daemon(mdns=True)
            api = await api_for(daemon)
            acts = await until(lambda: api.call("activities.list"), timeout=10)
            assert [(a["device"], a["id"]) for a in acts] == [(paired.id, "via-mdns")]
            devices = {d["id"]: d for d in await until(lambda: _both(api, paired.id, stranger.id), timeout=10)}
            assert devices[paired.id]["connected"] and devices[paired.id]["transport"] == "lan"
            assert devices[stranger.id]["discovered"] == ["lan"]
            assert not devices[stranger.id]["connected"] and not devices[stranger.id]["paired"]
            assert devices[stranger.id]["name"] == f"Stranger {stranger.id[:6]}"
            assert stranger._writers == []  # never connected to
            await asyncio.sleep(1.0)  # live records are not taken for expired ones
            assert await _both(api, paired.id, stranger.id)
        finally:
            for info in infos:
                await (await azc.async_unregister_service(info))
            await azc.async_close()


async def _both(api, a, b):
    devices = await api.call("devices.list")
    ids = {d["id"] for d in devices}
    return devices if {a, b} <= ids else None
