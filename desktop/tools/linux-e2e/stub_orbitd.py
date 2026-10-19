"""Stub orbitd speaking spec/LOCAL_API.md, with one fake phone playing music.

Stdlib only. Used by tests/test_mpris.py and the Linux E2E harness.

    stub_orbitd.py serve --socket PATH
    stub_orbitd.py ctl --socket PATH METHOD [JSON_PARAMS]     # prints the JSON result

Besides the local API it answers `stub.*` control methods: `stub.calls` (recorded
`activities.action` params), `stub.remove {"id"}`, `stub.patch {"id", "set"?, "media"?}`,
`stub.device {"set"}` and `stub.reset`. Every state change is broadcast to subscribers.
"""

import argparse
import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

DEVICE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
MEDIA_ID = "media:com.spotify.music"
TIMER_ID = "timer:com.google.android.deskclock"


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + bytes(rgb) * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


def art_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


ART = [png(64, 64, (0x1D, 0xB9, 0x54)), png(64, 64, (0xE9, 0x1E, 0x63))]
TRACKS = [
    {"title": "Song One", "subtitle": "Artist A", "text": "Album X", "durationMs": 215000, "art": ART[0]},
    {"title": "Song Two", "subtitle": "Artist B", "text": "Album Y", "durationMs": 180000, "art": ART[1]},
]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class StubError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Stub:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.art_requests: list[str] = []
        self.subscribers: set[asyncio.StreamWriter] = set()
        self.server: asyncio.AbstractServer | None = None
        self.art = {art_key(a): a for a in ART}
        self.reset()

    def reset(self) -> None:
        self.devices = [
            {
                "id": DEVICE_ID,
                "name": "Pixel 8",
                "platform": "android",
                "paired": True,
                "connected": True,
                "transport": "lan",
                "address": "127.0.0.1:47820",
                "discovered": ["lan"],
            }
        ]
        self.track = 0
        self.activities = {
            MEDIA_ID: self._media("playing", 61000),
            TIMER_ID: {
                "device": DEVICE_ID,
                "id": TIMER_ID,
                "kind": "timer",
                "app": {"id": "com.google.android.deskclock", "name": "Clock"},
                "title": "Timer",
                "chrono": {"baseMs": now_ms() + 300000, "countDown": True, "running": True},
                "actions": [],
                "updatedMs": now_ms(),
            },
        }

    def _media(self, state: str, position_ms: int) -> dict:
        t = TRACKS[self.track]
        transport = "media.pause" if state == "playing" else "media.play"
        return {
            "device": DEVICE_ID,
            "id": MEDIA_ID,
            "kind": "media",
            "app": {"id": "com.spotify.music", "name": "Spotify"},
            "title": t["title"],
            "subtitle": t["subtitle"],
            "text": t["text"],
            "artKey": art_key(t["art"]),
            "media": {
                "state": state,
                "positionMs": position_ms,
                "positionAtMs": now_ms(),
                "durationMs": t["durationMs"],
                "rate": 1.0,
                "seekable": True,
            },
            "actions": [{"id": a, "label": a} for a in ("media.previous", transport, "media.next", "open")],
            "updatedMs": now_ms(),
        }

    def _position(self, act: dict) -> int:
        m = act["media"]
        pos = m["positionMs"]
        if m["state"] == "playing":
            pos += now_ms() - m["positionAtMs"]
        return min(int(pos), m["durationMs"])

    def broadcast(self, event: str, data: object) -> None:
        line = (json.dumps({"event": event, "data": data}) + "\n").encode()
        for w in list(self.subscribers):
            if w.is_closing():
                self.subscribers.discard(w)
            else:
                w.write(line)

    def _activities_changed(self) -> None:
        self.broadcast("activities.changed", {"activities": list(self.activities.values())})

    def _act(self, params: dict) -> dict:
        act = self.activities.get(params.get("activity") or params.get("id"))
        if act is None:
            raise StubError("not_found", "unknown activity")
        return act

    def action(self, params: dict) -> dict:
        self.calls.append(params)
        act = self._act(params)
        action = params["action"]
        if act["id"] != MEDIA_ID:
            return {"ok": False, "error": "unsupported"}
        pos = self._position(act)
        state = act["media"]["state"]
        if action == "media.play":
            new = self._media("playing", pos)
        elif action == "media.pause":
            new = self._media("paused", pos)
        elif action == "media.stop":
            new = self._media("stopped", 0)
        elif action in ("media.next", "media.previous"):
            self.track = (self.track + (1 if action == "media.next" else -1)) % len(TRACKS)
            new = self._media(state, 0)
        elif action == "media.seek":
            new = self._media(state, int(params["value"]))
        elif action == "open":
            return {"ok": True}
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        self.activities[MEDIA_ID] = new
        self._activities_changed()
        return {"ok": True}

    def handle(self, method: str, params: dict) -> object:
        if method == "status":
            return {"version": "stub", "name": "stub", "deviceId": "0" * 32}
        if method == "devices.list":
            return self.devices
        if method == "pairing.list":
            return []
        if method == "activities.list":
            return list(self.activities.values())
        if method == "activities.action":
            return self.action(params)
        if method == "art.get":
            self.art_requests.append(params.get("key"))
            data = self.art.get(params.get("key"))
            if params.get("device") != DEVICE_ID or data is None:
                raise StubError("not_found", "unknown art")
            return {"mime": "image/png", "data": base64.b64encode(data).decode()}
        if method == "stub.calls":
            return self.calls
        if method == "stub.remove":
            self.activities.pop(params["id"], None)
        elif method == "stub.patch":
            act = self._act(params)
            act.update(params.get("set", {}))
            act["media"].update(params.get("media", {}))
            if "positionMs" in params.get("media", {}):
                act["media"]["positionAtMs"] = now_ms()
        elif method == "stub.device":
            self.devices[0].update(params["set"])
            self.broadcast("devices.changed", {"devices": self.devices})
            return True
        elif method == "stub.reset":
            self.reset()
        else:
            raise StubError("bad_request", f"unknown method {method}")
        self._activities_changed()
        return True

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                req = json.loads(line)
                reply: dict = {"id": req.get("id")}
                try:
                    if req.get("method") == "subscribe":
                        reply["result"] = True
                    else:
                        reply["result"] = self.handle(req.get("method"), req.get("params") or {})
                except StubError as e:
                    reply["error"] = {"code": e.code, "message": str(e)}
                writer.write((json.dumps(reply) + "\n").encode())
                if req.get("method") == "subscribe":
                    self.subscribers.add(writer)
                    self.broadcast("devices.changed", {"devices": self.devices})
                    self.broadcast("pairing.changed", {"pairings": []})
                    self._activities_changed()
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            self.subscribers.discard(writer)
            writer.close()

    async def start(self, path: str | Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        self.server = await asyncio.start_unix_server(self._on_conn, str(path))
        os.chmod(path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            for w in list(self.subscribers):
                w.close()
            self.subscribers.clear()
            await self.server.wait_closed()


async def _serve(path: str) -> None:
    stub = Stub()
    await stub.start(path)
    print(f"stub orbitd listening on {path}", flush=True)
    await asyncio.Event().wait()


async def _ctl(path: str, method: str, params: dict) -> int:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps({"id": 1, "method": method, "params": params}) + "\n").encode())
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    if "error" in reply:
        print(json.dumps(reply["error"]), file=sys.stderr)
        return 1
    print(json.dumps(reply["result"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--socket", required=True)
    ctl = sub.add_parser("ctl")
    ctl.add_argument("--socket", required=True)
    ctl.add_argument("method")
    ctl.add_argument("params", nargs="?", default="{}")
    args = parser.parse_args()
    if args.cmd == "serve":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_serve(args.socket))
        return 0
    return asyncio.run(_ctl(args.socket, args.method, json.loads(args.params)))


if __name__ == "__main__":
    sys.exit(main())
