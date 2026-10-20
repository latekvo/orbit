"""Clock skew between phone and laptop: timers and media positions as a frontend on the laptop would show them."""

import asyncio
import os
import signal
import time

import pytest
from harness import pair

HOUR, DAY = 3_600_000, 86_400_000


def real_ms() -> int:
    return time.time_ns() // 1_000_000


def shown(acts, laptop_skew=0):
    """(timer remaining s, media position s) as computed by a frontend whose clock is the laptop's."""
    now = real_ms() + laptop_skew
    t = next(a for a in acts if a["id"] == "timer:countdown")
    m = next(a for a in acts if a["id"] == "media:com.example.music")
    rem = (t["chrono"]["baseMs"] - now) / 1000
    md = m["media"]
    pos = (md["positionMs"] + (now - md["positionAtMs"]) * md["rate"]) / 1000
    return rem, pos, (now - t["updatedMs"]) / 1000


@pytest.mark.parametrize("phone_skew", [-DAY, -HOUR, 0, HOUR, DAY])
async def test_phone_clock_skew(lab, phone_skew):
    d = await lab.daemon()
    p = await lab.phone(env={"ORBIT_WALL_SKEW_MS": str(phone_skew)})
    t_start = p.grep("listening on")[0][0]
    api = await lab.api(d)
    dev = await pair(api, p)
    await api.wait_fresh_activities(dev, 5)
    await asyncio.sleep(2)
    acts = [a for a in await api.call("activities.list") if a["device"] == dev]
    rem, pos, age = shown(acts)
    elapsed = time.monotonic() - t_start
    print(
        f"\nphone skew {phone_skew / HOUR:+.0f}h: timer shows {rem:.1f}s (truth {300 - elapsed:.1f}), "
        f"media at {pos:.1f}s (truth {elapsed:.1f}), timer updatedMs age {age:.1f}s"
    )
    assert abs(rem - (300 - elapsed)) < 1.0 and abs(pos - elapsed) < 1.0 and -2 < age < 60


@pytest.mark.parametrize("laptop_skew", [-HOUR, HOUR])
async def test_laptop_clock_skew(lab, laptop_skew):
    d = await lab.daemon(env={"ORBIT_WALL_SKEW_MS": str(laptop_skew)})
    p = await lab.phone()
    t_start = p.grep("listening on")[0][0]
    api = await lab.api(d)
    dev = await pair(api, p)
    await api.wait_fresh_activities(dev, 5)
    acts = [a for a in await api.call("activities.list") if a["device"] == dev]
    rem, pos, _ = shown(acts, laptop_skew)
    elapsed = time.monotonic() - t_start
    print(
        f"\nlaptop skew {laptop_skew / HOUR:+.0f}h: timer {rem:.1f}s (truth {300 - elapsed:.1f}), "
        f"media {pos:.1f}s (truth {elapsed:.1f})"
    )
    assert abs(rem - (300 - elapsed)) < 1.0 and abs(pos - elapsed) < 1.0


async def test_laptop_clock_step_mid_session(lab):
    """The laptop's clock is stepped by +1 h (NTP fix) while connected; the phone keeps running."""
    skew_file = lab.root / "skew"
    skew_file.write_text("0")
    d = await lab.daemon(env={"ORBIT_SKEW_FILE": str(skew_file)})
    p = await lab.phone()
    t_start = p.grep("listening on")[0][0]
    api = await lab.api(d)
    dev = await pair(api, p)
    await api.wait_fresh_activities(dev, 5)
    skew_file.write_text(str(HOUR))
    os.kill(d.pid, signal.SIGUSR2)
    await d.wait_line("WRAP skew now", 5)
    samples = []
    for _ in range(4):
        await asyncio.sleep(5)
        acts = [a for a in await api.call("activities.list") if a["device"] == dev]
        rem, pos, _ = shown(acts, HOUR)
        samples.append((round(rem, 1), round(300 - (time.monotonic() - t_start), 1)))
    print(f"\nlaptop clock +1h mid-session: timer (shown, truth) every 5 s: {samples}")
    assert abs(samples[-1][0] - samples[-1][1]) < 2, "timer stays off by the clock step until the phone resends it"
