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
| `status` | - | `{"version": "...", "name": "<this computer>", "deviceId": "...", "lan": "off" \| "starting" \| "browsing" \| "unavailable", "lanError": string \| null, "ble": "off" \| "starting" \| "scanning" \| "unavailable", "bleError": string \| null}` - `lan` is mDNS discovery: `"unavailable"` (e.g. UDP 5353 cannot be bound; `lanError` says why) is retried every 10 s, while BLE and manual addresses keep working. BLE `"unavailable"` (Bluetooth off, no adapter or no permission; `bleError` says why) is retried with backoff up to every 5 s; switching Bluetooth off while scanning also makes it `"unavailable"` until it is back on |
| `subscribe` | - | `true`; the connection then also receives events (below), starting with one of each |
| `devices.list` | - | `[Device]` |
| `devices.connect` | `{"id"}` or `{"host", "port"}` | `{"id": "<deviceId>"}` once the handshake completes (pairing may follow); always the real 32-hex id, also when `id` was provisional. An already connected device's session is first checked (it must answer within 4 s, else it is replaced). Answers within 12 s (else `timeout`, listing the addresses that failed) |
| `devices.disconnect` | `{"id"}` | `true`; also stops a `"reconnecting"` device from reconnecting, including a connection attempt already under way |
| `devices.forget` | `{"id"}` | `true` - unpairs (sends `unpair` if connected) and removes all stored data |
| `pairing.list` | - | `[Pairing]` |
| `pairing.respond` | `{"session", "accept": bool}` | `true` |
| `activities.list` | - | `[DeviceActivity]` |
| `activities.action` | `{"device", "activity", "action", "value"?}` | `{"ok": bool, "error"?: string}`; answers within 12 s (below) |
| `art.get` | `{"device", "key"}` | `{"mime", "data"}` (b64) |

Error codes: `bad_request`, `not_found`, `not_connected`, `not_paired`, `timeout`,
`failed`.

Every call answers within 30 s (else `timeout`, "orbitd gave up after 30 s"); a client
timeout should be longer than that, or than the method's own bound.

`activities.action` answers within 12 s. On a `"reconnecting"` device it first waits up to
5 s for the session to come back; after the computer wakes from sleep it waits until the
session has proved alive (or was replaced). An action is re-sent on a new session only if the
phone cannot have received the whole message (part of it never left this computer; the old
connection is then reset so that it cannot arrive later), so a toggle is never applied twice.
A session that neither answers an action nor is still sending data 5 s later is dropped.
Errors:
- `not_connected` `"reconnecting"`: the session was not back within 5 s; not sent;
- `not_connected` `"phone offline"` (or the device's `problem`): the device is `"offline"`; not sent;
- `failed` `"connection lost after the action was sent; it may have been applied"`;
- `timeout` `"phone did not respond; it may still be applied"`: no `action_result` within the
  bound.

## Connecting

orbitd keeps every paired device connected that was not disconnected with `devices.disconnect`.
It tries the device's LAN addresses (the one that last worked first, then the advertised ones:
IPv4, routable IPv6, link-local IPv6) a quarter second apart, without waiting for each to time
out, and BLE 2 s after (at once when every LAN address failed within the last minute); the first
session wins. After a session that carried the phone's snapshot it reconnects at once; failed
attempts back off up to 2 s while the device is `"reconnecting"`, up to 5 s for 5 minutes, then
up to 30 s. A re-announcement over mDNS, a BLE advertisement after a gap, a change of this
computer's network interfaces and waking from sleep each retry at once. Sessions are checked
(a `ping` that must draw an answer within a few seconds) when the network changes, when the
phone announces new addresses, and when the computer wakes, with the activities shown as
`"stale"` until the check passes.

## Types

```jsonc
// Device
{"id": "...", "name": "Pixel 8", "platform": "android",
 "paired": true, "connected": true,
 "state": "connected" | "reconnecting" | "offline",
 "transport": "lan" | "ble" | null,
 "address": "192.168.1.20:47820" | null,
 "discovered": ["lan", "ble"],            // currently seen by discovery
 "provisional": false,
 "problem": null}                         // or why it cannot be used as it is, for the user, e.g.
                                          // its address now answers as another device (a reset or
                                          // reinstalled phone; pairing it again under the same name
                                          // removes this entry), the phone unpaired this computer, or
                                          // its manually entered address has not answered for 1 min.

// state: "reconnecting" for up to 15 s after a paired device's session dropped while orbitd
// keeps trying to get it back ("connected" is false); its last activities stay listed with
// "stale": true meanwhile. "offline" otherwise when not connected.

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
// When the local clock is stepped, these follow it.
// "stale": true from the moment the session dropped (the device is "reconnecting") until the
// reconnected phone's snapshot replaces them; if the device does not come back within the
// grace period they are removed. Also true after the computer wakes from sleep, until the
// session proved alive.
{"device": "<deviceId>", "id": "...", "kind": "media", ..., "stale": false}
```

## Events

| event | data |
|---|---|
| `devices.changed` | `{"devices": [Device]}` |
| `pairing.changed` | `{"pairings": [Pairing]}` |
| `activities.changed` | `{"activities": [DeviceActivity]}` |
