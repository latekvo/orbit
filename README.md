# Orbit

See your phone's live activities - now playing, timers, downloads, navigation, calls - on your
laptop, and control them from there. No server, no account, no Internet: devices talk directly
over the local network (mDNS + TCP) or Bluetooth LE.

```
 Android phone                                   Laptop (Linux / macOS)
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ NotificationListener      │                   │ orbitd (daemon)              │
 │ MediaSessionManager       │  LAN: mDNS + TCP  │  ├─ discovery (mDNS, BLE)    │
 │        │                  │◄─────────────────►│  ├─ pairing + encryption     │
 │ Orbit companion           │  BLE: GATT stream │  └─ local API (Unix socket)  │
 │  (Responder: advertises,  │                   │        ▲          ▲          │
 │   pairs, streams          │                   │  orbit-applet   orbit CLI    │
 │   activities, runs        │                   │  (tray/menu     MPRIS bridge │
 │   actions)                │                   │   bar popover)  (Linux)      │
 └───────────────────────────┘                   └──────────────────────────────┘
```

| Path | What |
|---|---|
| `spec/PROTOCOL.md` | Wire protocol v1: transports, framing, handshake, pairing, encryption, activity model |
| `spec/LOCAL_API.md` | Daemon <-> frontend API on the local Unix socket |
| `spec/test-vectors.json` | Deterministic handshake vectors every implementation must reproduce |
| `desktop/` | Python: reference protocol core, `orbitd`, `orbit` CLI, `orbit-applet`, `orbit-fakephone` |
| `android/companion/` | Android companion app (the phone side) |
| `android/sample-producer/` | Test app that produces realistic live activities on an emulator |

## Setup

### Laptop (macOS or Linux)

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
cd desktop
uv sync --extra applet          # add --no-group qt where PySide6 has no wheels (e.g. Linux aarch64, glibc < 2.39)
uv run orbitd --name "My Laptop" &   # the daemon; keep it running (login item / systemd user unit)
uv run --extra applet orbit-applet & # tray / menu-bar icon
uv run orbit-mpris &                  # Linux only: phone media in GNOME/KDE media controls
```

On macOS the first BLE scan triggers a Bluetooth permission prompt for the terminal (or
whatever launches `orbitd`); without it Orbit falls back to Wi-Fi only.

### Phone (Android 10+)

```sh
cd android/companion
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Open **Orbit**, then grant notification access, notifications and Bluetooth, and accept the
battery-optimization exemption. On ColorOS, MIUI, EMUI and One UI also follow the in-app hint
to allow auto-start, or the OS will kill the companion in the background.

### Pairing

1. Keep both devices on the same Wi-Fi, or within Bluetooth range.
2. In the applet open **Devices** and click **Pair** next to the phone, or run `uv run orbit devices` then
   `uv run orbit connect <name or id prefix>`.
3. Both screens show a 6-digit code. If the codes match, accept on the phone and on the
   laptop (applet dialog, or `uv run orbit pair --accept`).

Paired devices reconnect automatically: over Wi-Fi when both are on the same network,
otherwise over Bluetooth LE.

### Everyday use

| Command | What |
|---|---|
| `orbit status` | daemon and per-device state |
| `orbit activities` | what the phone is showing right now |
| `orbit action <device> <activity> <action-id> [value]` | press a button (e.g. `media.next`) |
| `orbit watch` | stream events as JSON lines |
| `orbit forget <device>` | unpair |

### Troubleshooting

- **Phone not discovered over Wi-Fi:** guest and corporate networks often block mDNS or
  device-to-device traffic (client isolation). Bluetooth still works; or use
  `orbit connect <phone-ip>:47820`.
- **Cards grey out and the applet says "reconnecting":** the link dropped and Orbit is
  reconnecting; cards stay greyed out for up to 15 s. Frequent drops usually mean the OS is
  killing the companion: check the battery and auto-start settings above.
- **Diagnostics:** `orbitd -v` logs every session close with its cause; the phone app's
  **Diagnostics** section shows the event log (copy/share it when reporting a bug).
- **Several daemons:** `ORBIT_SOCKET` / `--socket` and `--config-dir` select an instance.

## Security model

- Each device has a long-term P-256 identity key; its device id is derived from the key.
- Every session runs an ephemeral + static ECDH handshake (AES-256-GCM records), so traffic is
  encrypted and forward-secret on any transport.
- First contact requires pairing: both devices show a 6-digit code derived from the handshake
  transcript and the user confirms on both. The initiator commits to its keys up front, so a
  man-in-the-middle gets a single 1-in-a-million guess.
- After pairing, peers are pinned; an impostor without the private key cannot complete a session.
