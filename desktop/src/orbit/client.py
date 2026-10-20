"""Async client for the orbitd local API (spec/LOCAL_API.md)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .paths import socket_path

LINE_LIMIT = 16 * 1024 * 1024
# orbitd answers every call within 30 s (spec/LOCAL_API.md); past this it is stuck (e.g. stopped).
CALL_TIMEOUT = 40.0


class ApiError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class LocalClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self.call_timeout: float | None = CALL_TIMEOUT
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, path: Path | str | None = None) -> LocalClient:
        reader, writer = await asyncio.open_unix_connection(str(path or socket_path()), limit=LINE_LIMIT)
        return cls(reader, writer)

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                msg = json.loads(line)
                if "event" in msg:
                    self._events.put_nowait((msg["event"], msg.get("data")))
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(ApiError(err.get("code", "failed"), err.get("message", "")))
                else:
                    fut.set_result(msg.get("result"))
        except (ConnectionError, ValueError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("orbitd closed the connection"))
            self._pending.clear()
            self._events.put_nowait(None)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call ``method``; raises TimeoutError if orbitd does not answer within ``call_timeout`` s."""
        if self._task.done():
            raise ConnectionError("orbitd closed the connection")
        self._next_id += 1
        req = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        line = json.dumps({"id": req, "method": method, "params": params or {}}) + "\n"
        try:
            async with asyncio.timeout(self.call_timeout):
                self._writer.write(line.encode())
                await self._writer.drain()
                return await fut
        finally:
            self._pending.pop(req, None)

    async def events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event, data)`` pairs; call ``subscribe`` first. Ends when the daemon disconnects."""
        while (item := await self._events.get()) is not None:
            yield item
        self._events.put_nowait(None)

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
