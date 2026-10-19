# Frontend connectivity harness

Real `orbitd` + `orbit-fakephone` processes with a fault-injecting TCP proxy between them
(`harness.FaultProxy`: blackhole / heal / reset / refuse / listen), and the real applet UI run
offscreen by `applet_probe.py`, which records what the user sees (header, empty state, cards with
stale / error / pending, device rows, pairing dialogs, tray tooltip, menu) to `ui.jsonl` and takes
screenshots on request. Everything lives under `/tmp/conn-frontends/<run>/`; the default orbitd
socket and config dir are never touched.

```sh
cd desktop
uv pip install pytest-xdist                          # once
uv run pytest tools/conn-frontends -n 8 -q           # macOS / Linux with Qt
tools/conn-frontends/linux/run.sh tools/conn-frontends/test_mpris_linux.py -n 6 -q -rA   # Docker: MPRIS, BLE unavailable
tools/conn-frontends/linux/run.sh tools/conn-frontends/test_socket.py -n 6 -q -rA        # Docker: socket tests on Linux
```

Every scenario prints a `MEASURE <name> key=value ...` line, also appended to
`/tmp/conn-frontends/measures.log`. Tests assert what a user would expect (spec/LOCAL_API.md plus
"recover fast, never double-apply, never lie"); a failing test is a finding, a passing one is a
regression test.

| file | what |
|---|---|
| `test_states.py` | device state transitions, reconnect timing, actions during reconnecting / offline / blackhole |
| `test_applet.py` | the applet during blips, offline, blackhole, link reset, orbitd restart / kill / freeze |
| `test_applet_pairing.py` | pairing dialog when the link or orbitd drops; Connect / Disconnect buttons |
| `test_applet_more.py` | Bluetooth unavailable, silent address, frozen applet, applet before orbitd |
| `test_real_applet_entry.py` | the `orbit-applet --screenshot` entry point in each state, light and dark |
| `test_socket.py` | the local socket: `orbit watch` across restarts, deleted socket, second orbitd, hung / many clients, malformed and oversized requests, CLI against a frozen orbitd |
| `test_mpris_linux.py` | `orbit-mpris` on a real session bus (Docker) |
| `test_soak.py` | flapping link and repeated orbitd restarts: leaks and reconnect latency |
