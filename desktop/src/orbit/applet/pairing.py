"""Pairing confirmation: shows the 6-digit SAS and answers `pairing.respond`."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from . import icons
from .client import DaemonClient
from .model import connect_failure, format_sas, parse_host_port
from .theme import Theme


class PairingDialog(QDialog):
    def __init__(self, client: DaemonClient, theme: Theme, pairing: dict, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("pairingDialog")
        self.client = client
        self.pairing: dict = {}
        self.device: dict = {}  # the device being paired, as last listed while the session was live
        self._answered = False
        self.declined = False
        self.failed = False
        self.setWindowTitle("Pair device - Orbit")
        self.setMinimumWidth(380)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(10)
        glyph = QLabel()
        glyph.setPixmap(icons.icon("orbit", theme.accent).pixmap(QSize(40, 40)))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline = QLabel()
        self.headline.setProperty("role", "heading")
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel()
        self.hint.setProperty("role", "muted")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code = QLabel()
        self.code.setObjectName("sas")
        self.code.setProperty("role", "sas")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(44)
        mono.setBold(True)
        self.code.setFont(mono)
        self.state = QLabel()
        self.state.setObjectName("pairingState")
        self.state.setProperty("role", "muted")
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state.setWordWrap(True)

        buttons = QHBoxLayout()
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("pairCancel")
        self.accept_btn = QPushButton("Codes match")
        self.accept_btn.setObjectName("pairAccept")
        self.accept_btn.setProperty("role", "primary")
        self.accept_btn.setDefault(True)
        self.cancel.clicked.connect(lambda: self.respond(False))
        self.accept_btn.clicked.connect(lambda: self.respond(True))
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.accept_btn)

        for w in (glyph, self.headline, self.hint, self.code, self.state):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addLayout(buttons)
        self.update_pairing(pairing)

    @property
    def session(self) -> str:
        return self.pairing.get("session", "")

    def update_pairing(self, pairing: dict) -> None:
        self.pairing = dict(pairing)
        name = pairing.get("name") or "your phone"
        self.headline.setText(f"Pair with {name}?")
        self.hint.setText(f"Check that {name} shows the same code, then confirm on both devices.")
        self.code.setText(format_sas(str(pairing.get("sas", ""))))
        if pairing.get("localAccepted"):
            self.state.setText(f"Waiting for {name} to confirm...")
            self.accept_btn.setEnabled(False)
        elif pairing.get("remoteAccepted"):
            self.state.setText(f"{name} has confirmed the code.")
        else:
            self.state.setText("")
        self.state.setVisible(bool(self.state.text()))

    def respond(self, accept: bool) -> None:
        if self.failed:
            if accept:
                self.retry()
            else:
                self.close()
            return
        self._answered = True
        self.declined = not accept
        self.accept_btn.setEnabled(False)
        self.cancel.setEnabled(accept)
        if accept:
            self.state.setText(f"Waiting for {self.pairing.get('name') or 'your phone'} to confirm...")
            self.state.show()

        def done(result: Any, error: dict | None) -> None:
            if not isValid(self):  # the daemon resolved the pairing first; the manager closed us
                return
            if error:
                self._answered = False
                self.declined = False
                self.accept_btn.setEnabled(True)
                self.cancel.setEnabled(True)
                self.state.setText(f"Couldn't respond: {error.get('message') or error.get('code')}")
                self.state.show()
            elif not accept:
                self.done(QDialog.DialogCode.Rejected)

        self.client.request("pairing.respond", {"session": self.session, "accept": accept}, done)

    def reject(self) -> None:
        """Window close / Escape cancels the pairing."""
        if not self._answered:
            self.respond(False)
        super().reject()

    def show_failed(self, reason: str) -> None:
        """The session ended without pairing: say why and offer to try again."""
        self.failed = self._answered = True
        name = self.pairing.get("name") or "your phone"
        self.headline.setText(f"Pairing with {name} failed")
        self.hint.setText(reason)
        self.code.hide()
        self.state.hide()
        self.cancel.setText("Close")
        self.cancel.setEnabled(True)
        self.accept_btn.setText("Try again")
        self.client.connectedChanged.connect(self._sync_retry)
        self._sync_retry()

    def _sync_retry(self, *_: Any) -> None:
        if isValid(self):
            self.accept_btn.setEnabled(self.client.connected)

    def retry(self) -> None:
        """devices.connect again; the new session opens its own dialog."""
        dev = self.device
        params: dict[str, Any] = {"id": dev.get("id") or self.pairing.get("deviceId")}
        if dev.get("transport") == "lan" and dev.get("address"):
            try:
                host, port = parse_host_port(dev["address"])
                params = {"host": host, "port": port}
            except ValueError:
                pass
        self.accept_btn.setEnabled(False)
        self.state.setText("Connecting...")
        self.state.show()

        def done(result: Any, error: dict | None) -> None:
            if not isValid(self):
                return
            if error:
                self.state.setText(connect_failure(error, self.pairing.get("name") or "the phone"))
                self._sync_retry()
            else:
                self.close()

        self.client.request("devices.connect", params, done)


# A stopping orbitd ends its sessions before it closes the socket: wait this long for the
# disconnect before blaming the phone or the link.
FAILURE_SETTLE_MS = 500


class PairingManager(QObject):
    """One dialog per pending pairing session, kept in sync with `pairing.changed`."""

    pairingStarted = Signal(dict)
    pairingFinished = Signal(dict)
    pairingFailed = Signal(dict, str)  # pairing, reason

    def __init__(self, client: DaemonClient, theme: Theme):
        super().__init__()
        self.client = client
        self.theme = theme
        self.dialogs: dict[str, PairingDialog] = {}  # by live session
        self.failed: list[PairingDialog] = []  # sessions that ended unpaired, still on screen
        client.pairingsChanged.connect(self.sync)
        client.devicesChanged.connect(self._track_devices)

    def _track_devices(self, devices: list) -> None:
        for dlg in self.dialogs.values():
            dev = next((d for d in devices if d.get("id") == dlg.pairing.get("deviceId")), None)
            if dev is not None:
                dlg.device = dev

    def _failure_reason(self, dlg: PairingDialog) -> str | None:
        """Why the session of `dlg` ended without pairing, or None if it paired or the user declined."""
        if dlg.declined:
            return None
        name = dlg.pairing.get("name") or "the phone"
        if not self.client.connected:
            return "orbitd stopped before pairing finished."
        dev = next((d for d in self.client.devices if d.get("id") == dlg.pairing.get("deviceId")), None)
        if dev is not None and dev.get("paired"):
            return None
        if dlg.pairing.get("remoteAccepted"):
            return f"The connection to {name} was lost before pairing finished."
        return f"{name} declined the code, or the connection was lost before it confirmed."

    def _fail(self, dlg: PairingDialog) -> None:
        if dlg not in self.failed or not dlg.isVisible():
            return
        reason = self._failure_reason(dlg)
        if reason is None:
            self.failed.remove(dlg)
            dlg.close()
            self.pairingFinished.emit(dlg.pairing)
            return
        dlg.show_failed(reason)
        self.pairingFailed.emit(dlg.pairing, reason)

    def sync(self, pairings: list) -> None:
        live = {p.get("session"): p for p in pairings if isinstance(p, dict) and p.get("session")}
        self.failed = [d for d in self.failed if isValid(d) and d.isVisible()]
        for session in list(self.dialogs):
            if session not in live:
                dlg = self.dialogs.pop(session)
                reason = self._failure_reason(dlg)
                dlg._answered = True  # the session is gone: closing must not answer it
                if reason is None:
                    dlg.close()
                    dlg.deleteLater()
                    self.pairingFinished.emit(dlg.pairing)
                else:
                    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                    self.failed.append(dlg)
                    QTimer.singleShot(FAILURE_SETTLE_MS, dlg, lambda d=dlg: self._fail(d))
        new_devices = {p.get("deviceId") for s, p in live.items() if s not in self.dialogs}
        for dlg in [d for d in self.failed if d.pairing.get("deviceId") in new_devices]:
            self.failed.remove(dlg)
            dlg.close()  # a new attempt for the same device replaces it
        for session, p in live.items():
            dlg = self.dialogs.get(session)
            if dlg is None:
                dlg = PairingDialog(self.client, self.theme, p)
                self.dialogs[session] = dlg
                self._track_devices(self.client.devices)
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
                self.pairingStarted.emit(p)
            else:
                dlg.update_pairing(p)
