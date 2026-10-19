# orbitd local API

`orbitd` (the desktop daemon) owns all connections to phones. Frontends (tray applet, CLI,
Linux MPRIS bridge, future GNOME/KDE widgets) talk to it over a local Unix socket.

## Socket

- `$ORBIT_SOCKET` if set, else
- Linux: `$XDG_RUNTIME_DIR/orbit/orbitd.sock` (fallback `~/.local/state/orbit/orbitd.sock`)
- macOS: `~/Library/Application Support/orbit/orbitd.sock`

Created with mode `0600`.

## Wire format

Newline-delimited JSON, one object per line, UTF-8.

```
-> {"id": 1, "method": "devices.list", "params": {}}
<- {"id": 1, "result": [...]}
<- {"id": 2, "error": {"code": "not_found", "message": "unknown device"}}
<- {"event": "activities.changed", "data": {...}}          (after "subscribe")
```

## Methods

| method | params | result |
|---|---|---|
| `status` | - | `{"version": "...", "name": "<this computer>", "deviceId": "..."}` |
| `subscribe` | - | `true`; the connection then also receives events (below), starting with one of each |
| `devices.list` | - | `[Device]` |
| `devices.connect` | `{"id"}` or `{"host", "port"}` | `{"id": "<deviceId>"}` once the handshake completes (pairing may follow); always the real 32-hex id, also when `id` was provisional |
| `devices.disconnect` | `{"id"}` | `true` |
| `devices.forget` | `{"id"}` | `true` - unpairs (sends `unpair` if connected) and removes all stored data |
| `pairing.list` | - | `[Pairing]` |
| `pairing.respond` | `{"session", "accept": bool}` | `true` |
| `activities.list` | - | `[DeviceActivity]` |
| `activities.action` | `{"device", "activity", "action", "value"?}` | `{"ok": bool, "error"?: string}` (waits up to 10 s for `action_result`) |
| `art.get` | `{"device", "key"}` | `{"mime", "data"}` (b64) |

Error codes: `bad_request`, `not_found`, `not_connected`, `not_paired`, `timeout`,
`failed`.

## Types

```jsonc
// Device
{"id": "...", "name": "Pixel 8", "platform": "android",
 "paired": true, "connected": true,
 "transport": "lan" | "ble" | null,
 "address": "192.168.1.20:47820" | null,
 "discovered": ["lan", "ble"],            // currently seen by discovery
 "provisional": false}

// Provisional devices: an unpaired phone seen only via BLE advertises just the first 8 bytes
// of its deviceId (PROTOCOL.md 1.2). It is listed with
//   "id": the 16-hex-char prefix, "provisional": true, "paired": false, "connected": false,
//   "platform": "", "discovered": ["ble"],
//   "name": the advertised local name, else "Orbit device <first 4 hex chars>".
// devices.connect {"id": "<prefix>"} connects over BLE and fails ("failed") unless the
// responder's real id starts with the prefix. As soon as the real id is known (handshake, or
// the same device appearing via mDNS) the provisional entry disappears from devices.changed and
// the device is listed under its real id; follow it via the devices.connect result or the
// pairing's deviceId. Paired devices are never provisional.

// Pairing (a session waiting for the user's confirmation)
{"session": "<opaque>", "deviceId": "...", "name": "Pixel 8", "sas": "042917",
 "localAccepted": false, "remoteAccepted": true}

// DeviceActivity = protocol Activity + "device", with every timestamp converted to the
// local clock (epoch ms):
//   updatedMs, chrono.baseMs  -> local clock
//   media.positionAtMs        -> local time at which media.positionMs was valid
{"device": "<deviceId>", "id": "...", "kind": "media", ...}
```

## Events

| event | data |
|---|---|
| `devices.changed` | `{"devices": [Device]}` |
| `pairing.changed` | `{"pairings": [Pairing]}` |
| `activities.changed` | `{"activities": [DeviceActivity]}` |
