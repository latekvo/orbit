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

## Security model

- Each device has a long-term P-256 identity key; its device id is derived from the key.
- Every session runs an ephemeral + static ECDH handshake (AES-256-GCM records), so traffic is
  encrypted and forward-secret on any transport.
- First contact requires pairing: both devices show a 6-digit code derived from the handshake
  transcript and the user confirms on both. The initiator commits to its keys up front, so a
  man-in-the-middle gets a single 1-in-a-million guess.
- After pairing, peers are pinned; an impostor without the private key cannot complete a session.
