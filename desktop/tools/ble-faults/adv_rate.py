"""Measure how often the real CoreBluetooth/BlueZ scanner re-reports the same peripheral.

orbitd treats a BLE address as gone BLE_STALE_AFTER (30 s) after its last advertisement callback, so
this decides whether a phone that keeps advertising stays "discovered". Usage:
    uv run python tools/ble-faults/adv_rate.py [seconds] [service-uuid]
"""

import asyncio
import collections
import sys
import time

from bleak import BleakScanner


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    uuids = [sys.argv[2]] if len(sys.argv) > 2 else None
    seen: dict[str, list[float]] = collections.defaultdict(list)
    t0 = time.monotonic()

    def cb(device, adv):
        seen[device.address].append(time.monotonic() - t0)

    scanner = BleakScanner(detection_callback=cb, service_uuids=uuids)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    print(f"{len(seen)} peripherals in {seconds:g} s, {sum(map(len, seen.values()))} callbacks")
    for addr, times in sorted(seen.items(), key=lambda kv: -len(kv[1]))[:15]:
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        print(
            f"{addr}: {len(times)} callbacks, first {times[0]:.1f} s, last {times[-1]:.1f} s, "
            f"max gap {max(gaps, default=0):.1f} s"
        )
    once = sum(1 for t in seen.values() if len(t) == 1)
    print(f"seen exactly once: {once}/{len(seen)}")


asyncio.run(main())
