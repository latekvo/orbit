# Orbit Protocol v1

Orbit mirrors a phone's *live activities* (ongoing, interactive notifications: media
playback, timers, downloads, navigation, calls, ...) to nearby computers, and lets the
computer trigger the activity's actions. There is no server: devices talk directly over
the local network or Bluetooth LE.

Roles:

- **Responder (R)** - the device that owns activities (the phone). Listens/advertises.
- **Initiator (I)** - the device that displays them (the laptop). Discovers and connects.

Everything above the transport is identical for every transport.

## 1. Transports

Every transport provides one reliable, ordered, bidirectional **byte stream per session**.

### 1.1 LAN (TCP)

- R listens on TCP port `47820` (falls back to any free port if taken).
- R advertises DNS-SD service type `_orbit._tcp` (mDNS, `local.`), instance name = human
  device name, TXT records:
  - `v=1` - the protocol versions R speaks, comma-separated (`v=1,2`); I ignores a
    responder that lists none it speaks
  - `id=<deviceId>` - see 3.1
  - `pf=<platform>` - `android`, `linux`, `macos`, ...
- I may also connect to a manually entered `host:port` (used for emulators behind
  `adb forward`, or networks that block multicast).

### 1.2 Bluetooth LE (GATT)

R is the GATT peripheral, I is the central.

| Item | UUID |
|---|---|
| Service | `616dfa5c-069e-4839-8335-87edbeb2c1a5` |
| RX characteristic (I -> R, *write with response*) | `616dfa5d-069e-4839-8335-87edbeb2c1a5` |
| TX characteristic (R -> I, *notify*) | `616dfa5e-069e-4839-8335-87edbeb2c1a5` |

- Advertisement: the service UUID. Scan response: Service Data for the service UUID
  carrying the first 8 bytes of the raw deviceId (16 hex chars -> 8 bytes), plus the
  local name if it fits.
- A session starts when I enables notifications on TX and ends when the link drops.
- The byte stream is split into chunks of at most `ATT_MTU - 3` bytes (min 20, max 512).
  Chunk boundaries carry no meaning; the receiver concatenates chunks into the stream.
- R must wait for `onNotificationSent` (or equivalent) before sending the next chunk.

## 2. Framing

The stream is a sequence of frames:

```
uint32 big-endian  length of body (1 .. 1048576)
bytes[length]      body
```

A frame with length 0 or > 1 MiB is a protocol error (close the stream). Handshake frames
(3.2) are at most 4096 bytes; a longer one is a protocol error too.
Handshake bodies are UTF-8 JSON. After the handshake every body is an encrypted record
(section 4) whose plaintext is UTF-8 JSON.

Every JSON message is an object with a string field `t` (type). Receivers ignore unknown
fields; unknown `t` values in the encrypted phase are ignored (forward compatibility).

## 3. Handshake

### 3.1 Identity

Each device has a long-term **static** ECDH key pair on NIST P-256.

- Public keys are encoded as the 65-byte uncompressed SEC1 point (`0x04 || X || Y`),
  base64 (standard alphabet, padded) in JSON.
- `deviceId` = lowercase hex of the first 16 bytes of `SHA-256(static public key bytes)`
  (32 hex chars). Peers MUST verify that a peer's `id` matches its `static` key.

### 3.2 Messages (plaintext frames)

```
1. I -> R  commit   {"t":"commit","v":1,"c":"<b64 SHA-256(body of I's hello frame)>"}
2. R -> I  hello    {"t":"hello","v":1,"role":"responder", ...}
3. I -> R  hello    {"t":"hello","v":1,"role":"initiator", ...}
```

Hello fields: `id`, `name` (human device name), `platform`, `static` (b64 public key),
`eph` (b64 fresh ephemeral public key, new for every session), `nonce` (b64 of 16 random
bytes).

The initiator commits to its hello before seeing the responder's keys; this is what makes
the 6-digit pairing code (3.4) resistant to a man-in-the-middle that grinds keys.

Checks (failure -> close the stream, optionally after a plaintext
`{"t":"error","code":"...","message":"..."}`):

- R: `commit.v` supported, else error `unsupported_version` with `"supported":[1]`.
- R: `SHA-256(body of I's hello frame) == commit.c` - hash the **exact bytes received**,
  never a re-serialization.
- Both: `role` is the expected one, `id` matches `static`, keys decode to valid P-256
  points, `nonce` is 16 bytes.

### 3.3 Key schedule

```
lp(x)     = uint32_be(len(x)) || x
th        = SHA-256("orbit/1 transcript" || lp(commit_body) || lp(hello_R_body) || lp(hello_I_body))
dh_ee     = ECDH(eph_I, eph_R)          32-byte X coordinate
dh_ss     = ECDH(static_I, static_R)    32-byte X coordinate
okm       = HKDF-SHA256(salt = th, ikm = dh_ee || dh_ss, info = "orbit/1 session keys", L = 64)
k_i2r     = okm[0:32]
k_r2i     = okm[32:64]
sas       = uint32_be(SHA-256("orbit/1 sas" || th)[0:4]) mod 1000000, as 6 digits zero-padded
```

All string constants are ASCII without a terminator. `*_body` are the exact frame bodies as
sent/received.

`dh_ee` gives forward secrecy; `dh_ss` binds the session to both static keys, so once a peer
is pinned (3.4) an impostor without its private key cannot derive the session keys and the
first encrypted frame fails to decrypt.

### 3.4 Pairing and trust

Each device keeps a set of **pinned peers**: `deviceId -> static public key` (+ name).

Immediately after the key schedule, each side sends an encrypted
`{"t":"ready","paired":<bool>,"now":<epoch ms>}` where `paired` says whether it has the
peer's static key pinned. A session is **paired** iff both sides send `paired:true`.
I's `ready` MAY carry `"art": [<key>, ...]`, the art keys it still holds from earlier sessions;
R SHOULD then not resend those (they count as sent in this session, see `art` in 5), so a
reconnect's snapshot is not held up behind art the laptop already has.
An initiator that connects to a specific known device MUST check that the responder's `id`
is the expected one; a different `id` is a different, unpaired device.

If the session is not paired, only these messages are valid: `ready`, `pair_request`,
`pair_decision`, `ping`, `pong`, `bye`, `error`. Anything else gets
`{"t":"error","code":"not_paired"}`.

Pairing:

1. I sends `{"t":"pair_request"}`.
2. Both devices display the 6-digit `sas` and ask their user to confirm it matches the
   code on the other device.
3. Each side sends `{"t":"pair_decision","accepted":<bool>}` once its user decides.
4. When a side has sent *and* received `accepted:true` it pins the peer, and the session
   becomes paired (R then sends `snapshot`). Any `accepted:false` -> both close.
5. Pairing that is not completed within 120 s is abandoned (close).

R closes a session that is neither paired nor pairing 30 s after the handshake.

`{"t":"unpair"}` from either side: the receiver removes the pin and closes.

## 4. Encrypted records

After the key schedule every frame body is `AES-256-GCM(key, nonce, plaintext, aad = empty)`
= ciphertext || 16-byte tag.

- I -> R uses `k_i2r`, R -> I uses `k_r2i`.
- `nonce` = 4 zero bytes || uint64_be(counter); one counter per direction, starting at 0,
  incremented after every record. The receiver tracks the expected counter implicitly; any
  authentication failure closes the stream.

## 5. Application messages (encrypted, paired sessions only)

Time: every message that carries timestamps has `now` = sender's epoch ms at send time.
The receiver estimates `offset = local_now - now` and converts sender-clock timestamps to
its own clock. Media positions are valid at the message's `now`.

| `t` | Dir | Fields |
|---|---|---|
| `snapshot` | R->I | `now`, `activities: [Activity]` - full replacement of the activity set |
| `upsert` | R->I | `now`, `activity: Activity` - add or replace by `id` |
| `remove` | R->I | `id` |
| `art` | R->I | `key`, `mime` (`image/png`/`image/jpeg`), `data` (b64). Sent before the first activity that references `key` in this session. `key` = first 16 hex chars of SHA-256(data). Images are at most 256x256. |
| `action` | I->R | `req` (int, unique per session), `activity` (id), `action` (action id), `value` (optional; see below) |
| `action_result` | R->I | `req`, `ok` (bool), `error` (optional string) |
| `ping` / `pong` | both | `req` (int). Send `ping` after 10 s without sending anything; close after 30 s without receiving anything (any byte counts, so a large frame still arriving keeps the session alive). I also closes when one of its pings goes unanswered for 10 s, or an `action` for 5 s (an answer is the `pong`, the `action_result`, or data still arriving at that moment), so a silently dead link is noticed before the user's click could be delivered late. A peer still on the older 20 s / 60 s stays connected: the other side's pings draw its pongs. |
| `bye` | both | `reason` (optional). Sender closes after sending. |
| `error` | both | `code`, `message` |

### 5.1 Activity

```jsonc
{
  "id": "media:com.spotify.music",       // stable for the activity's lifetime
  "kind": "media",                       // media | timer | progress | call | navigation | generic
  "app": {"id": "com.spotify.music", "name": "Spotify"},
  "title": "Song title",
  "subtitle": "Artist",                  // optional
  "text": "Album",                       // optional
  "iconKey": "1a2b3c4d5e6f7a8b",         // optional, art key of the app icon
  "artKey": "8b7a6f5e4d3c2b1a",          // optional, art key of the large image
  "color": "#1db954",                    // optional accent
  "progress": {"value": 40, "max": 100, "indeterminate": false},          // optional
  "media": {"state": "playing", "positionMs": 61000, "durationMs": 215000,
            "rate": 1.0, "seekable": true},                               // optional
  "chrono": {"baseMs": 1760000000000, "countDown": false, "running": true}, // optional
  "actions": [Action],
  "updatedMs": 1760000000000
}
```

- `media.state`: `playing | paused | buffering | stopped`.
- `chrono`: a running clock shown as text. Count-up: elapsed = now - baseMs. Count-down:
  remaining = baseMs - now. When `running` is false, the clock shows the fixed value in
  `chrono.valueMs` instead.

### 5.2 Action

```jsonc
{"id": "media.pause", "label": "Pause", "icon": "pause",
 "input": {"kind": "text", "label": "Reply"},   // input optional
 "active": true,                                // optional, toggles only
 "overflow": true}                              // optional, default false
```

`active` (optional bool): the current state of a toggle action (like, shuffle, repeat, mute...),
`true` = currently on. Producers SHOULD set it whenever they know the state, because labels
are ambiguous across apps ("Shuffle on" may name the current state or the action to take).
Consumers MUST prefer it over anything inferred from the label.

`overflow: true` means the phone's own UI does not show this action as a button. Consumers
SHOULD put such actions behind an overflow ("More") menu rather than inline, keeping the
listed order. `open` is the activity's primary tap target: bind it to clicking the card or
its header, not to a button. `dismiss` belongs in the overflow menu whether or not it is
marked.

Well-known `icon` values: `play`, `pause`, `next`, `previous`, `stop`, `close`, `reply`,
`add`, `remove`, `check`, `mute`, `unmute`, `like`, `dislike`, `shuffle`, `repeat`,
`call_end`. Unknown icons fall back to the label.

Well-known action ids:

| id | meaning | `value` |
|---|---|---|
| `media.play`, `media.pause`, `media.next`, `media.previous`, `media.stop` | transport controls | - |
| `media.seek` | seek; available when `media.seekable` (need not be listed in `actions`) | position ms (int) |
| `open` | open the activity on the phone | - |
| `dismiss` | dismiss the activity (only offered if dismissable) | - |
| `notif.<n>` | the n-th action of the source notification | text if the action has `input` |
