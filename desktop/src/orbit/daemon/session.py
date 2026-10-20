"""One encrypted Initiator session with a phone (spec/PROTOCOL.md 3.4 and 5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import struct
import time
from typing import TYPE_CHECKING, Any

from ..client import ApiError
from ..proto import ProtocolError, SecureChannel
from .timeconv import now_ms

if TYPE_CHECKING:
    from .core import Daemon

log = logging.getLogger(__name__)

UNPAIRED_ALLOWED = {"pair_request", "pair_decision", "ping", "pong", "bye", "error"}
SENT_THEN_LOST = "connection lost after the action was sent; it may have been applied"
NO_RESPONSE = "phone did not respond; it may still be applied"
# A probe also passes if data is still arriving at its deadline (a large frame, a burst of art).
PROBE_FLOWING = 1.0


class LinkDown(ConnectionError):
    """The transport is gone. ``sent`` is False only if the phone cannot have the whole failed frame."""

    def __init__(self, cause: str, sent: bool) -> None:
        super().__init__(cause)
        self.sent = sent


class ActionNotSent(Exception):
    """The session ended before the phone could have the whole action, so it is safe to send it on the next one."""


class WatchedReader:
    """FrameStream reader that records when bytes arrive and whether a frame is half-read.

    ``last_byte`` is stamped when bytes reach the stream's buffer, not when the read loop gets to
    them. ``mid_frame`` relies on FrameStream.read_frame reading a 4-byte header, then the body: it
    is true from a frame's first byte until its last.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self.last_byte = time.monotonic()
        self.mid_frame = False
        self._next_is_header = True
        feed = reader.feed_data

        def stamped(data: bytes) -> None:
            if data:
                self.last_byte = time.monotonic()
            feed(data)

        reader.feed_data = stamped  # type: ignore[method-assign]

    async def readexactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self._reader.read(n - len(buf))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(buf), n)
            buf += chunk
            self.mid_frame = True
        self._next_is_header = not self._next_is_header
        self.mid_frame = not self._next_is_header
        return bytes(buf)


class WatchedWriter:
    """FrameStream writer that counts the bytes written and can tell how many the phone may have.

    ``delivered()`` is the number of leading bytes that may have reached the phone: for TCP those
    handed to the kernel (unacknowledged ones too: the acknowledgement can be what was lost), for
    BLE those handed to the radio (``BleLink.handed``); None when that cannot be known.
    """

    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self.written = 0

    def write(self, data: bytes) -> None:
        self._writer.write(data)
        self.written += len(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    def delivered(self) -> int | None:
        handed = getattr(self._writer, "handed", None)
        if isinstance(handed, int):
            return handed
        transport = getattr(self._writer, "transport", None)
        if transport is None:
            return None
        return self.written - transport.get_write_buffer_size()

    def abort(self) -> None:
        """Drop the connection now, discarding unsent and unacknowledged bytes (TCP: RST)."""
        transport = getattr(self._writer, "transport", None)
        if transport is None:
            self._writer.close()
            return
        sock = transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError, ValueError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        transport.abort()


def _retrieve(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()


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
        self.started = False
        self.close_reason = ""
        # The phone sent its snapshot: the session carried real data (resets the reconnect backoff).
        self.productive = False
        # A liveness check is pending and actions wait for its outcome (e.g. after the computer slept).
        self.suspect = False
        self._req = 0
        self._ping_req = 0
        self._pending: dict[int, asyncio.Future] = {}
        # Request id -> stream offset just past its action frame, once written.
        self._frame_end: dict[int, int] = {}
        # Request id -> whether the phone may have its action, judged when the session ended.
        self._outcomes: dict[int, bool] = {}
        self._send_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._rx: WatchedReader = channel.stream.reader
        self._tx: WatchedWriter = channel.stream.writer
        self._abort = False
        # Pending liveness probes: (deadline, reason, first ping req, first action req that answers it).
        self._probes: list[tuple[float, str, int, int]] = []
        self._verified = asyncio.Event()
        self._verified.set()
        self._kick = asyncio.Event()
        self.opened = self._last_tx = time.monotonic()

    @property
    def paired(self) -> bool:
        return self.local_paired and self.remote_paired

    async def send(self, msg: dict[str, Any], req: int | None = None) -> None:
        """Send one frame within ``daemon.send_timeout``, or close the session (the link is stalled).

        A caller cancelled while waiting for its turn sends nothing; once writing has started the
        frame is finished regardless, so a cancelled caller never cuts a frame in half.
        """
        await self._send_lock.acquire()
        write = asyncio.ensure_future(self._write_locked(msg, req))
        write.add_done_callback(_retrieve)
        await asyncio.shield(write)

    async def _write_locked(self, msg: dict[str, Any], req: int | None) -> None:
        try:
            if self.closed.is_set():
                raise LinkDown(f"session closed: {self.close_reason}", sent=False)
            timeout = self.daemon.send_timeout
            before = self._tx.written
            try:
                await asyncio.wait_for(self.channel.send(msg), timeout)
            except TimeoutError:
                self._note_frame(req, before)
                reason = f"link stalled: sending took over {timeout:g} s"
                self.close_reason = self.close_reason or reason
                self._abort = True
                self._finish()
                raise LinkDown(reason, sent=True) from None
            except BaseException:
                self._note_frame(req, before)
                raise
            self._note_frame(req, before)
            self._last_tx = time.monotonic()
        finally:
            self._send_lock.release()

    def _note_frame(self, req: int | None, before: int) -> None:
        if req is not None and self._tx.written > before:
            self._frame_end[req] = self._tx.written

    async def recv(self) -> dict[str, Any]:
        return await self.channel.recv()

    async def exchange_ready(self, timeout: float, art: list[str] | None = None) -> None:
        """Send our ``ready`` (with the art keys we hold, spec 3.4) and wait for the phone's."""
        ready: dict[str, Any] = {"t": "ready", "paired": self.local_paired, "now": now_ms()}
        if art:
            ready["art"] = art
        await self.send(ready)
        msg = await asyncio.wait_for(self.recv(), timeout)
        if msg["t"] == "error":
            raise ProtocolError(str(msg.get("code", "error")), str(msg.get("message", "")))
        if msg["t"] != "ready":
            raise ProtocolError("bad_message", f"expected ready, got {msg['t']}")
        self.remote_paired = msg.get("paired") is True
        self._note_clock(msg)

    def start(self) -> None:
        self.started = True
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._keepalive())]

    async def request_pairing(self) -> None:
        self.pairing = Pairing()
        self._tasks.append(asyncio.create_task(self._pairing_deadline()))
        await self.send({"t": "pair_request"})

    async def decide(self, accept: bool) -> None:
        if self.pairing is None:
            raise ApiError("not_found", "no pairing pending for this session")
        if self.pairing.local is not None:
            raise ApiError("bad_request", "this pairing was already answered")
        self.pairing.local = accept
        self.daemon.changed("pairing")
        if accept:
            # Before the send yields: the phone may send its snapshot as soon as it has our decision.
            self._maybe_pair_complete()
        await self.send({"t": "pair_decision", "accepted": accept})
        if not accept:
            await self.close("pairing rejected locally")

    # ---- liveness ----------------------------------------------------------------------------

    def probe(
        self, reason: str, timeout: float, hold_actions: bool = False, ping: bool = True, action: int | None = None
    ) -> None:
        """Close the session unless, within ``timeout``, the phone answers a ping sent from now on (``ping``
        sends one) or the action ``action``, or data is still arriving at the deadline. Other bytes prove
        nothing: they may have been on their way before the link died.

        ``hold_actions``: actions wait for the outcome instead of being sent on a link that may be dead.
        """
        if self.closed.is_set():
            return
        self._probes.append((time.monotonic() + timeout, reason, self._ping_req + 1, action or self._req + 1))
        if ping:
            self._spawn(self._ping())
        if hold_actions and not self.suspect:
            self.suspect = True
            self._verified.clear()
        self._kick.set()

    async def verify(self, timeout: float, quiet_for: float = 1.0) -> bool:
        """True if the session is alive: bytes arrived within ``quiet_for``, or answer a probe within ``timeout``."""
        if self.closed.is_set():
            return False
        if self.quiet_for() < quiet_for and not self.suspect:
            return True
        self.probe("checking the connection", timeout, hold_actions=True)
        await self._verified.wait()
        return not self.closed.is_set()

    def quiet_for(self) -> float:
        """Seconds since the last byte from the phone."""
        return time.monotonic() - self._rx.last_byte

    def _answered(self, ping: Any = None, action: Any = None) -> None:
        """A pong or action_result arrived: it passes the probes started before its request was sent."""
        ping = ping if isinstance(ping, int) else -1
        action = action if isinstance(action, int) else -1
        if self._probes:
            self._probes = [p for p in self._probes if ping < p[2] and action < p[3]]
            if not self._probes:
                self._probe_passed()

    def _probe_passed(self) -> None:
        if self.suspect:
            self.suspect = False
            self._verified.set()
            self.daemon.session_verified(self)

    async def _ping(self) -> None:
        self._ping_req += 1
        with contextlib.suppress(LinkDown, ConnectionError, OSError):
            await self.send({"t": "ping", "req": self._ping_req})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    # ---- actions -------------------------------------------------------------------------------

    async def action(self, activity: str, action: str, value: Any, has_value: bool, timeout: float) -> dict[str, Any]:
        if self.suspect:
            await self._verified.wait()
        if self.closed.is_set():
            raise ActionNotSent(f"session closed: {self.close_reason}")
        self._req += 1
        req = self._req
        fut = asyncio.get_running_loop().create_future()
        self._pending[req] = fut
        msg: dict[str, Any] = {"t": "action", "req": req, "activity": activity, "action": action}
        if has_value:
            msg["value"] = value
        try:
            await self.send(msg, req)
            # A phone that is alive answers within a moment (its action_result or our ping's pong);
            # silence means the link died, and the action must not be delivered later.
            self.probe("no answer to an action", self.daemon.action_probe, action=req)
            result = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise ApiError("timeout", NO_RESPONSE) from None
        except ProtocolError as e:
            if e.code == "bad_frame":
                raise ApiError("bad_request", "action message too large") from None
            raise ApiError("not_connected", str(e)) from None
        except (ConnectionError, OSError) as e:  # LinkDown, or set by _finish while we waited
            sent = self._outcomes[req] if req in self._outcomes else self._may_have(req)
            if not self.closed.is_set():
                self.close_reason = self.close_reason or f"connection lost: {e}"
                self._finish()
            if sent:
                raise ApiError("failed", SENT_THEN_LOST) from None
            raise ActionNotSent(str(e)) from None
        finally:
            self._pending.pop(req, None)
            self._frame_end.pop(req, None)
            self._outcomes.pop(req, None)
            if fut.done():  # failed by _finish while we were still sending
                _retrieve(fut)
        out: dict[str, Any] = {"ok": result.get("ok") is True}
        if result.get("error") is not None:
            out["error"] = str(result["error"])
        return out

    def _may_have(self, req: int) -> bool:
        """Whether the phone may have the whole action frame of ``req`` (it cannot apply part of one)."""
        end = self._frame_end.get(req)
        if end is None:
            return False
        delivered = self._tx.delivered()
        return delivered is None or delivered >= end

    # ---- closing -------------------------------------------------------------------------------

    async def close(self, reason: str, bye: bool = False) -> None:
        if self.closed.is_set() or self.close_reason:
            return
        self.close_reason = reason
        if bye:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send({"t": "bye", "reason": reason}), 2)
        self._finish()

    async def retire(self, reason: str, wait: float) -> None:
        """Close once requests in flight are answered (up to ``wait``): the session was replaced."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while self._pending and not self.closed.is_set() and loop.time() < deadline:
            await asyncio.wait(list(self._pending.values()), timeout=deadline - loop.time())
        await self.close(reason, bye=True)

    def _finish(self) -> None:
        if self.closed.is_set():
            return
        self.close_reason = self.close_reason or "closed"
        log.info(
            "session_closed session=%s device=%s name=%r transport=%s address=%s uptime=%.1fs cause=%r",
            self.id,
            self.peer.id,
            self.peer.name,
            self.transport,
            self.address,
            time.monotonic() - self.opened,
            self.close_reason,
        )
        # Judged before the transport goes, which discards its buffer.
        outcomes = self._outcomes = {req: self._may_have(req) for req in self._pending}
        if self._abort or not all(outcomes.values()):
            # Bytes in flight must not reach the phone later (TCP would keep retransmitting them).
            with contextlib.suppress(Exception):
                self._tx.abort()
        with contextlib.suppress(Exception):
            self.channel.close()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for req, fut in self._pending.items():
            if not fut.done():
                fut.set_exception(LinkDown(f"session closed: {self.close_reason}", sent=outcomes[req]))
        self.closed.set()
        self._verified.set()
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

    def _dead(self, reason: str) -> None:
        self.close_reason = reason
        self._abort = True
        self._finish()

    async def _keepalive(self) -> None:
        d = self.daemon
        while True:
            now = time.monotonic()
            last = self._rx.last_byte
            silent = now - last
            due = [p for p in self._probes if now >= p[0]]
            if due:
                if silent >= PROBE_FLOWING:
                    self._dead(f"no data from phone for {silent:.0f} s ({min(due)[1]})")
                    return
                self._probes = [p for p in self._probes if p not in due]
                if not self._probes:
                    self._probe_passed()
            if silent >= d.dead_after or (self._rx.mid_frame and silent >= d.stall_after):
                what = "mid-frame" if silent < d.dead_after else "from phone"
                self._dead(f"no data {what} for {silent:.0f} s")
                return
            if now - self._last_tx >= d.keepalive:
                self._last_tx = now
                self._spawn(self._ping())
                # A ping that draws no answer within stall_after means the link is gone.
                self.probe("no answer to a ping", d.stall_after, ping=False)
                continue
            wake = min(self._rx.last_byte + min(d.dead_after, d.stall_after), self._last_tx + d.keepalive)
            if self._probes:
                wake = min([wake] + [p[0] for p in self._probes])
            self._kick.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), max(0.01, wake - now))

    async def _pairing_deadline(self) -> None:
        await asyncio.sleep(self.daemon.pairing_timeout)
        if not self.paired:
            await self.close("pairing timed out", bye=True)

    def _maybe_pair_complete(self) -> None:
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
                self._answered(ping=msg.get("req"))
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
                self._maybe_pair_complete()
            case "snapshot":
                now = self._note_clock(msg)
                acts = msg.get("activities")
                self.productive = True
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
                self._answered(action=msg.get("req"))
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
