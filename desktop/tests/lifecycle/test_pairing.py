"""Pairing edge cases with real processes; the phone's decisions are typed into its stdin prompt."""

import asyncio
import json
import subprocess
import time

from harness import ORBIT_CLI, Phone, Proxy

from orbit.client import ApiError


async def pending(api, dev=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ps = [p for p in await api.call("pairing.list") if dev is None or p["deviceId"] == dev]
        if ps:
            return ps
        await asyncio.sleep(0.02)
    raise TimeoutError("no pending pairing")


async def phone_prompt(p: Phone, timeout=5.0, since=0.0):
    _, line = await p.wait_line(r"Pair with .*\? Code (\d{6})", timeout, since)
    return line.split("Code ")[1][:6]


def pins(path):
    f = path / "peers.json"
    return list(json.loads(f.read_text())) if f.exists() else []


async def start_pairing(lab, phone_kw=None, via_proxy=False):
    d = await lab.daemon()
    p = await lab.phone(accept=None, **(phone_kw or {}))
    api = await lab.api(d)
    proxy = None
    port = p.port
    if via_proxy:
        proxy = Proxy(p.port)
        await proxy.__aenter__()
        port = proxy.port
    t0 = time.monotonic()
    res = await api.call("devices.connect", {"host": "127.0.0.1", "port": port})
    dev = res["id"]
    [pp] = await pending(api, dev)
    sas_phone = await phone_prompt(p, since=t0)
    assert pp["sas"] == sas_phone
    return d, p, api, dev, pp, proxy


async def test_phone_side_pairing_timeout(lab):
    d, p, api, dev, pp, _ = await start_pairing(lab, {"extra": ["--pairing-timeout", "6"]})
    t0 = time.monotonic()
    while await api.call("pairing.list"):
        await asyncio.sleep(0.05)
    gone = time.monotonic() - t0
    late = None
    try:
        await api.call("pairing.respond", {"session": pp["session"], "accept": True})
    except ApiError as e:
        late = f"{e.code}: {e.message}"
    reason = d.grep(r"session_closed")[-1][1]
    devs = await api.call("devices.list")
    print(
        f"\nphone timeout: pairing vanished after {gone:.1f}s; late accept -> {late}; closed: {reason}; "
        f"devices {[(x['id'][:8], x['state'], x['paired']) for x in devs]}"
    )
    assert 5 < gone < 8 and late and late.startswith("not_found")
    assert pins(d.config) == [] and pins(p.config) == []


async def test_laptop_side_pairing_timeout(lab):
    d, p, api, dev, pp, _ = await start_pairing(lab, {"extra": ["--pairing-timeout", "400"]})
    t0 = time.monotonic()
    while await api.call("pairing.list"):
        await asyncio.sleep(0.2)
    gone = time.monotonic() - t0
    await asyncio.sleep(0.5)
    closed = p.events("session_closed")
    print(
        f"\nlaptop timeout: laptop dropped pairing after {gone:.1f}s; phone saw "
        f"{[(round(e['_t'] - t0, 1), e['reason']) for e in closed]}"
    )
    assert 118 < gone < 123
    assert closed and closed[0]["_t"] - t0 < 123
    # The phone should learn why (a bye), not just see the TCP stream end.
    assert "bye" in closed[0]["reason"], closed[0]["reason"]


async def test_reject_on_phone(lab):
    d, p, api, dev, pp, _ = await start_pairing(lab)
    cli = await asyncio.create_subprocess_exec(
        ORBIT_CLI,
        "--socket",
        str(d.socket),
        "pair",
        "--accept",
        "--timeout",
        "20",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await asyncio.sleep(0.5)
    p.write("n\n")
    out = (await asyncio.wait_for(cli.communicate(), 25))[0].decode()
    print(f"\nreject on phone: CLI rc={cli.returncode} output={out!r}")
    assert cli.returncode == 1 and "failed" in out
    assert pins(d.config) == [] and pins(p.config) == []


async def test_reject_on_laptop(lab):
    d, p, api, dev, pp, _ = await start_pairing(lab)
    t0 = time.monotonic()
    await api.call("pairing.respond", {"session": pp["session"], "accept": False})
    _, line = await p.wait_line(r"session s\d+ closed", 5, t0)
    print(f"\nreject on laptop: phone -> {line}")
    assert pins(d.config) == [] and pins(p.config) == []
    assert not await api.call("pairing.list")


async def test_accept_on_both_simultaneously_repeated(lab):
    d = await lab.daemon()
    p = await lab.phone(accept=None)
    api = await lab.api(d)
    outcomes = []
    for i in range(10):
        t0 = time.monotonic()
        dev = (await api.call("devices.connect", {"host": "127.0.0.1", "port": p.port}))["id"]
        [pp] = await pending(api, dev)
        await phone_prompt(p, since=t0)
        # the same instant on both ends (the phone reads stdin in a thread; delay varied around zero)
        if i % 2:
            p.write("y\n")
            await api.call("pairing.respond", {"session": pp["session"], "accept": True})
        else:
            r = asyncio.create_task(api.call("pairing.respond", {"session": pp["session"], "accept": True}))
            p.write("y\n")
            await r
        for _ in range(200):
            x = await api.device(dev)
            if x and x["paired"] and x["connected"]:
                break
            await asyncio.sleep(0.02)
        for _ in range(150):
            acts = [a for a in await api.call("activities.list") if a["device"] == dev]
            if acts:
                break
            await asyncio.sleep(0.02)
        me = (await api.call("status"))["deviceId"]
        outcomes.append((x["paired"], x["connected"], me in pins(p.config), len(acts) > 0))
        await api.call("devices.forget", {"id": dev})
        await asyncio.sleep(0.3)
    print(f"\nsimultaneous accept x10: {outcomes}")
    assert all(o == (True, True, True, True) for o in outcomes), outcomes


async def test_drop_mid_pairing_before_decisions_then_retry(lab):
    d, p, api, dev, pp, proxy = await start_pairing(lab, via_proxy=True)
    t0 = time.monotonic()
    proxy.live[0].rst()
    await asyncio.sleep(1)
    left_laptop = await api.call("pairing.list")
    phone_closed = p.events("session_closed", t0)
    t1 = time.monotonic()
    dev2 = (await api.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port}))["id"]
    [pp2] = await pending(api, dev2)
    sas2 = await phone_prompt(p, since=t1)
    p.write("y\n")
    await api.call("pairing.respond", {"session": pp2["session"], "accept": True})
    await asyncio.sleep(1)
    x = await api.device(dev)
    print(
        f"\ndrop mid-pairing: laptop pendings after drop {left_laptop}; phone closed "
        f"{[e['reason'] for e in phone_closed]}; retry SAS {pp['sas']} -> {pp2['sas']} (phone {sas2}); "
        f"after retry paired={x['paired']} connected={x['connected']}"
    )
    assert left_laptop == [] and phone_closed
    assert pp2["sas"] == sas2 and pp2["sas"] != pp["sas"]
    me = (await api.call("status"))["deviceId"]
    assert x["paired"] and x["connected"] and me in pins(p.config) and dev in pins(d.config)
    await proxy.close()


async def test_drop_after_laptop_pinned_but_phone_did_not(lab):
    """Phone accepts first; laptop accepts; the laptop's decision never reaches the phone."""
    d, p, api, dev, pp, proxy = await start_pairing(lab, via_proxy=True)
    p.write("y\n")
    for _ in range(100):
        if (await pending(api, dev))[0]["remoteAccepted"]:
            break
        await asyncio.sleep(0.02)
    conn = proxy.live[0]
    conn.freeze(up=True, down=False)
    t0 = time.monotonic()
    await api.call("pairing.respond", {"session": pp["session"], "accept": True})
    await asyncio.sleep(0.3)
    right_after = await api.device(dev)
    conn.rst()
    await asyncio.sleep(8)
    later = await api.device(dev)
    states = [(round(t - t0, 2), s) for t, s in api.state_log(dev, t0)]
    warn = d.grep("no longer trusts")
    print(
        f"\nlaptop pinned, phone not: right after accept "
        f"{right_after and (right_after['paired'], right_after['state'])}; "
        f"8 s later {later and (later['paired'], later['state'])}; states {states}; "
        f"laptop pins {pins(d.config)}; phone pins {pins(p.config)}; log {[w[1][-70:] for w in warn]}"
    )
    # Both ends agree the device is not paired, and the laptop never said it was.
    assert pins(d.config) == [] and pins(p.config) == []
    assert right_after is None or not right_after["paired"]
    assert not warn and (later is None or not later.get("problem"))
    await proxy.close()


async def test_drop_after_phone_pinned_but_laptop_did_not(lab):
    """Laptop accepts first; phone accepts and pins; the phone's decision never reaches the laptop."""
    d, p, api, dev, pp, proxy = await start_pairing(lab, via_proxy=True)
    await api.call("pairing.respond", {"session": pp["session"], "accept": True})
    await asyncio.sleep(0.3)
    conn = proxy.live[0]
    conn.freeze(up=False, down=True)
    p.write("y\n")
    await p.wait_line(r"paired with", 5)
    conn.rst()
    await asyncio.sleep(1)
    laptop_view = await api.device(dev)
    phone_pins = pins(p.config)
    # recover: pair again from the laptop
    t1 = time.monotonic()
    dev2 = (await api.call("devices.connect", {"host": "127.0.0.1", "port": proxy.port}))["id"]
    [pp2] = await pending(api, dev2)
    sas2 = await phone_prompt(p, since=t1)
    p.write("y\n")
    await api.call("pairing.respond", {"session": pp2["session"], "accept": True})
    await asyncio.sleep(1)
    x = await api.device(dev)
    print(
        f"\nphone pinned, laptop not: laptop sees {laptop_view}; phone pins {phone_pins}; "
        f"re-pair prompted again on phone: {sas2 == pp2['sas']}; afterwards paired={x['paired']}"
    )
    assert x["paired"] and x["connected"]
    await proxy.close()


async def test_two_laptops_pair_with_one_phone_at_once(lab):
    d1 = await lab.daemon(name="Lap1", tag="d1")
    d2 = await lab.daemon(name="Lap2", tag="d2")
    p = await lab.phone(accept=None)
    a1, a2 = await lab.api(d1), await lab.api(d2)
    t0 = time.monotonic()
    await asyncio.gather(
        a1.call("devices.connect", {"host": "127.0.0.1", "port": p.port}),
        a2.call("devices.connect", {"host": "127.0.0.1", "port": p.port}),
    )
    [q1], [q2] = await pending(a1), await pending(a2)
    await phone_prompt(p, since=t0)
    await a1.call("pairing.respond", {"session": q1["session"], "accept": True})
    await a2.call("pairing.respond", {"session": q2["session"], "accept": True})
    p.write("y\n")
    for _ in range(100):
        prompts = [s.split("Code ")[1][:6] for _, s in p.grep(r"Pair with .*\? Code")]
        if len(prompts) == 2:
            break
        await asyncio.sleep(0.05)
    first, second = prompts
    await asyncio.sleep(0.2)
    p.write("y\n")
    await asyncio.sleep(1)
    dev = q1["deviceId"]
    s1, s2 = await a1.device(dev), await a2.device(dev)
    print(
        f"\ntwo laptops at once: phone prompts {first}, {second} (laptops {q1['sas']}, {q2['sas']}); "
        f"paired {s1['paired']}/{s2['paired']}; phone pins {len(pins(p.config))}"
    )
    assert {first, second} == {q1["sas"], q2["sas"]}
    assert s1["paired"] and s2["paired"] and len(pins(p.config)) == 2


async def test_one_laptop_pairs_two_phones_at_once_with_cli(lab):
    d = await lab.daemon()
    p1 = await lab.phone(tag="p1", name="Phone A")
    p2 = await lab.phone(tag="p2", name="Phone B")
    api = await lab.api(d)
    await asyncio.gather(*(api.call("devices.connect", {"host": "127.0.0.1", "port": x.port}) for x in (p1, p2)))
    r = subprocess.run(
        [ORBIT_CLI, "--socket", str(d.socket), "pair", "--accept", "--timeout", "10"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    devs = await api.call("devices.list")
    print(f"\ntwo phones at once via CLI: rc={r.returncode}\n{r.stdout}")
    assert r.returncode == 0 and sum(x["paired"] and x["connected"] for x in devs) == 2


async def test_forgotten_on_laptop_while_offline_phone_still_pins(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    from harness import pair

    dev = await pair(api, p)
    await p.stop()
    await asyncio.sleep(0.5)
    await api.call("devices.forget", {"id": dev})  # phone is gone: no unpair reaches it
    p2 = Phone(lab.root, tag="p-again", port=p.port, config=p.config, accept=None)
    lab.procs.append(p2)
    await p2.start()
    still = pins(p2.config)
    t0 = time.monotonic()
    dev2 = (await api.call("devices.connect", {"host": "127.0.0.1", "port": p2.port}))["id"]
    [pp] = await pending(api, dev2)
    sas = await phone_prompt(p2, since=t0)
    p2.write("y\n")
    await api.call("pairing.respond", {"session": pp["session"], "accept": True})
    await asyncio.sleep(1)
    x = await api.device(dev)
    print(
        f"\nlaptop forgot (phone offline): phone still pins laptop {bool(still)}; re-pair prompted on phone "
        f"{sas == pp['sas']}; paired again {x['paired'] and x['connected']}"
    )
    assert x["paired"] and x["connected"]


async def test_phone_forgot_laptop_while_disconnected(lab):
    d = await lab.daemon()
    p = await lab.phone()
    api = await lab.api(d)
    from harness import pair

    dev = await pair(api, p)
    await p.stop()
    (p.config / "peers.json").write_text("{}")  # the phone user removed the laptop while apart
    t0 = time.monotonic()
    p2 = Phone(lab.root, tag="p-again", port=p.port, config=p.config)
    lab.procs.append(p2)
    await p2.start()
    await asyncio.sleep(6)
    x = await api.device(dev)
    lat, res = await api.timed("activities.action", {"device": dev, "activity": "msg:chat", "action": "notif.1"})
    states = [(round(t - t0, 2), s) for t, s in api.state_log(dev, t0)]
    print(
        f"\nphone forgot laptop: laptop device now {x}; states {states}; action -> {res!r}; "
        f"warning {[w[1][-80:] for w in d.grep('no longer trusts')]}"
    )
    assert x is None or not x["paired"]
