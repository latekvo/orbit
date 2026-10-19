"""Pairing confirmation: shows the 6-digit SAS and answers `pairing.respond`."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import icons
from .client import DaemonClient
from .model import format_sas
from .theme import Theme


class PairingDialog(QDialog):
    def __init__(self, client: DaemonClient, theme: Theme, pairing: dict, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("pairingDialog")
        self.client = client
        self.pairing: dict = {}
        self._answered = False
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
        self._answered = True
        self.accept_btn.setEnabled(False)
        self.cancel.setEnabled(accept)
        if accept:
            self.state.setText(f"Waiting for {self.pairing.get('name') or 'your phone'} to confirm...")
            self.state.show()

        def done(result: Any, error: dict | None) -> None:
            if error:
                self._answered = False
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


class PairingManager(QObject):
    """One dialog per pending pairing session, kept in sync with `pairing.changed`."""

    pairingStarted = Signal(dict)
    pairingFinished = Signal(dict)

    def __init__(self, client: DaemonClient, theme: Theme):
        super().__init__()
        self.client = client
        self.theme = theme
        self.dialogs: dict[str, PairingDialog] = {}
        client.pairingsChanged.connect(self.sync)

    def sync(self, pairings: list) -> None:
        live = {p.get("session"): p for p in pairings if isinstance(p, dict) and p.get("session")}
        for session in list(self.dialogs):
            if session not in live:
                dlg = self.dialogs.pop(session)
                dlg._answered = True
                dlg.close()
                dlg.deleteLater()
                self.pairingFinished.emit(dlg.pairing)
        for session, p in live.items():
            dlg = self.dialogs.get(session)
            if dlg is None:
                dlg = PairingDialog(self.client, self.theme, p)
                self.dialogs[session] = dlg
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
                self.pairingStarted.emit(p)
            else:
                dlg.update_pairing(p)
