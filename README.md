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

Or install `orbit-companion-<version>.apk` from the
[latest release](https://github.com/latekvo/orbit/releases/latest).

### Updating the Android app

The app checks the latest GitHub release when opened (at most hourly) and from its background
service about once a day. A newer version shows a card at the top of the app and an
**Updates** notification. **Update** opens the APK in the browser: once it has downloaded,
open it and confirm **Update**. The first time, Android asks you to allow the browser to
install apps, and Chrome warns that the file might be harmful. A release updates an earlier
release in place and keeps pairings; a build signed with another key (e.g. your own debug build)
must be uninstalled first. **Not now** hides that version until a newer one is released.

### Releasing

```sh
git tag v0.2.0 && git push origin v0.2.0
```

The `Release` workflow runs the tests and lint, builds the signed release APK with
versionName `0.2.0` and versionCode `major*10000 + minor*100 + patch` (minor and patch stay
below 100), and publishes the GitHub release with `orbit-companion-0.2.0.apk` attached. It signs
with the `ORBIT_KEYSTORE_B64`, `ORBIT_KEYSTORE_PASSWORD`, `ORBIT_KEY_ALIAS` and
`ORBIT_KEY_PASSWORD` secrets. Locally, `./gradlew :app:assembleRelease -PversionName=X.Y.Z`
builds `app-release-unsigned.apk` unless `ORBIT_KEYSTORE` (a path) and the other three
variables, or the `orbit.keystore`, `orbit.keystorePassword`, `orbit.keyAlias` and
`orbit.keyPassword` Gradle properties, are set. To test the updater against a local server,
build debug with `-PupdateApiUrl=http://10.0.2.2:<port>/...` (debug builds allow plain HTTP to
`10.0.2.2` and `localhost`).

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
- **Phone reset or reinstalled:** `orbit status` says so under the device; pair it again, and
  `orbit forget` the old entry if it is still listed.
- **Several daemons:** each needs its own `--config-dir` (one identity runs once) and socket
  (`ORBIT_SOCKET` / `--socket`).

## Security model

- Each device has a long-term P-256 identity key; its device id is derived from the key.
- Every session runs an ephemeral + static ECDH handshake (AES-256-GCM records), so traffic is
  encrypted and forward-secret on any transport.
- First contact requires pairing: both devices show a 6-digit code derived from the handshake
  transcript and the user confirms on both. The initiator commits to its keys up front, so a
  man-in-the-middle gets a single 1-in-a-million guess.
- After pairing, peers are pinned; an impostor without the private key cannot complete a session.
