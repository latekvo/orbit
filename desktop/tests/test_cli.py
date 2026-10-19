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


def test_parse_address():
    import pytest

    from orbit.cli import parse_address

    assert parse_address("192.168.1.20:47820") == {"host": "192.168.1.20", "port": 47820}
    assert parse_address("[fe80::1%en0]:47820") == {"host": "fe80::1%en0", "port": 47820}
    assert parse_address("pixel") is None and parse_address("ba760a41") is None
    for bad in ("fe80::1", "::1", "host:port"):
        with pytest.raises(SystemExit):
            parse_address(bad)


async def test_pair_rerun_waits_for_an_already_accepted_pairing(make_daemon, make_phone):
    other = await make_phone(auto_accept=None)
    daemon = await make_daemon()
    assert (await orbit(daemon, "connect", f"127.0.0.1:{other.port}"))[0] == 0
    code, out = await orbit(daemon, "pair", "--accept", "--timeout", "0.3")
    assert code == 1 and "did not confirm" in out  # accepted here, the phone has not answered yet

    # A second run picks the already-accepted pairing up again and reports the phone's answer.
    rerun = asyncio.ensure_future(orbit(daemon, "pair", "--accept", "--timeout", "5"))
    await asyncio.sleep(0.5)
    await other.respond_pairing(True)
    code, out = await rerun
    assert code == 0 and f"paired with Fake Phone ({other.device_id})" in out, out


async def test_pair_wait_for_specific_device(make_daemon, make_phone):
    busy = await make_phone(config="busy", auto_accept=None)
    late = await make_phone(config="late", auto_accept=True)
    daemon = await make_daemon()
    assert (await orbit(daemon, "connect", f"127.0.0.1:{busy.port}"))[0] == 0  # a pending pairing that is not ours
    late_id = late.device_id
    waiter = asyncio.ensure_future(orbit(daemon, "pair", "--device", late_id, "--wait", "5", "--accept"))
    await asyncio.sleep(0.5)
    await daemon.api_connect({"host": "127.0.0.1", "port": late.port})
    code, out = await waiter
    assert code == 0 and f"({late_id})" in out and "paired with" in out, out


def _interactive_pair(socket_path, keys: bytes):
    """Run `orbit pair` on a pty, answer the prompt with ``keys``; return (exit status or None if hung, output)."""
    import os
    import pty
    import signal
    import subprocess
    import time

    main_fd, child_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", "from orbit.cli import main; main()", "--socket", str(socket_path), "pair"],
        stdin=child_fd,
        stdout=child_fd,
        stderr=child_fd,
        start_new_session=True,
    )
    os.close(child_fd)
    out = b""
    deadline = time.monotonic() + 10
    while b"[y/N]" not in out and time.monotonic() < deadline:
        out += os.read(main_fd, 1024)
    if keys == b"\x03":
        proc.send_signal(signal.SIGINT)
    else:
        os.write(main_fd, keys)
    try:
        code = proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = None
    import select

    while select.select([main_fd], [], [], 0.2)[0]:
        try:
            chunk = os.read(main_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    os.close(main_fd)
    return code, out.decode(errors="replace")


async def test_interactive_prompt_handles_ctrl_c_and_ctrl_d(make_daemon, make_phone):
    phone = await make_phone(auto_accept=None)
    daemon = await make_daemon()
    assert (await orbit(daemon, "connect", f"127.0.0.1:{phone.port}"))[0] == 0
    code, out = await asyncio.to_thread(_interactive_pair, daemon.api.path, b"\x04")  # Ctrl-D
    assert code == 0 and "Traceback" not in out, out
    code, out = await asyncio.to_thread(_interactive_pair, daemon.api.path, b"\x03")  # Ctrl-C
    assert code == 130, out
    assert daemon.pairings_json()[0]["localAccepted"] is False
