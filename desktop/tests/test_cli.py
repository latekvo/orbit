"""The ``orbit`` CLI as a subprocess against an in-process orbitd and fakephone."""

import asyncio
import sys

from conftest import until


async def orbit(daemon, *args):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from orbit.cli import main; main()",
        "--socket",
        str(daemon.api.path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    return proc.returncode, out.decode()


async def test_connect_pair_activities_action(make_daemon, make_phone):
    phone = await make_phone(auto_accept=True)  # the phone accepts before the laptop does
    daemon = await make_daemon()
    code, out = await orbit(daemon, "connect", f"127.0.0.1:{phone.port}")
    sas = phone.sessions[0].sas
    assert code == 0 and f"code {sas}" in out and phone.device_id in out

    code, out = await orbit(daemon, "pair", "--accept", "--timeout", "5")
    assert code == 0 and f"paired with Fake Phone ({phone.device_id})" in out, out

    await until(lambda: daemon.devices[phone.device_id].activities)
    code, out = await orbit(daemon, "activities")
    assert code == 0 and "[media]" in out and "media:com.example.music" in out and "playing" in out

    code, out = await orbit(daemon, "action", phone.device_id[:8], "media:com.example.music", "media.seek", "42000")
    assert (code, out.strip()) == (0, "ok")
    assert abs(phone.media.position_ms() - 42_000) < 1000

    code, out = await orbit(daemon, "action", phone.device_id[:8], "msg:chat", "notif.0", "123", "--string")
    assert code == 0 and phone.chat.replies == ["123"]

    code, out = await orbit(daemon, "action", phone.device_id[:8], "msg:chat", "notif.0")
    assert code == 1 and "text required" in out

    code, out = await orbit(daemon, "action", "nosuchdevice", "a", "b")
    assert code == 1 and "not_found" in out


async def test_pair_reports_rejection(make_daemon, make_phone):
    phone = await make_phone(auto_accept=None)
    daemon = await make_daemon()
    assert (await orbit(daemon, "connect", f"127.0.0.1:{phone.port}"))[0] == 0
    pair = asyncio.ensure_future(orbit(daemon, "pair", "--accept", "--timeout", "5"))
    await until(lambda: daemon.pairings_json() and daemon.pairings_json()[0]["localAccepted"])
    await phone.respond_pairing(False)
    code, out = await pair
    assert code == 1 and "failed" in out
