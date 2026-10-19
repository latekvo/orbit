"""One encrypted Initiator session with a phone (spec/PROTOCOL.md 3.4 and 5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from ..client import ApiError
from ..proto import ProtocolError, SecureChannel
from .timeconv import now_ms

if TYPE_CHECKING:
    from .core import Daemon

log = logging.getLogger(__name__)

UNPAIRED_ALLOWED = {"pair_request", "pair_decision", "ping", "pong", "bye", "error"}


class Pairing:
    def __init__(self) -> None:
        self.local: bool | None = None
        self.remote: bool | None = None


class Session:
    def __init__(self, daemon: Daemon, channel: SecureChannel, transport: str, address: str, manual: bool) -> None:
        self.daemon = daemon
        self.channel = channel
        self.peer = channel.peer
        self.sas = channel.sas
        self.transport = transport
        self.address = address
        # Address came from the user (devices.connect host:port): remember it once paired.
        self.manual = manual
        self.id = secrets.token_hex(8)
        self.local_paired = False
        self.remote_paired = False
        self.pairing: Pairing | None = None
        self.offset = 0
        self.closed = asyncio.Event()
        self.close_reason = ""
        self._req = 0
        self._ping_req = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._last_rx = self._last_tx = time.monotonic()

    @property
    def paired(self) -> bool:
        return self.local_paired and self.remote_paired

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.channel.send(msg)
            self._last_tx = time.monotonic()

    async def recv(self) -> dict[str, Any]:
        msg = await self.channel.recv()
        self._last_rx = time.monotonic()
        return msg

    async def exchange_ready(self, timeout: float) -> None:
        """Send our ``ready`` and wait for the phone's (spec 3.4)."""
        await self.send({"t": "ready", "paired": self.local_paired, "now": now_ms()})
        msg = await asyncio.wait_for(self.recv(), timeout)
        if msg["t"] == "error":
            raise ProtocolError(str(msg.get("code", "error")), str(msg.get("message", "")))
        if msg["t"] != "ready":
            raise ProtocolError("bad_message", f"expected ready, got {msg['t']}")
        self.remote_paired = msg.get("paired") is True
        self._note_clock(msg)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._keepalive())]

    async def request_pairing(self) -> None:
        self.pairing = Pairing()
        self._tasks.append(asyncio.create_task(self._pairing_deadline()))
        await self.send({"t": "pair_request"})

    async def decide(self, accept: bool) -> None:
        if self.pairing is None:
            raise ApiError("not_found", "no pairing pending for this session")
        self.pairing.local = accept
        self.daemon.changed("pairing")
        await self.send({"t": "pair_decision", "accepted": accept})
        if not accept:
            await self.close("pairing rejected locally")
        else:
            await self._maybe_pair_complete()

    async def action(self, activity: str, action: str, value: Any, has_value: bool, timeout: float) -> dict[str, Any]:
        self._req += 1
        req = self._req
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        msg: dict[str, Any] = {"t": "action", "req": req, "activity": activity, "action": action}
        if has_value:
            msg["value"] = value
        try:
            await self.send(msg)
            result = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise ApiError("timeout", f"no action_result within {timeout:g} s") from None
        except (ConnectionError, ProtocolError) as e:
            raise ApiError("not_connected", str(e)) from None
        finally:
            self._pending.pop(req, None)
        out: dict[str, Any] = {"ok": result.get("ok") is True}
        if result.get("error") is not None:
            out["error"] = str(result["error"])
        return out

    async def close(self, reason: str, bye: bool = False) -> None:
        if self.closed.is_set() or self.close_reason:
            return
        self.close_reason = reason
        log.info("session %s with %s (%s) closing: %s", self.id, self.peer.name, self.peer.id, reason)
        if bye:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send({"t": "bye", "reason": reason}), 2)
        self._finish()

    def _finish(self) -> None:
        if self.closed.is_set():
            return
        self.close_reason = self.close_reason or "closed"
        with contextlib.suppress(Exception):
            self.channel.close()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"session closed: {self.close_reason}"))
        self.closed.set()
        self.daemon.session_closed(self)

    def _note_clock(self, msg: dict[str, Any]) -> int:
        now = msg.get("now")
        if isinstance(now, int | float) and not isinstance(now, bool):
            self.offset = now_ms() - int(now)
            return int(now)
        return now_ms() - self.offset

    async def _read_loop(self) -> None:
        try:
            while True:
                await self._dispatch(await self.recv())
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError:
            self.close_reason = self.close_reason or "connection closed by phone"
        except ProtocolError as e:
            self.close_reason = self.close_reason or f"protocol error: {e}"
        except (ConnectionError, OSError) as e:
            self.close_reason = self.close_reason or f"connection lost: {e}"
        except Exception as e:
            log.exception("session %s crashed", self.id)
            self.close_reason = self.close_reason or f"internal error: {e}"
        self._finish()

    async def _keepalive(self) -> None:
        d = self.daemon
        while True:
            now = time.monotonic()
            if now - self._last_rx >= d.dead_after:
                self.close_reason = f"no data from phone for {d.dead_after:g} s"
                self._finish()
                return
            if now - self._last_tx >= d.keepalive:
                self._ping_req += 1
                try:
                    await self.send({"t": "ping", "req": self._ping_req})
                except Exception as e:
                    self.close_reason = self.close_reason or f"connection lost: {e}"
                    self._finish()
                    return
                continue
            wake = min(self._last_rx + d.dead_after, self._last_tx + d.keepalive)
            await asyncio.sleep(max(0.01, wake - now))

    async def _pairing_deadline(self) -> None:
        await asyncio.sleep(self.daemon.pairing_timeout)
        if not self.paired:
            await self.close("pairing timed out")

    async def _maybe_pair_complete(self) -> None:
        if self.pairing and self.pairing.local and self.pairing.remote:
            self.pairing = None
            self.local_paired = self.remote_paired = True
            self.daemon.pair_completed(self)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg["t"]
        if not self.paired and t not in UNPAIRED_ALLOWED:
            await self.send({"t": "error", "code": "not_paired", "message": f"'{t}' requires a paired session"})
            return
        d = self.daemon
        match t:
            case "ping":
                await self.send({"t": "pong", "req": msg.get("req")})
            case "pong":
                pass
            case "bye":
                self.close_reason = f"phone said bye ({msg.get('reason') or 'no reason'})"
                self._finish()
            case "error":
                log.warning("phone %s reported error %s: %s", self.peer.id, msg.get("code"), msg.get("message"))
            case "pair_decision":
                if self.pairing is None:
                    return
                if msg.get("accepted") is not True:
                    self.pairing.remote = False
                    self.close_reason = "pairing rejected on the phone"
                    self._finish()
                    return
                self.pairing.remote = True
                d.changed("pairing")
                await self._maybe_pair_complete()
            case "snapshot":
                now = self._note_clock(msg)
                acts = msg.get("activities")
                d.set_activities(self, [a for a in acts if _valid_activity(a)] if isinstance(acts, list) else [], now)
            case "upsert":
                now = self._note_clock(msg)
                if _valid_activity(msg.get("activity")):
                    d.upsert_activity(self, msg["activity"], now)
            case "remove":
                if isinstance(msg.get("id"), str):
                    d.remove_activity(self, msg["id"])
            case "art":
                d.store_art(self, msg)
            case "action_result":
                fut = self._pending.get(msg.get("req"))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            case "unpair":
                self.close_reason = "phone unpaired this computer"
                d.unpaired_by_phone(self)
                self._finish()
            case _:
                pass  # spec 2: unknown types are ignored


def _valid_activity(a: Any) -> bool:
    return isinstance(a, dict) and isinstance(a.get("id"), str)
