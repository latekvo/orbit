"""DNS-SD advertisement of the fake phone (spec/PROTOCOL.md section 1.1)."""

from __future__ import annotations

import ipaddress
import socket
import warnings

import zeroconf
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

SERVICE_TYPE = "_orbit._tcp.local."
WILDCARDS = ("", "0.0.0.0")


def advertised_addresses(host: str) -> list[str]:
    """IPv4 addresses to put in the A records for a server bound to ``host``."""
    if host not in WILDCARDS:
        return [socket.gethostbyname(host)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        addresses = zeroconf.get_all_addresses()
    lan = [a for a in addresses if not ipaddress.ip_address(a).is_loopback]
    return lan or addresses


class Advertiser:
    def __init__(self, name: str, device_id: str, platform: str, host: str, port: int) -> None:
        self.addresses = advertised_addresses(host)
        self.info = AsyncServiceInfo(
            SERVICE_TYPE,
            f"{name}.{SERVICE_TYPE}",
            port=port,
            properties={"v": "1", "id": device_id, "pf": platform},
            server=f"orbit-fakephone-{device_id[:12]}.local.",
            parsed_addresses=self.addresses,
        )
        self._zc = AsyncZeroconf(ip_version=zeroconf.IPVersion.V4Only)

    @property
    def instance_name(self) -> str:
        return self.info.name

    async def register(self) -> None:
        # allow_name_change: a second fake phone with the same name becomes "Name (2)".
        await self._zc.async_register_service(self.info, allow_name_change=True)

    async def close(self) -> None:
        try:
            await self._zc.async_unregister_service(self.info)
        finally:
            await self._zc.async_close()
