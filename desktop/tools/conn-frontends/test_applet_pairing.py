"""Pairing and device-row actions in the applet while the link or the daemon misbehaves."""

import asyncio
import time

from harness import measure


async def open_pairing(world, a):
    """Start pairing the way `orbit connect host:port` does; the applet shows the dialog."""
    task = asyncio.create_task(world.cli("connect", f"127.0.0.1:{world.proxy.port}"))
    s = await a.until(lambda s: s["dialogs"], 10)
    return task, s


async def test_pairing_dialog_when_link_drops(world):
    w = world
    from harness import FaultProxy, Proc, bin_path

    # a phone that waits for its user to confirm the code (no --auto-accept)
    w.phone = await Proc("fakephone", [bin_path("orbit-fakephone"), "--no-mdns", "--port", "0", "--host", "127.0.0.1",
                                       "--config-dir", str(w.dir / "phone"), "--name", "Test Phone"],
                         w.dir / "phone2.log").start()
    m = await w.phone.wait_log(r"listening on 127\.0\.0\.1:(\d+)")
    w.proxy = FaultProxy(int(m.group(1)))
    await w.proxy.listen()
    await w.start_daemon()
    a = await w.applet()
    await a.until(lambda s: s["connected"], 10)
    task, s = await open_pairing(w, a)
    await a.shot("pairing-dialog")
    a.cmd("accept")
    s = await a.until(lambda s: s["dialogs"] and "Waiting" in s["dialogs"][0]["state"], 5)
    await a.shot("pairing-waiting")
    t = time.monotonic()
    w.proxy.reset()
    gone = await a.until(lambda s: not s["dialogs"], 10)
    await asyncio.sleep(0.5)
    a.cmd("devices")
    await asyncio.sleep(0.3)
    await a.shot("pairing-after-drop")
    cur = a.now()
    task.cancel()
    measure("pairing_link_drop", dialog_closed_after=round(gone["m"] - t, 2), header=cur["header"],
            rows=cur["rows"], devices_message=cur["devicesMessage"], waiting_state=s["dialogs"][0]["state"])
    # The user accepted and is waiting; the attempt failed. Something must say so.
    assert cur["devicesMessage"] or any("fail" in str(x).lower() for x in cur.values())
    assert [d["headline"] for d in cur["failedDialogs"]] == ["Pairing with Test Phone failed"]
    assert "connection was lost" in cur["failedDialogs"][0]["hint"]
    # "Try again" starts a new pairing session, shown in a fresh dialog
    a.cmd("retry")
    s = await a.until(lambda s: s["dialogs"] and not s["failedDialogs"], 15)
    measure("pairing_link_drop_retry", dialog=s["dialogs"][0])


async def test_connect_button_while_phone_unreachable(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["cards"])
    await w.proxy.refuse()
    w.proxy.reset()
    await a.until(lambda s: s["rows"] and s["rows"][0]["status"] == "Offline", 25)
    a.cmd("devices")
    t = time.monotonic()
    a.cmd(f"devbtn {w.phone_id[:8]} Connect")
    s = await a.until(lambda s: s["devicesMessage"] or (s["rows"] and s["rows"][0].get("error")), 20)
    busy = a.first(lambda s: s["rows"] and s["rows"][0]["buttons"].get("Connect", ["", True])[1] is False, t) or s
    await a.shot("connect-unreachable")
    measure("connect_unreachable", busy_label=busy["rows"][0]["buttons"]["Connect"][0],
            message=s["devicesMessage"], row_error=s["rows"][0].get("error"), after=round(s["m"] - t, 2))
    message = s["devicesMessage"] or s["rows"][0]["error"]
    # then the phone comes back; the user clicks Connect once more
    await w.proxy.listen()
    t = time.monotonic()
    a.cmd(f"devbtn {w.phone_id[:8]} Connect")
    s2 = await a.until(lambda s: s["cards"] and not s["cards"][0]["stale"], 20)
    measure("connect_after_back", after=round(s2["m"] - t, 2), header=s2["header"])
    assert "Errno" not in message, f"raw Python exception text shown to the user: {message!r}"
    assert (s["rows"][0].get("error"), s["devicesMessage"]) == ("Test Phone isn't reachable", None)


async def test_disconnect_while_reconnecting(paired):
    w = paired
    a = await w.applet()
    await a.until(lambda s: s["cards"])
    await w.proxy.refuse()
    w.proxy.reset()
    await a.until(lambda s: s["rows"] and s["rows"][0]["status"] == "Reconnecting...", 5)
    a.cmd("devices")
    a.cmd(f"devbtn {w.phone_id[:8]} Disconnect")
    s = await a.until(lambda s: s["rows"] and s["rows"][0]["status"] == "Offline", 5)
    await w.proxy.listen()
    await asyncio.sleep(5)
    st = (await w.device())["state"]
    measure("disconnect_reconnecting", ui=s["rows"], daemon_state_after_phone_back=st, msg=s["devicesMessage"])
    assert st == "offline"


async def test_daemon_restart_during_pairing(world):
    w = world
    from harness import Proc, bin_path, FaultProxy
    w.phone = await Proc("fakephone", [bin_path("orbit-fakephone"), "--no-mdns", "--port", "0", "--host", "127.0.0.1",
                                       "--config-dir", str(w.dir / "phone"), "--name", "Test Phone"],
                         w.dir / "phone.log").start()
    m = await w.phone.wait_log(r"listening on 127\.0\.0\.1:(\d+)")
    w.proxy = FaultProxy(int(m.group(1)))
    await w.proxy.listen()
    await w.start_daemon()
    a = await w.applet()
    await a.until(lambda s: s["connected"], 10)
    task, s = await open_pairing(w, a)
    await w.restart_daemon()
    s = await a.until(lambda s: s["connected"] and not s["dialogs"], 15)
    await asyncio.sleep(1)
    cur = a.now()
    task.cancel()
    measure("pairing_daemon_restart", dialogs=cur["dialogs"], rows=cur["rows"], msg=cur["devicesMessage"],
            header=cur["header"], failed=cur["failedDialogs"])
    assert [(d["headline"], d["hint"], d["retry"]) for d in cur["failedDialogs"]] == [
        ("Pairing with Test Phone failed", "orbitd stopped before pairing finished.", True)]
