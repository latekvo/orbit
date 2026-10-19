"""The fake phone: an Orbit Responder (spec/PROTOCOL.md sections 1.1, 3, 4 and 5)."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..proto import FrameStream, ProtocolError, SecureChannel, device_id, respond
from ..proto.crypto import b64e
from ..store import PeerStore, PinnedPeer, load_or_create_identity
from .art import art_key, image_mime
from .mdns import Advertiser
from .models import ActionError, Chat, Download, Media, Model, Timer, now_ms

DEFAULT_PORT = 47820
FLUSH_TIMEOUT = 2.0
EVENT_LIMIT = 10_000

Prompt = Callable[["Session"], Awaitable[bool]]


def _summary(msg: dict[str, Any]) -> str:
    t = msg["t"]
    if t == "upsert":
        return f"upsert {msg['activity'].get('id')}"
    if t == "snapshot":
        return f"snapshot [{', '.join(str(a.get('id')) for a in msg['activities'])}]"
    detail = " ".join(f"{k}={msg[k]!r}" for k in ("id", "key", "req", "paired", "accepted", "ok", "code") if k in msg)
    return f"{t} {detail}".rstrip()


class Session:
    """One encrypted connection from an initiator (a laptop)."""

    def __init__(self, phone: FakePhone, channel: SecureChannel, sid: str, address: str) -> None:
        self.phone = phone
        self.channel = channel
        self.id = sid
        self.address = address
        self.peer = channel.peer
        self.sas = channel.sas
        self.paired = False
        self.closed = False
        self.close_reason: str | None = None
        self.local_ready = phone.pins.is_pinned(self.peer.id, self.peer.static)
        self.remote_ready: bool | None = None
        self.pairing = False
        self.local_decision: bool | None = None
        self.remote_decision: bool | None = None
        self._sent_art: set[str] = set()
        self._out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closing = asyncio.Event()
        self._flush = True
        self._ping_req = 0
        self._loop = asyncio.get_running_loop()
        self._last_sent = self._last_recv = self._loop.time()
        self._pair_timer: asyncio.TimerHandle | None = None
        self._prompt_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"<Session {self.id} {self.peer.name!r} paired={self.paired} closed={self.closed}>"

    # -- outgoing ---------------------------------------------------------------------------------------------

    def send(self, msg: dict[str, Any]) -> None:
        if not self.closed and not self.phone.frozen:
            self._out.put_nowait(msg)

    def _send_art_for(self, activity: dict[str, Any]) -> None:
        for field in ("iconKey", "artKey"):
            key = activity.get(field)
            if isinstance(key, str) and key not in self._sent_art and key in self.phone.art:
                self._sent_art.add(key)
                data = self.phone.art[key]
                self.send({"t": "art", "key": key, "mime": image_mime(data), "data": b64e(data)})

    def send_upsert(self, activity: dict[str, Any], now: int) -> None:
        self._send_art_for(activity)
        self.send({"t": "upsert", "now": now, "activity": activity})

    def send_snapshot(self) -> None:
        now = now_ms()
        activities = self.phone._build_all(now)
        for activity in activities:
            self._send_art_for(activity)
        self.send({"t": "snapshot", "now": now, "activities": activities})

    async def _writer(self) -> None:
        while (msg := await self._out.get()) is not None:
            if self.phone.frozen:
                continue
            try:
                await self.channel.send(msg)
            except (ConnectionError, OSError) as e:
                self.close(f"connection lost ({e})", flush=False)
                return
            self._last_sent = self._loop.time()
            self.phone.log.debug("-> %s %s", self.id, _summary(msg))

    # -- incoming ---------------------------------------------------------------------------------------------

    async def _reader(self) -> None:
        try:
            while True:
                msg = await self.channel.recv()
                if self.phone.frozen:
                    await asyncio.Future()  # hung: never read again; cancelled when the session closes
                self._last_recv = self._loop.time()
                self.phone.log.debug("<- %s %s", self.id, _summary(msg))
                self._dispatch(msg)
        except asyncio.IncompleteReadError:
            self.close("connection closed by peer", flush=False)
        except (ConnectionError, OSError) as e:
            self.close(f"connection lost ({e})", flush=False)
        except ProtocolError as e:
            self.close(f"protocol error: {e}", flush=False)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg["t"]
        if self.remote_ready is None:
            if t != "ready":
                self.send({"t": "error", "code": "bad_message", "message": f"expected ready, got {t}"})
                self.close(f"peer sent {t} before ready")
                return
            self._on_ready(msg.get("paired") is True)
        elif t == "ping":
            self.send({"t": "pong", "req": msg.get("req")})
        elif t in ("pong", "ready"):
            pass
        elif t == "bye":
            self.close(f"bye from peer ({msg.get('reason', '')})" if msg.get("reason") else "bye from peer")
        elif t == "error":
            self.phone.log.warning("session %s: peer error %s: %s", self.id, msg.get("code"), msg.get("message"))
        elif t == "pair_request":
            self._on_pair_request()
        elif t == "pair_decision":
            self._on_pair_decision(msg.get("accepted") is True)
        elif not self.paired:  # only the types above are valid before pairing (spec 3.4)
            self.send({"t": "error", "code": "not_paired", "message": f"{t} requires a paired session"})
        elif t == "action":
            self.phone._handle_action(self, msg)
        elif t == "unpair":
            self.phone._unpair(self)
        else:
            self.phone.log.debug("session %s: ignoring unknown message %s", self.id, t)

    def _on_ready(self, remote_paired: bool) -> None:
        self.remote_ready = remote_paired
        self.paired = self.local_ready and remote_paired
        self.phone._emit(
            "ready",
            {"session": self.id, "peer": self.peer.id, "paired": self.paired, "remote_paired": remote_paired},
        )
        if self.paired:
            self.phone.log.info("session %s paired (known peer %r) - sending snapshot", self.id, self.peer.name)
            self.send_snapshot()
        else:
            self.phone.log.info(
                "session %s not paired (phone has pin: %s, peer has pin: %s)",
                self.id,
                self.local_ready,
                remote_paired,
            )

    # -- pairing ----------------------------------------------------------------------------------------------

    def _on_pair_request(self) -> None:
        if self.paired or self.pairing:
            self.phone.log.info(
                "session %s: ignoring pair_request (already %s)", self.id, "paired" if self.paired else "pairing"
            )
            return
        self.pairing = True
        self._pair_timer = self._loop.call_later(self.phone.pairing_timeout, self._pairing_expired)
        self.phone.log.info("pairing request from %r (%s) - code %s", self.peer.name, self.peer.id, self.sas)
        self.phone._emit("pairing", {"session": self.id, "peer": self.peer.id, "name": self.peer.name, "sas": self.sas})
        self.phone._pairing_requested(self)

    def _on_pair_decision(self, accepted: bool) -> None:
        if not accepted:
            self._pairing_failed("peer")
        elif self.paired:
            pass
        elif not self.pairing:
            self.send({"t": "error", "code": "bad_state", "message": "pair_decision without pair_request"})
        else:
            self.remote_decision = True
            self._maybe_complete()

    def decide(self, accept: bool) -> None:
        if self.closed or not self.pairing or self.local_decision is not None:
            raise LookupError(f"session {self.id} has no pending pairing decision")
        if self._prompt_task and self._prompt_task is not asyncio.current_task():
            self._prompt_task.cancel()
        self.local_decision = accept
        self.send({"t": "pair_decision", "accepted": accept})
        self.phone.log.info("pairing with %r %s on the phone", self.peer.name, "accepted" if accept else "rejected")
        if accept:
            self._maybe_complete()
        else:
            self._pairing_failed("local")

    def _maybe_complete(self) -> None:
        if not (self.local_decision and self.remote_decision):
            return
        self.pairing = False
        if self._pair_timer:
            self._pair_timer.cancel()
        p = self.peer
        self.phone.pins.pin(PinnedPeer(id=p.id, name=p.name, platform=p.platform, static=p.static))
        self.local_ready = self.paired = True
        self.phone.log.info("paired with %r (%s) - sending snapshot", p.name, p.id)
        self.phone._emit("paired", {"session": self.id, "peer": p.id, "name": p.name})
        self.send_snapshot()

    def _pairing_failed(self, by: str) -> None:
        self.pairing = False
        self.phone.log.info("pairing with %r rejected by %s", self.peer.name, by)
        self.phone._emit("pair_rejected", {"session": self.id, "peer": self.peer.id, "by": by})
        if by == "timeout":
            self.close("pairing timed out", bye="pairing timeout")
        else:
            self.close(f"pairing rejected by {by}")

    def _pairing_expired(self) -> None:
        self._pair_timer = None
        if self.pairing and not self.closed and not self.phone.frozen:
            self._pairing_failed("timeout")

    # -- lifecycle --------------------------------------------------------------------------------------------

    async def _watchdog(self) -> None:
        keepalive, dead_after = self.phone.keepalive, self.phone.dead_after
        interval = max(0.005, min(keepalive, dead_after, 10.0) / 10)
        while True:
            await asyncio.sleep(interval)
            if self.phone.frozen:
                continue
            now = self._loop.time()
            if now - self._last_recv >= dead_after:
                self.close(f"peer silent for {dead_after:g}s", flush=False)
                return
            if now - self._last_sent >= keepalive:
                self._ping_req += 1
                self.send({"t": "ping", "req": self._ping_req})
                self._last_sent = now

    def close(self, reason: str, *, bye: str | None = None, flush: bool = True) -> None:
        """Close the session; queued messages (and ``bye`` if given) are flushed first when ``flush``."""
        if self.closed:
            return
        if bye is not None:
            self.send({"t": "bye", "reason": bye})
        self.closed = True
        self.close_reason = reason
        self._flush = flush
        self.pairing = False
        if self._pair_timer:
            self._pair_timer.cancel()
        if self._prompt_task:
            self._prompt_task.cancel()
        self.phone._session_closed(self)
        self._closing.set()

    async def run(self) -> None:
        tasks = [asyncio.create_task(c) for c in (self._writer(), self._reader(), self._watchdog())]
        writer = tasks[0]
        self.send({"t": "ready", "paired": self.local_ready, "now": now_ms()})
        try:
            await self._closing.wait()
        finally:
            for task in tasks[1:]:
                task.cancel()
            if self._flush and not self.phone.frozen and not writer.done():
                self._out.put_nowait(None)
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(writer), FLUSH_TIMEOUT)
            writer.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.channel.close()


class FakePhone:
    """Simulated Orbit Responder with scripted live activities; see ``orbit.fakephone`` for usage."""

    def __init__(
        self,
        config_dir: Path,
        name: str = "Fake Phone",
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        mdns: bool = True,
        auto_accept: bool | None = None,
        tick: float = 1.0,
        keepalive: float = 20.0,
        dead_after: float = 60.0,
        pairing_timeout: float = 120.0,
        platform: str = "android",
        prompt: Prompt | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.name = name
        self.host = host
        self.port = port
        self.mdns = mdns
        self.auto_accept = auto_accept
        self.prompt = prompt
        self.tick = tick
        self.keepalive = keepalive
        self.dead_after = dead_after
        self.pairing_timeout = pairing_timeout
        self.platform = platform
        self.log = logging.getLogger("orbit.fakephone")

        self.identity = load_or_create_identity(self.config_dir)
        self.device_id = device_id(self.identity.public)
        self.pins = PeerStore(self.config_dir)
        self.frozen = False
        self.art: dict[str, bytes] = {}
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=EVENT_LIMIT)
        self._log: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=EVENT_LIMIT)
        self.advertiser: Advertiser | None = None

        self._seq = 0
        self._consumed: set[int] = set()
        self._waiters: list[asyncio.Future[None]] = []
        self._sessions: list[Session] = []
        self._next_session = 0
        self._conn_tasks: set[asyncio.Task[Any]] = set()
        self._activities: dict[str, Model | dict[str, Any]] = {}
        self._server: asyncio.Server | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

        self.media = Media(self)
        self.download = Download(self)
        self.timer = Timer(self)
        self.chat = Chat(self)
        self.models: list[Model] = [self.media, self.download, self.timer, self.chat]

    # -- public API -------------------------------------------------------------------------------------------

    @property
    def sessions(self) -> list[Session]:
        """Live (not yet closed) sessions, oldest first."""
        return list(self._sessions)

    @property
    def history(self) -> list[tuple[str, dict[str, Any]]]:
        """Every event so far as (kind, data), oldest first (the last 10000)."""
        return [(kind, data) for _, kind, data in self._log]

    @property
    def activities(self) -> dict[str, dict[str, Any]]:
        """The current activity set as it would appear in a snapshot now."""
        now = now_ms()
        return {a["id"]: a for a in self._build_all(now)}

    async def start(self) -> None:
        self._server = await self._listen()
        self.port = self._server.sockets[0].getsockname()[1]
        self.log.info(
            "listening on %s:%d as %r - device id %s, platform %s",
            self.host,
            self.port,
            self.name,
            self.device_id,
            self.platform,
        )
        for model in self.models:
            model.start()
        self._tick_task = asyncio.create_task(self._tick_loop())
        if self.mdns:
            self.advertiser = Advertiser(self.name, self.device_id, self.platform, self.host, self.port)
            await self.advertiser.register()
            self.log.info(
                "mdns registered %r on port %d, addresses %s, TXT v=1 id=%s pf=%s",
                self.advertiser.instance_name,
                self.port,
                ",".join(self.advertiser.addresses),
                self.device_id,
                self.platform,
            )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._stopped.set()
        if self._tick_task:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
        for model in self.models:
            model.stop()
        for session in self.sessions:
            session.close("phone shutting down", bye="shutdown")
        if self._conn_tasks:
            _, pending = await asyncio.wait(self._conn_tasks, timeout=FLUSH_TIMEOUT + 1)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self.advertiser:
            await self.advertiser.close()
            self.log.info("mdns unregistered %r", self.advertiser.instance_name)
            self.advertiser = None
        self.log.info("stopped")

    async def respond_pairing(self, accept: bool, session: Session | str | None = None) -> Session:
        """Decide a pending pairing: ``session`` (object, session id or peer id), else the most recent pending."""
        if isinstance(session, Session):
            target = session
        else:
            pending = [
                s
                for s in self._sessions
                if s.pairing and s.local_decision is None and (session is None or session in (s.id, s.peer.id))
            ]
            if not pending:
                raise LookupError(f"no pending pairing{'' if session is None else f' for {session}'}")
            target = pending[-1]
        target.decide(accept)
        return target

    def freeze(self) -> None:
        """Simulate a hung phone: TCP stays open but nothing is read, sent or timed out any more."""
        self.frozen = True
        self.log.info("frozen: no more reads, writes or keepalives")

    def upsert(self, activity: dict[str, Any]) -> None:
        """Add or replace an arbitrary activity and push it to all paired sessions."""
        if not isinstance(activity.get("id"), str):
            raise ValueError("activity needs a string id")
        entry = dict(activity)
        entry.setdefault("updatedMs", now_ms())
        entry.setdefault("actions", [])
        self._publish(entry)

    def remove(self, activity_id: str) -> bool:
        for model in self.models:
            if model.id == activity_id:
                model.retired = True  # its simulation keeps running but never republishes it
        return self._unpublish(activity_id)

    def add_art(self, data: bytes) -> str:
        """Register a PNG or JPEG; activities referencing the returned key get it sent first."""
        image_mime(data)
        key = art_key(data)
        self.art[key] = data
        return key

    async def wait_event(
        self,
        kind: str,
        timeout: float = 5.0,  # noqa: ASYNC109 - part of the test-facing API
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Return the oldest not-yet-returned event of ``kind`` (past or future) matching ``predicate``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for seq, k, data in self._log:
                if k == kind and seq not in self._consumed and (predicate is None or predicate(data)):
                    self._consumed.add(seq)
                    return data
            waiter = loop.create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, max(0.0, deadline - loop.time()))
            except TimeoutError:
                recent = [k for _, k, _ in list(self._log)[-10:]]
                raise TimeoutError(f"no {kind!r} event within {timeout:g}s (recent events: {recent})") from None
            finally:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)

    # -- internals --------------------------------------------------------------------------------------------

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self._seq += 1
        self._log.append((self._seq, kind, data))
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait((kind, data))
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _listen(self) -> asyncio.Server:
        try:
            return await asyncio.start_server(self._on_connect, self.host, self.port)
        except OSError as e:
            if self.port != DEFAULT_PORT or e.errno != errno.EADDRINUSE:
                raise
            self.log.info("port %d is taken, using a free port", self.port)
            return await asyncio.start_server(self._on_connect, self.host, 0)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            for model in self.models:
                try:
                    model.tick()
                except Exception:
                    self.log.exception("tick of %s failed", model.id)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._conn_tasks.add(task)
        peername = writer.get_extra_info("peername")
        address = f"{peername[0]}:{peername[1]}" if peername else "?"
        try:
            if self.frozen:
                await self._stopped.wait()
                return
            try:
                channel = await asyncio.wait_for(
                    respond(FrameStream(reader, writer), self.identity, self.name, self.platform), self.dead_after
                )
            except (ProtocolError, asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError) as e:
                self.log.info("handshake from %s failed: %s", address, e or type(e).__name__)
                return
            self._next_session += 1
            session = Session(self, channel, f"s{self._next_session}", address)
            self._sessions.append(session)
            self.log.info(
                "session %s connected: %r (%s, %s) from %s",
                session.id,
                session.peer.name,
                session.peer.platform,
                session.peer.id,
                address,
            )
            self._emit(
                "session",
                {"session": session.id, "peer": session.peer.id, "name": session.peer.name, "address": address},
            )
            await session.run()
        finally:
            writer.close()
            self._conn_tasks.discard(task)

    def _session_closed(self, session: Session) -> None:
        if session in self._sessions:
            self._sessions.remove(session)
        self.log.info("session %s closed (%r): %s", session.id, session.peer.name, session.close_reason)
        self._emit(
            "session_closed",
            {"session": session.id, "peer": session.peer.id, "name": session.peer.name, "reason": session.close_reason},
        )

    def _pairing_requested(self, session: Session) -> None:
        if self.auto_accept is not None:
            session.decide(self.auto_accept)
        elif self.prompt is not None:
            session._prompt_task = asyncio.create_task(self._run_prompt(session))

    async def _run_prompt(self, session: Session) -> None:
        accept = await self.prompt(session)  # type: ignore[misc]
        if not session.closed and session.pairing and session.local_decision is None:
            session.decide(accept)

    def _unpair(self, session: Session) -> None:
        peer = session.peer
        self.pins.remove(peer.id)
        self.log.info("unpaired by %r (%s) - pin removed", peer.name, peer.id)
        self._emit("unpaired", {"session": session.id, "peer": peer.id, "name": peer.name})
        session.close("unpaired by peer")
        for other in self.sessions:
            if other.peer.id == peer.id:
                other.close("peer unpaired")

    def _build(self, entry: Model | dict[str, Any], now: int) -> dict[str, Any]:
        return entry.build(now) if isinstance(entry, Model) else dict(entry)

    def _build_all(self, now: int) -> list[dict[str, Any]]:
        return [self._build(entry, now) for entry in self._activities.values()]

    def _paired_sessions(self) -> list[Session]:
        return [s for s in self._sessions if s.paired and not s.closed]

    def _publish(self, entry: Model | dict[str, Any]) -> None:
        activity_id = entry.id if isinstance(entry, Model) else entry["id"]
        self._activities[activity_id] = entry
        now = now_ms()
        activity = self._build(entry, now)
        for session in self._paired_sessions():
            session.send_upsert(activity, now)
        self._emit("upsert", {"id": activity_id, "activity": activity})

    def _unpublish(self, activity_id: str) -> bool:
        if self._activities.pop(activity_id, None) is None:
            return False
        for session in self._paired_sessions():
            session.send({"t": "remove", "id": activity_id})
        self._emit("remove", {"id": activity_id})
        return True

    def _handle_action(self, session: Session, msg: dict[str, Any]) -> None:
        req = msg.get("req")
        if isinstance(req, bool) or not isinstance(req, int):
            session.send({"t": "error", "code": "bad_message", "message": "action.req must be an integer"})
            return
        activity_id, action_id, value = msg.get("activity"), msg.get("action"), msg.get("value")
        entry = self._activities.get(activity_id) if isinstance(activity_id, str) else None
        error: str | None = None
        try:
            if entry is None:
                raise ActionError(f"unknown activity {activity_id!r}")
            if not isinstance(action_id, str):
                raise ActionError("action must be a string")
            actions = {a.get("id") for a in self._build(entry, now_ms()).get("actions", [])}
            if action_id == "open":
                desc = f"open {activity_id} on the phone"
            elif action_id == "dismiss" and "dismiss" not in actions:
                raise ActionError(f"{activity_id} is not dismissable")
            elif isinstance(entry, Model):
                desc = entry.act(action_id, value)
            elif action_id == "dismiss":
                self.remove(activity_id)
                desc = f"{activity_id} dismissed"
            elif action_id in actions:
                desc = f"{activity_id} {action_id} (value {value!r})"
            else:
                raise ActionError(f"unknown action {action_id}")
        except ActionError as e:
            error = str(e)
        result: dict[str, Any] = {"t": "action_result", "req": req, "ok": error is None}
        if error is not None:
            result["error"] = error
        session.send(result)
        if error is None:
            self.log.info("action %s -> %s (from %r)", action_id, desc, session.peer.name)
        else:
            self.log.info("action %s on %s -> failed: %s (from %r)", action_id, activity_id, error, session.peer.name)
        self._emit(
            "action",
            {
                "session": session.id,
                "peer": session.peer.id,
                "activity": activity_id,
                "action": action_id,
                "value": value,
                "ok": error is None,
                "error": error,
            },
        )
