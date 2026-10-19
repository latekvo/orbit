"""orbitd local-API client on the Qt event loop (QLocalSocket, no threads)."""

from __future__ import annotations

import base64
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QLocalSocket

from .model import ORBITD_DOWN, ORBITD_UNRESPONSIVE

log = logging.getLogger("orbit.applet.client")

Callback = Callable[[Any, dict | None], None]
"""Called with (result, None) on success or (None, {"code", "message"}) on failure."""

RECONNECT_MIN_MS = 500
RECONNECT_MAX_MS = 5000
# Requests that reach the phone wait longer than orbitd's own bound for them (activities.action
# answers within ~12 s); the rest are answered locally and fast.
REQUEST_TIMEOUTS_MS = {
    "activities.action": 20_000,
    "art.get": 20_000,
    "devices.connect": 45_000,  # BLE connect + handshake
    "devices.disconnect": 20_000,
    "devices.forget": 20_000,
    "pairing.respond": 20_000,
}
QUICK_TIMEOUT_MS = 5_000
# After a timeout, a `status` ping tells a slow phone from an unresponsive orbitd.
PROBE_TIMEOUT_MS = 3_000
ART_CACHE_SIZE = 64


class DaemonClient(QObject):
    connectedChanged = Signal(bool)
    statusChanged = Signal(dict)
    devicesChanged = Signal(list)
    pairingsChanged = Signal(list)
    activitiesChanged = Signal(list)
    artLoaded = Signal(str)  # art key

    def __init__(self, socket_path: str, parent: QObject | None = None):
        super().__init__(parent)
        self.socket_path = str(socket_path)
        self.connected = False
        self.status: dict = {}
        self.devices: list[dict] = []
        self.pairings: list[dict] = []
        self.activities: list[dict] = []
        self._next_id = 1
        self._pending: dict[int, tuple[Callback | None, QTimer, bool]] = {}
        self.timeouts_ms = dict(REQUEST_TIMEOUTS_MS)
        self.quick_timeout_ms = QUICK_TIMEOUT_MS
        self.probe_timeout_ms = PROBE_TIMEOUT_MS
        self._buf = b""
        self._art: OrderedDict[str, QPixmap] = OrderedDict()
        self._art_requested: set[str] = set()
        self._backoff = RECONNECT_MIN_MS
        self._stopped = False

        self._sock = QLocalSocket(self)
        self._sock.connected.connect(self._on_connected)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.errorOccurred.connect(self._on_error)
        self._sock.readyRead.connect(self._on_ready_read)
        self._retry = QTimer(self, singleShot=True)
        self._retry.timeout.connect(self._connect)

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        self._stopped = False
        self._connect()

    def stop(self) -> None:
        self._stopped = True
        self._retry.stop()
        self._sock.abort()
        self._set_disconnected()

    def _connect(self) -> None:
        if self._stopped or self._sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        log.debug("connecting to %s", self.socket_path)
        self._sock.connectToServer(self.socket_path)

    def _on_connected(self) -> None:
        log.info("connected to orbitd at %s", self.socket_path)
        self._backoff = RECONNECT_MIN_MS
        self._buf = b""
        self.connected = True
        self._art_requested.clear()
        self.connectedChanged.emit(True)
        self.request("status", callback=self._on_status)
        self.request("subscribe")

    def _on_status(self, result: Any, error: dict | None) -> None:
        if isinstance(result, dict):
            self.status = result
            self.statusChanged.emit(result)

    def _on_error(self, err: QLocalSocket.LocalSocketError) -> None:
        if err != QLocalSocket.LocalSocketError.PeerClosedError:
            log.debug("socket error: %s (%s)", err, self._sock.errorString())
        # Emitted before the socket leaves ConnectingState, so look again once it has settled.
        QTimer.singleShot(0, self._check_unconnected)

    def _check_unconnected(self) -> None:
        if self._sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self._on_disconnected()

    def _on_disconnected(self) -> None:
        if self.connected and not self._stopped:
            log.warning("lost connection to orbitd")
        self._set_disconnected()
        if not self._stopped and not self._retry.isActive():
            self._retry.start(self._backoff)
            self._backoff = min(self._backoff * 2, RECONNECT_MAX_MS)

    def _set_disconnected(self) -> None:
        pending, self._pending = self._pending, {}
        for cb, timer, _ in pending.values():
            timer.stop()
            if cb:
                cb(None, {"code": ORBITD_DOWN, "message": "orbitd is not running"})
        was = self.connected
        self.connected = False
        if self.devices or self.pairings or self.activities:
            self.devices, self.pairings, self.activities = [], [], []
            self.devicesChanged.emit([])
            self.pairingsChanged.emit([])
            self.activitiesChanged.emit([])
        if was:
            self.connectedChanged.emit(False)

    # -- requests --------------------------------------------------------------------------

    def request(
        self, method: str, params: dict | None = None, callback: Callback | None = None, *, _probe: bool = False
    ) -> int | None:
        if not self.connected:
            if callback:
                QTimer.singleShot(0, lambda: callback(None, {"code": ORBITD_DOWN, "message": "orbitd is not running"}))
            return None
        rid = self._next_id
        self._next_id += 1
        msg = {"id": rid, "method": method, "params": params or {}}
        timer = QTimer(self, singleShot=True)
        timer.timeout.connect(lambda: self._on_timeout(rid))
        timer.start(self.probe_timeout_ms if _probe else self.timeouts_ms.get(method, self.quick_timeout_ms))
        self._pending[rid] = (callback, timer, _probe)
        log.debug("-> %s", msg)
        self._sock.write(QByteArray(json.dumps(msg).encode() + b"\n"))
        self._sock.flush()
        return rid

    def _on_timeout(self, rid: int) -> None:
        entry = self._pending.pop(rid, None)
        if entry is None or entry[0] is None:
            return
        cb, _, probe = entry
        unresponsive = {"code": ORBITD_UNRESPONSIVE, "message": "orbitd isn't responding"}
        if probe:
            cb(None, unresponsive)
            return
        log.warning("request %s timed out; pinging orbitd", rid)
        self.request(
            "status",
            None,
            lambda r, e: cb(
                None, unresponsive if e else {"code": "timeout", "message": "no answer from the phone in time"}
            ),
            _probe=True,
        )

    def _on_ready_read(self) -> None:
        self._buf += bytes(self._sock.readAll().data())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("ignoring malformed line from orbitd: %r", line[:200])
                continue
            if isinstance(msg, dict):
                self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "event" in msg:
            self._on_event(msg["event"], msg.get("data") or {})
            return
        entry = self._pending.pop(msg.get("id"), None) if isinstance(msg.get("id"), int) else None
        if entry is None:
            return
        cb, timer, _ = entry
        timer.stop()
        if cb is None:
            if "error" in msg:
                log.warning("request %s failed: %s", msg.get("id"), msg["error"])
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"code": "failed", "message": str(msg["error"])}
            cb(None, err)
        else:
            cb(msg.get("result"), None)

    def _on_event(self, event: str, data: dict) -> None:
        log.debug("<- event %s", event)
        if event == "devices.changed":
            self.devices = list(data.get("devices") or [])
            self.devicesChanged.emit(self.devices)
        elif event == "pairing.changed":
            self.pairings = list(data.get("pairings") or [])
            self.pairingsChanged.emit(self.pairings)
        elif event == "activities.changed":
            self.activities = list(data.get("activities") or [])
            self.activitiesChanged.emit(self.activities)

    # -- API helpers -----------------------------------------------------------------------

    def action(
        self, device: str, activity: str, action: str, value: Any = None, callback: Callback | None = None
    ) -> int | None:
        params: dict[str, Any] = {"device": device, "activity": activity, "action": action}
        if value is not None:
            params["value"] = value
        return self.request("activities.action", params, callback)

    def art(self, device: str, key: str | None) -> QPixmap | None:
        """Cached art pixmap, or None while it is being fetched (artLoaded fires when ready)."""
        if not key:
            return None
        pix = self._art.get(key)
        if pix is not None:
            self._art.move_to_end(key)
            return pix
        if key not in self._art_requested and self.connected:
            self._art_requested.add(key)
            self.request("art.get", {"device": device, "key": key}, lambda r, e, k=key: self._on_art(k, r, e))
        return None

    def _on_art(self, key: str, result: Any, error: dict | None) -> None:
        if error or not isinstance(result, dict):
            log.debug("art %s unavailable: %s", key, error)
            if error and error.get("code") in ("not_connected", "timeout", ORBITD_DOWN, ORBITD_UNRESPONSIVE):
                self._art_requested.discard(key)
            return
        pix = QPixmap()
        try:
            ok = pix.loadFromData(base64.b64decode(result.get("data", "")))
        except ValueError:
            ok = False
        if not ok:
            log.warning("art %s is not a valid image", key)
            return
        self._art[key] = pix
        while len(self._art) > ART_CACHE_SIZE:
            evicted, _ = self._art.popitem(last=False)
            self._art_requested.discard(evicted)
        self.artLoaded.emit(key)
