"""Local API server: newline-delimited JSON over a Unix socket (spec/LOCAL_API.md)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..client import LINE_LIMIT, ApiError
from ..paths import ensure_private_dir, socket_path

if TYPE_CHECKING:
    from .core import Daemon

log = logging.getLogger(__name__)

# A subscriber that stops reading is dropped rather than buffering events without bound.
MAX_CLIENT_BUFFER = 32 * 1024 * 1024
# A client with this much unread output, or this many requests in flight, is not read from until it catches up.
PAUSE_READING_AT = 1024 * 1024
MAX_IN_FLIGHT = 64
CALL_TIMEOUT = 30.0  # every method answers within this (spec/LOCAL_API.md)
SOCKET_CHECK = 5.0  # a socket file deleted under a running orbitd is re-created this often


class StartupError(RuntimeError):
    pass


class AlreadyRunning(StartupError):
    pass


class Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.subscribed = False

    def send(self, obj: dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        self.writer.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
        if self.writer.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
            log.warning("local API client is not reading; disconnecting it")
            self.writer.close()


class ApiServer:
    def __init__(self, daemon: Daemon, path: Path | None) -> None:
        self.daemon = daemon
        self.path = path or socket_path()
        self.clients: set[Client] = set()
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._inode: int | None = None
        self._watch: asyncio.Task | None = None

    async def start(self) -> None:
        await self._listen()
        self._watch = asyncio.ensure_future(self._watch_socket())

    async def _watch_socket(self) -> None:
        """Re-create the socket file if something deleted it (e.g. a cleanup of the runtime dir)."""
        while True:
            await asyncio.sleep(SOCKET_CHECK)
            try:
                inode = self.path.lstat().st_ino
            except OSError:
                inode = None
            if inode == self._inode:
                continue
            log.warning("local API socket %s was removed or replaced; listening again", self.path)
            if self._server:
                self._server.close()
            try:
                await self._listen()
            except StartupError as e:
                log.error("%s", e)

    async def _listen(self) -> None:
        try:
            ensure_private_dir(self.path.parent)
            try:
                mode = self.path.lstat().st_mode
            except FileNotFoundError:
                mode = None
            if mode is not None:
                if not stat.S_ISSOCK(mode):
                    raise StartupError(f"{self.path} exists and is not a socket; not replacing it")
                if _socket_alive(self.path):
                    raise AlreadyRunning(f"another orbitd is already listening on {self.path}")
                self.path.unlink()
        except OSError as e:
            raise StartupError(f"cannot prepare {self.path}: {e.strerror or e}") from e
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, str(self.path), limit=LINE_LIMIT)
        except OSError as e:
            raise StartupError(f"cannot listen on {self.path}: {e.strerror or e}") from e
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self._inode = self.path.lstat().st_ino

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self._server:
            self._server.close()
        for c in list(self.clients):
            c.writer.close()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(OSError):
            if self.path.lstat().st_ino == self._inode:  # not another orbitd's socket
                self.path.unlink()

    def broadcast(self, event: str, data: dict[str, Any]) -> None:
        for c in list(self.clients):
            if c.subscribed:
                c.send({"event": event, "data": data})

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(writer)
        self.clients.add(client)
        slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        try:
            while True:
                # Backpressure: a client that sends requests without reading the answers gets no more work.
                if writer.transport.get_write_buffer_size() > PAUSE_READING_AT:
                    await writer.drain()
                await slots.acquire()
                try:
                    line = await reader.readline()
                except ValueError:  # line longer than LINE_LIMIT
                    client.send({"id": None, "error": {"code": "bad_request", "message": "request too long"}})
                    break
                if not line:
                    break
                if not line.strip():
                    slots.release()
                    continue
                task = asyncio.create_task(self._request(client, line))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                task.add_done_callback(lambda _t: slots.release())
        except ConnectionError:
            pass
        finally:
            self.clients.discard(client)
            writer.close()

    async def _request(self, client: Client, line: bytes) -> None:
        req_id = None
        try:
            try:
                req = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ApiError("bad_request", f"invalid JSON: {e}") from None
            except RecursionError:
                raise ApiError("bad_request", "invalid JSON: nested too deeply") from None
            if not isinstance(req, dict):
                raise ApiError("bad_request", "request must be a JSON object")
            req_id = req.get("id")
            method, params = req.get("method"), req.get("params", {})
            if params is None:
                params = {}
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ApiError("bad_request", "'method' must be a string and 'params' an object")
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    result = await self._call(client, method, params)
            except TimeoutError:
                raise ApiError("timeout", f"orbitd gave up after {CALL_TIMEOUT:g} s") from None
            client.send({"id": req_id, "result": result})
            if method == "subscribe":
                for kind in ("devices", "pairing", "activities"):
                    event, data = self.daemon.event(kind)
                    client.send({"event": event, "data": data})
        except ApiError as e:
            client.send({"id": req_id, "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            log.exception("local API request failed")
            client.send({"id": req_id, "error": {"code": "failed", "message": str(e)}})

    async def _call(self, client: Client, method: str, params: dict[str, Any]) -> Any:
        d = self.daemon
        match method:
            case "status":
                return d.api_status()
            case "subscribe":
                client.subscribed = True
                return True
            case "devices.list":
                return d.devices_json()
            case "devices.connect":
                return await d.api_connect(params)
            case "devices.disconnect":
                return await d.api_disconnect(params)
            case "devices.forget":
                return await d.api_forget(params)
            case "pairing.list":
                return d.pairings_json()
            case "pairing.respond":
                return await d.api_pairing_respond(params)
            case "activities.list":
                return d.activities_json()
            case "activities.action":
                return await d.api_action(params)
            case "art.get":
                return d.api_art(params)
        raise ApiError("bad_request", f"unknown method {method!r}")


def _socket_alive(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(str(path))
        except OSError:
            return False
    return True
