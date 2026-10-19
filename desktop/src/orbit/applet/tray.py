"""Tray / menu-bar icon, its menu, and panel placement."""

from __future__ import annotations

import logging
import sys
import time

from PySide6.QtCore import QObject, QRect, QSize, QTimer
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import icons
from .client import DaemonClient
from .pairing import PairingManager
from .panel import Panel, compute_panel_position
from .theme import Theme

log = logging.getLogger("orbit.applet.tray")

# A click on the tray icon first deactivates (and so hides) an open panel; a toggle arriving
# this soon after that auto-hide is the same click and must not re-open it.
REOPEN_GUARD_S = 0.35


def tray_icon(theme: Theme) -> QIcon:
    if sys.platform == "darwin":
        ic = icons.icon("orbit", "#000000")
        ic.setIsMask(True)  # template image: macOS tints it for light/dark menu bars
        return ic
    return icons.icon("orbit", "#e8e8ec" if theme.dark else "#2b2b30")


class TrayController(QObject):
    def __init__(self, client: DaemonClient, theme: Theme):
        super().__init__()
        self.client = client
        self.theme = theme
        self.panel = Panel(client, theme)
        self.pairing = PairingManager(client, theme)
        self.pairing.pairingStarted.connect(self._on_pairing_started)
        self.pairing.pairingFinished.connect(self._on_pairing_finished)

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._rebuild_menu)
        self._rebuild_menu()

        self.tray = QSystemTrayIcon(tray_icon(theme))
        self.tray.setToolTip("Orbit")
        self.tray.activated.connect(self._on_activated)
        if sys.platform != "darwin":
            # StatusNotifierItem hosts show this on secondary click. On macOS a context menu
            # would hijack the primary click, so it is popped up manually on Context instead.
            self.tray.setContextMenu(self.menu)
        client.connectedChanged.connect(self._update_tooltip)
        client.devicesChanged.connect(self._update_tooltip)

    def show(self) -> None:
        self.tray.show()
        log.info(
            "tray icon created: system tray available=%s, visible=%s, geometry=%s",
            QSystemTrayIcon.isSystemTrayAvailable(),
            self.tray.isVisible(),
            _rect(self.tray.geometry()),
        )

    def hide(self) -> None:
        self.tray.hide()
        self.panel.hide()

    def set_theme(self, theme: Theme) -> None:
        """Rebuild themed widgets (icons are tinted at creation)."""
        visible = self.panel.isVisible()
        self.theme = theme
        old = self.panel
        self.panel = Panel(self.client, theme)
        old.hide()
        old.deleteLater()
        self.pairing.theme = theme
        self.tray.setIcon(tray_icon(theme))
        if visible:
            self.show_panel()

    # -- tray ------------------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        log.debug("tray activated: %s", reason)
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_panel()
        elif reason == QSystemTrayIcon.ActivationReason.Context and sys.platform == "darwin":
            self.panel.hide()
            self.menu.popup(QCursor.pos())

    def _update_tooltip(self, *_) -> None:
        if not self.client.connected:
            self.tray.setToolTip("Orbit - orbitd not running")
            return
        live = [d.get("name") or "Phone" for d in self.client.devices if d.get("paired") and d.get("connected")]
        self.tray.setToolTip("Orbit - " + (", ".join(live) if live else "no phone connected"))

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        elif time.monotonic() - self.panel.last_auto_hide > REOPEN_GUARD_S:
            self.show_panel()

    def show_panel(self, devices: bool = False, focus_address: bool = False) -> None:
        if devices:
            self.panel.show_devices(focus_address=focus_address)
        else:
            self.panel.show_activities()
        anchor = self.tray.geometry()
        if anchor.isEmpty() or QGuiApplication.screenAt(anchor.center()) is None:
            # Most StatusNotifierItem hosts report no geometry, and macOS reports an off-screen
            # one until the status item has been laid out.
            anchor = QRect(QCursor.pos(), QSize(1, 1))
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        pos = compute_panel_position(anchor, self.panel.size(), screen.availableGeometry())
        log.info("showing panel at (%d, %d), anchor=%s", pos.x(), pos.y(), _rect(anchor))
        self.panel.show_at(pos)
        QTimer.singleShot(
            300,
            lambda: log.info(
                "panel visible=%s exposed=%s active=%s geometry=%s",
                self.panel.isVisible(),
                bool(self.panel.windowHandle() and self.panel.windowHandle().isExposed()),
                self.panel.isActiveWindow(),
                _rect(self.panel.geometry()),
            ),
        )

    # -- menu ------------------------------------------------------------------------------

    def _rebuild_menu(self) -> None:
        m = self.menu
        m.clear()
        c = self.client
        status = (
            f"orbitd {c.status.get('version', '')} - connected".replace("  ", " ")
            if c.connected
            else "orbitd not running"
        )
        head = m.addAction(status)
        head.setEnabled(False)
        m.addAction("Show activities", self.show_panel)
        m.addSeparator()
        devices = m.addMenu("Devices")
        paired = [d for d in c.devices if d.get("paired")]
        for d in paired:
            state = "connected" if d.get("connected") else "offline"
            act = devices.addAction(f"{d.get('name') or d.get('id')} - {state}")
            act.triggered.connect(lambda *_: self.show_panel(devices=True))
        if not paired:
            none = devices.addAction("No paired devices")
            none.setEnabled(False)
        devices.addSeparator()
        devices.addAction("Manage devices...", lambda: self.show_panel(devices=True))
        m.addAction("Pair new device...", lambda: self.show_panel(devices=True))
        m.addAction("Add device by address...", lambda: self.show_panel(devices=True, focus_address=True))
        m.addSeparator()
        quit_action = QAction("Quit Orbit", m)
        quit_action.triggered.connect(QApplication.quit)
        m.addAction(quit_action)

    # -- pairing ---------------------------------------------------------------------------

    def _on_pairing_started(self, pairing: dict) -> None:
        if self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(
                "Orbit pairing",
                f"Confirm the code {pairing.get('sas', '')} on {pairing.get('name') or 'your phone'}",
                QSystemTrayIcon.MessageIcon.Information,
                8000,
            )

    def _on_pairing_finished(self, pairing: dict) -> None:
        dev = next((d for d in self.client.devices if d.get("id") == pairing.get("deviceId")), None)
        if dev and dev.get("paired") and self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(
                "Orbit", f"Paired with {dev.get('name') or 'your phone'}", QSystemTrayIcon.MessageIcon.Information, 4000
            )


def _rect(r: QRect) -> str:
    return f"{r.x()},{r.y()} {r.width()}x{r.height()}"
