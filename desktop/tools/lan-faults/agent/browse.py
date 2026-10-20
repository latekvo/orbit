"""Independent mDNS check: browse _orbit._tcp for N seconds with a fresh zeroconf and print what resolves."""

import asyncio
import json
import sys

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf


async def main(seconds):
    azc = AsyncZeroconf()
    found = {}

    def on(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Removed:
            asyncio.ensure_future(resolve(zeroconf, service_type, name))

    async def resolve(zc, st, name):
        info = AsyncServiceInfo(st, name)
        if await info.async_request(zc, 3000):
            found[name] = {"port": info.port, "addresses": info.parsed_scoped_addresses(IPVersion.All)}

    browser = AsyncServiceBrowser(azc.zeroconf, "_orbit._tcp.local.", handlers=[on])
    await asyncio.sleep(seconds)
    await browser.async_cancel()
    await azc.async_close()
    print(json.dumps(found))


asyncio.run(main(float(sys.argv[1])))
