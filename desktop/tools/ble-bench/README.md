# ble-bench

Measures orbitd's Bluetooth link to a real, paired phone, over GATT and over the L2CAP channel
(spec/PROTOCOL.md 1.2.1), with orbitd's own code: each run starts a separate `orbitd` with
`ORBIT_BLE_TRANSPORT=gatt|l2cap`, its own `--socket`, `--no-mdns` and `--config-dir` set to a copy
of your config dir (identity and the phone's pin only, LAN addresses removed), so the phone
accepts it as this computer and the session is over BLE. Your config dir is only read.

Per run it reports:

- **link**: from the phone's first advertisement to the `BLE link to ...` log line (radio link,
  service discovery and, for L2CAP, reading the PSM and opening the channel);
- **snapshot** and **snapshot+art**: from the link to the first activities, and to the last new
  art image (art counts as complete once no new image arrived for `--settle` s);
- **bytes in** phone -> laptop over that link (orbitd's `... ended after ...` log line) and
  **KB/s** = bytes in / snapshot+art time;
- **ping** median / p90 / min / max over `--pings` protocol pings (`devices.ping`), on a second
  link opened after the first, once the art is cached and the phone sends nothing else.

Then the medians over runs per transport. Runs alternate between the transports.

## Running it (macOS)

1. On the phone: Orbit installed with L2CAP support, paired with this computer, and something
   playing (a media activity with artwork), so there is a snapshot and art to transfer.
2. Stop the everyday orbitd and the applet (quit the applet; Ctrl-C a terminal orbitd). Two
   orbitds would share the Mac's one connection to the phone, and the phone would see two sessions.
3. Keep Wi-Fi as it is: the bench's orbitd does not use the LAN.
4. From `desktop/`:

```sh
uv run python tools/ble-bench/bench.py --config-dir ~/Library/Application\ Support/orbit
```

Options: `--transport gatt,l2cap` (default; also `auto`), `--runs 3`, `--pings 50`, `--settle 10`,
`--device <id prefix or name>` when several phones are paired, `--json results.json`.
Each run's orbitd log is kept in the `/tmp/orbit-bench-*` directory the bench prints first.

A forced transport does not fall back: `l2cap` fails the run if the phone offers no channel, and the
connect error (`no L2CAP channel (ORBIT_BLE_TRANSPORT=l2cap): ...`) is in that run's log.
