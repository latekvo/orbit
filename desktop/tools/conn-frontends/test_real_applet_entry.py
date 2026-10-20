"""The real `orbit-applet --screenshot` entry point during connectivity trouble (offscreen)."""

import asyncio
import os

import pytest
from harness import bin_path, measure


async def screenshot(w, name, theme):
    path = w.dir / f"{name}.png"
    p = await asyncio.create_subprocess_exec(
        bin_path("orbit-applet"), "--socket", str(w.sock), "--theme", theme, "--screenshot", str(path),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 30)
    return p.returncode, path, out.decode()


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_entry_point_screenshots(paired, theme):
    w = paired
    await w.proxy.refuse()
    w.proxy.reset()
    code, path, out = await screenshot(w, f"entry-reconnecting-{theme}", theme)
    assert code == 0 and path.exists(), out
    await asyncio.sleep(14)
    code, path2, out = await screenshot(w, f"entry-offline-{theme}", theme)
    await w.daemon.stop()
    code3, path3, out3 = await screenshot(w, f"entry-no-daemon-{theme}", theme)
    measure(f"entry_screens_{theme}", files=[str(path), str(path2), str(path3)], exit_no_daemon=code3)
    assert code == 0 and code3 == 0
