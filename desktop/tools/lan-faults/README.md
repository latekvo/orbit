# LAN fault lab

Real `orbitd` (laptop) and `orbit-fakephone` or the Kotlin `ResponderSession` (phone) in separate
Docker network namespaces; faults are iptables / tc netem / `docker network` / signals.

```sh
desktop/tools/lan-faults/jvm/build.sh      # only for test_kotlin_phone.py
cd desktop
uv run --with pytest-xdist pytest tools/lan-faults -n 6 -m "not slow"   # ~9 min
uv run --with pytest-xdist pytest tools/lan-faults -n 6 -m slow         # ~8 min
```

Each test writes what a user would observe to `results/<test>.json` and fails when orbitd does
not do what spec/LOCAL_API.md promises or a user can expect (recover within 10 s of the network
coming back, never report a false outcome). `agent/` is mounted into the containers:
`orbitd.py` (the real entry point plus a 1 s stats dump; `ORBIT_SLEEP_FILE` hides a simulated
suspend from `time.monotonic`), `phone.py` (FakePhone with a control socket), `rst.py` (forged
RST), `browse.py` (independent mDNS browser used as a control).
