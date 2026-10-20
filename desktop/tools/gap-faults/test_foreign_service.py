"""Something that is not an Orbit responder answers on the address the user (or a stale record) points at:
an HTTP service, a captive-portal / transparent proxy that accepts every SYN, a tarpit."""

import time

SERVERS = r"""cat > /tmp/foreign.py <<'EOF'
import asyncio

async def http(r, w):
    await asyncio.sleep(0.05)
    w.write(b"HTTP/1.1 302 Found\r\nLocation: http://portal.example/\r\nContent-Length: 0\r\n\r\n")
    await w.drain()
    await asyncio.sleep(30)
    w.close()

async def silent(r, w):
    await asyncio.sleep(3600)

async def tarpit(r, w):
    w.write(b"\x00\x00\x0f\xa0")  # a 4000-byte frame that never finishes
    while True:
        await asyncio.sleep(1)
        w.write(b"{")
        await w.drain()

async def closer(r, w):
    w.close()

async def main():
    for port, h in ((47830, http), (47831, silent), (47832, tarpit), (47833, closer)):
        await asyncio.start_server(h, "0.0.0.0", port)
    await asyncio.sleep(10**6)

asyncio.run(main())
EOF
nohup python /tmp/foreign.py > /tmp/foreign.log 2>&1 &
sleep 0.5"""


def test_foreign_service_on_the_port(rig, record):
    r = rig(pair=False)
    r.lab.sh(r.phone, SERVERS)
    out = {}
    for name, port in (("http_302", 47830), ("silent_accept", 47831), ("tarpit", 47832), ("accept_then_close", 47833)):
        res = r.call("devices.connect", {"host": r.phone_ip, "port": port}, timeout=40)
        out[name] = {"dt": res["dt"], **(res.get("error") or {"result": res.get("result")})}
    fds = r.stats()[-1]["fds"]
    time.sleep(2)
    record(**out, orbitd_fds_after=fds, orbitd_alive=bool(r.lab.sh(r.laptop, "pgrep -f [/]harness/orbitd.py", check=False).strip()))
    for name, v in out.items():
        assert v["dt"] <= 12.5, (name, v)  # LOCAL_API: devices.connect answers within 12 s
    # A user pointed at the wrong thing should be told so, not shown a parser error.
    assert "not an orbit" in out["http_302"]["message"].lower() or "orbit" in out["http_302"]["message"].lower(), out["http_302"]
