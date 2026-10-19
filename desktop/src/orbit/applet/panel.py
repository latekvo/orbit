"""The popover panel: live activity cards and the devices page."""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from shiboken6 import isValid

from . import icons
from .cards import ActivityCard, ArtView, ElidedLabel
from .client import DaemonClient
from .model import activity_key, device_status_text, now_ms, parse_host_port
from .theme import Theme

PANEL_WIDTH = 384
PANEL_MAX_HEIGHT = 600
TICK_MS = 250
FORGET_CONFIRM_MS = 4000


def compute_panel_position(anchor: QRect, size: QSize, available: QRect, margin: int = 8) -> QPoint:
    """Place the panel next to `anchor` (tray icon or cursor), inside `available`.

    Below the anchor when it sits in the upper half of the screen (macOS menu bar, top
    panels), above it otherwise (bottom taskbars); horizontally centered on it, clamped."""
    cx = anchor.center().x()
    x = cx - size.width() // 2
    x = max(available.left() + margin, min(x, available.right() + 1 - margin - size.width()))
    if anchor.center().y() < available.center().y():
        y = max(anchor.bottom() + 1 + margin // 2, available.top() + margin // 2)
    else:
        y = min(anchor.top() - margin // 2 - size.height(), available.bottom() + 1 - margin - size.height())
    y = max(available.top(), min(y, available.bottom() + 1 - size.height()))
    return QPoint(x, y)


class EmptyState(QWidget):
    actionClicked = Signal()

    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 16, 28, 24)
        lay.setSpacing(4)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.glyph.setPixmap(icons.icon("orbit", theme.muted).pixmap(QSize(48, 48)))
        self.headline = QLabel()
        self.headline.setObjectName("emptyHeadline")
        self.headline.setProperty("role", "heading")
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel()
        self.hint.setObjectName("emptyHint")
        self.hint.setProperty("role", "muted")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton()
        self.button.setProperty("role", "primary")
        self.button.clicked.connect(self.actionClicked)
        lay.addStretch(1)
        lay.addWidget(self.glyph)
        lay.addSpacing(6)
        lay.addWidget(self.headline)
        lay.addWidget(self.hint)
        lay.addSpacing(8)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(1)

    def set_state(self, headline: str, hint: str, button: str | None = None) -> None:
        self.headline.setText(headline)
        self.hint.setText(hint)
        self.button.setVisible(bool(button))
        self.button.setText(button or "")


class ActivitiesPage(QWidget):
    openDevices = Signal()

    def __init__(self, client: DaemonClient, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self.theme = theme
        self.cards: dict[tuple[str, str], ActivityCard] = {}
        self._headers: list[QWidget] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.empty = EmptyState(theme)
        self.empty.actionClicked.connect(self.openDevices)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("scrollBody")
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(10, 4, 10, 10)
        self.list.setSpacing(8)
        self.list.addStretch(1)
        self.scroll.setWidget(self.body)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.scroll)
        lay.addWidget(self.stack)

        client.activitiesChanged.connect(self.refresh)
        client.devicesChanged.connect(self.refresh)
        client.connectedChanged.connect(self.refresh)
        self.refresh()

    def tick(self, now: float) -> None:
        for card in self.cards.values():
            card.tick(now)

    def refresh(self, *_: Any) -> None:
        activities = [a for a in self.client.activities if isinstance(a, dict) and a.get("id")]
        names = {d.get("id"): d.get("name") or "Phone" for d in self.client.devices}

        seen = set()
        for a in activities:
            key = activity_key(a)
            seen.add(key)
            card = self.cards.get(key)
            if card is None:
                self.cards[key] = ActivityCard(self.client, self.theme, a)
            else:
                card.update_activity(a)
        for key in list(self.cards):
            if key not in seen:
                self.cards.pop(key).deleteLater()

        # Re-lay out: group by device (in daemon order) when more than one device is involved.
        for h in self._headers:
            h.deleteLater()
        self._headers = []
        while self.list.count() > 1:
            self.list.takeAt(0)
        order: list[str] = []
        for a in activities:
            if a.get("device", "") not in order:
                order.append(a.get("device", ""))
        grouped = len(order) > 1
        pos = 0
        for dev in order:
            if grouped:
                header = QLabel(names.get(dev, dev or "Unknown device"))
                header.setProperty("role", "section")
                header.setObjectName("groupHeader")
                self._headers.append(header)
                self.list.insertWidget(pos, header)
                pos += 1
            for a in activities:
                if a.get("device", "") == dev:
                    self.list.insertWidget(pos, self.cards[activity_key(a)])
                    pos += 1

        if activities:
            self.stack.setCurrentWidget(self.scroll)
        else:
            self.stack.setCurrentWidget(self.empty)
            self._update_empty()

    def _update_empty(self) -> None:
        if not self.client.connected:
            self.empty.set_state("orbitd is not running",
                                 "Start the Orbit daemon (orbitd). This panel reconnects automatically.")
        elif not any(d.get("paired") for d in self.client.devices):
            self.empty.set_state("No phone paired yet",
                                 "Open Orbit on your phone on the same Wi-Fi or nearby with Bluetooth, "
                                 "then pair it here.", "Pair a device")
        elif not any(d.get("paired") and d.get("connected") for d in self.client.devices):
            self.empty.set_state("No phone connected",
                                 "Your paired devices are out of reach. Open Orbit on your phone or "
                                 "connect it from the devices page.", "Devices")
        else:
            self.empty.set_state("Nothing live right now",
                                 "Music, timers, downloads, calls and navigation from your phone "
                                 "show up here.")


class DeviceRow(QFrame):
    """One device. Updated in place, so it survives a provisional id turning into the real one."""

    def __init__(self, page: "DevicesPage", device: dict):
        super().__init__()
        self.setObjectName("deviceRow")
        self.page = page
        self.device: dict = {}
        self.busy = False
        self._kind: tuple | None = None
        self.buttons: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self.glyph = QLabel()
        lay.addWidget(self.glyph)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.name = ElidedLabel()
        self.name.setProperty("role", "title")
        self.status = ElidedLabel()
        self.status.setObjectName("deviceStatus")
        self.status.setProperty("role", "muted")
        texts.addWidget(self.name)
        texts.addWidget(self.status)
        lay.addLayout(texts, 1)
        self._buttons_box = QHBoxLayout()
        self._buttons_box.setSpacing(8)
        lay.addLayout(self._buttons_box)
        self._confirm = QTimer(self, singleShot=True)
        self._confirm.timeout.connect(lambda: self.buttons["Forget"].setText("Forget"))
        self.update_device(device, pairing=False)

    @property
    def device_id(self) -> str:
        return self.device.get("id", "")

    def update_device(self, device: dict, pairing: bool) -> None:
        self.device = device
        theme = self.page.theme
        color = theme.accent if device.get("connected") else theme.muted
        self.glyph.setPixmap(icons.icon("phone", color).pixmap(QSize(22, 22)))
        self.name.setFullText(device.get("name") or device.get("id", ""))
        self.status.setFullText("Confirm the pairing code" if pairing else device_status_text(device))
        self.setToolTip(device.get("address") or "")
        kind = (True, bool(device.get("connected"))) if device.get("paired") else (False,)
        if kind != self._kind:
            self._kind = kind
            self._rebuild_buttons()
        pair = self.buttons.get("Pair")
        if pair is not None and not self.busy:
            pair.setEnabled(not pairing)
            pair.setText("Pairing..." if pairing else "Pair")

    def _rebuild_buttons(self) -> None:
        while self._buttons_box.count():
            self._buttons_box.takeAt(0).widget().deleteLater()
        self.buttons = {}
        self._confirm.stop()
        if self.device.get("paired"):
            if self.device.get("connected"):
                self._button("Disconnect", lambda: self._run("devices.disconnect", "Disconnect"))
            else:
                self._button("Connect", lambda: self._run("devices.connect", "Connect", "Connecting..."))
            self._button("Forget", self._forget).setProperty("role", "danger")
        else:
            self._button("Pair", lambda: self._run("devices.connect", "Pair", "Pairing...")).setProperty(
                "role", "primary")

    def _button(self, text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(f"device:{text.lower()}")
        btn.setProperty("compact", True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        self._buttons_box.addWidget(btn)
        self.buttons[text] = btn
        return btn

    def _run(self, method: str, name: str, busy: str | None = None) -> None:
        button = self.buttons[name]
        self.busy = True
        button.setEnabled(False)
        if busy:
            button.setText(busy)

        def done(result: Any, error: dict | None) -> None:
            if not isValid(self):
                return
            self.busy = False
            if isValid(button):
                button.setEnabled(True)
                button.setText(name)
            self.update_device(self.device, self.page.pairing_pending(self.device_id))
            if error:
                self.page.show_message(f"{error.get('message') or error.get('code')}", error=True)

        # The id is read at click time: a provisional row may have been re-keyed meanwhile.
        self.page.client.request(method, {"id": self.device_id}, done)

    def _forget(self) -> None:
        forget = self.buttons["Forget"]
        if forget.text() == "Forget":
            forget.setText("Confirm?")
            self._confirm.start(FORGET_CONFIRM_MS)
            return
        self._confirm.stop()
        self._run("devices.forget", "Forget")


class DevicesPage(QWidget):
    def __init__(self, client: DaemonClient, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self.theme = theme
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("scrollBody")
        self.list = QVBoxLayout(body)
        self.list.setContentsMargins(10, 4, 10, 10)
        self.list.setSpacing(6)
        scroll.setWidget(body)
        lay.addWidget(scroll)

        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(6)
        self.list.addLayout(self.rows_box)

        add_header = QLabel("Add by address")
        add_header.setProperty("role", "section")
        self.list.addWidget(add_header)
        add_row = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setObjectName("addressField")
        self.address.setPlaceholderText("192.168.1.20:47820")
        self.address.returnPressed.connect(self.connect_address)
        self.add_button = QPushButton("Connect")
        self.add_button.setObjectName("addressConnect")
        self.add_button.clicked.connect(self.connect_address)
        add_row.addWidget(self.address, 1)
        add_row.addWidget(self.add_button)
        self.list.addLayout(add_row)
        self.message = QLabel()
        self.message.setObjectName("devicesMessage")
        self.message.setWordWrap(True)
        self.message.hide()
        self.list.addWidget(self.message)
        self.list.addStretch(1)
        self._msg_timer = QTimer(self, singleShot=True)
        self._msg_timer.timeout.connect(self.message.hide)

        self._rows: dict[str, DeviceRow] = {}
        self._extras: list[QWidget] = []
        client.devicesChanged.connect(self.refresh)
        client.pairingsChanged.connect(self.refresh)
        client.connectedChanged.connect(self.refresh)
        self.refresh()

    def pairing_pending(self, device_id: str) -> bool:
        return any(p.get("deviceId") == device_id for p in self.client.pairings)

    def refresh(self, *_: Any) -> None:
        devices = [d for d in self.client.devices
                   if d.get("paired") or d.get("discovered") or d.get("connected")]
        ids = {d.get("id") for d in devices}
        rows: dict[str, DeviceRow] = {}
        for d in devices:
            did = d.get("id", "")
            row = self._rows.get(did)
            if row is None:
                # A provisional (BLE prefix) entry is replaced by the real id: keep its row.
                row = next((r for old, r in self._rows.items()
                            if r.device.get("provisional") and old not in ids and did.startswith(old)
                            and r not in rows.values()), None)
            if row is None:
                row = DeviceRow(self, d)
            row.update_device(d, self.pairing_pending(did))
            rows[did] = row
        for row in self._rows.values():
            if row not in rows.values():
                row.deleteLater()
        self._rows = rows

        while self.rows_box.count():
            self.rows_box.takeAt(0)
        for w in self._extras:
            w.deleteLater()
        self._extras = []
        paired = [rows[d["id"]] for d in devices if d.get("paired")]
        nearby = [rows[d["id"]] for d in devices if not d.get("paired")]
        if not self.client.connected:
            self._section("orbitd is not running")
        if paired:
            self._section("Paired devices")
            for r in paired:
                self.rows_box.addWidget(r)
        if nearby:
            self._section("Nearby")
            for r in nearby:
                self.rows_box.addWidget(r)
        if self.client.connected and not devices:
            self._section("No devices found")
            hint = QLabel("Open Orbit on your phone on the same Wi-Fi network, or with Bluetooth on, "
                          "and it will appear here. You can also add it by address.")
            hint.setProperty("role", "muted")
            hint.setWordWrap(True)
            self._extras.append(hint)
            self.rows_box.addWidget(hint)
        self.add_button.setEnabled(self.client.connected)

    def _section(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setProperty("role", "section")
        self._extras.append(lbl)
        self.rows_box.addWidget(lbl)

    def show_message(self, text: str, error: bool = False) -> None:
        self.message.setProperty("role", "error" if error else "muted")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setText(text)
        self.message.show()
        self._msg_timer.start(6000)

    def connect_address(self) -> None:
        try:
            host, port = parse_host_port(self.address.text())
        except ValueError as exc:
            self.show_message(str(exc), error=True)
            return

        def done(result: Any, error: dict | None) -> None:
            self.add_button.setEnabled(self.client.connected)
            self.add_button.setText("Connect")
            if error:
                self.show_message(f"Couldn't connect to {host}:{port}: "
                                  f"{error.get('message') or error.get('code')}", error=True)
            else:
                self.address.clear()
                self.show_message(f"Connected to {host}:{port}. Confirm the pairing code if asked.")

        self.add_button.setEnabled(False)
        self.add_button.setText("Connecting...")
        self.client.request("devices.connect", {"host": host, "port": port}, done)


class Panel(QWidget):
    """Frameless popover. Hides when it loses focus, like a menu-bar popover."""

    def __init__(self, client: DaemonClient, theme: Theme, popup: bool = True):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        flags |= Qt.WindowType.Tool if popup else Qt.WindowType.Window
        self.popup = popup
        self.last_auto_hide = 0.0
        super().__init__(None, flags)
        self.setObjectName("panel")
        self.setWindowTitle("Orbit")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, popup)
        self.client = client
        self.theme = theme

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("panelFrame")
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(14, 0, 8, 4)
        self.back = QToolButton()
        self.back.setObjectName("backButton")
        self.back.setIcon(icons.icon("back", theme.text))
        self.back.setToolTip("Back")
        self.back.clicked.connect(self.show_activities)
        self.logo = ArtView(22, 6)
        self.logo.set_pixmap(icons.icon("orbit", theme.accent).pixmap(QSize(44, 44)))
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("Orbit")
        self.title.setProperty("role", "heading")
        self.status = ElidedLabel()
        self.status.setObjectName("panelStatus")
        self.status.setProperty("role", "time")
        titles.addWidget(self.title)
        titles.addWidget(self.status)
        self.devices_button = QToolButton()
        self.devices_button.setObjectName("devicesButton")
        self.devices_button.setIcon(icons.icon("phone", theme.text))
        self.devices_button.setIconSize(QSize(18, 18))
        self.devices_button.setToolTip("Devices")
        self.devices_button.clicked.connect(self.show_devices)
        header.addWidget(self.back)
        header.addWidget(self.logo)
        header.addSpacing(4)
        header.addLayout(titles, 1)
        header.addWidget(self.devices_button)
        lay.addLayout(header)

        self.pages = QStackedWidget()
        self.activities_page = ActivitiesPage(client, theme)
        self.activities_page.openDevices.connect(self.show_devices)
        self.devices_page = DevicesPage(client, theme)
        self.pages.addWidget(self.activities_page)
        self.pages.addWidget(self.devices_page)
        lay.addWidget(self.pages, 1)

        self.setFixedWidth(PANEL_WIDTH)
        self.resize(PANEL_WIDTH, 480)
        self._ticker = QTimer(self)
        self._ticker.setInterval(TICK_MS)
        self._ticker.timeout.connect(lambda: self.activities_page.tick(now_ms()))

        for sig in (client.connectedChanged, client.devicesChanged, client.activitiesChanged, client.statusChanged):
            sig.connect(self._update_header)
        client.activitiesChanged.connect(self._fit_height)
        self.show_activities()
        self._update_header()

    # -- pages -----------------------------------------------------------------------------

    def show_activities(self) -> None:
        self.pages.setCurrentWidget(self.activities_page)
        self.back.hide()
        self.logo.show()
        self.devices_button.show()
        self.title.setText("Orbit")
        self._update_header()
        self._fit_height()

    def show_devices(self, focus_address: bool = False) -> None:
        self.pages.setCurrentWidget(self.devices_page)
        self.back.show()
        self.logo.hide()
        self.devices_button.hide()
        self.title.setText("Devices")
        self._update_header()
        self._fit_height()
        if focus_address:
            self.devices_page.address.setFocus()

    def _update_header(self, *_: Any) -> None:
        c = self.client
        if not c.connected:
            text = "orbitd not running - reconnecting"
        elif self.pages.currentWidget() is self.devices_page:
            text = f"Connected to orbitd on {c.status.get('name')}" if c.status.get("name") else "Connected to orbitd"
        else:
            live = [d.get("name") or "Phone" for d in c.devices if d.get("paired") and d.get("connected")]
            text = ", ".join(live) + (" connected" if live else "") if live else "No phone connected"
        self.status.setFullText(text)

    def _fit_height(self, *_: Any) -> None:
        if self.pages.currentWidget() is self.activities_page and self.activities_page.cards:
            body = self.activities_page.body
            body.adjustSize()
            want = body.sizeHint().height() + 70
        elif self.pages.currentWidget() is self.devices_page:
            want = 460
        else:
            want = 340
        self.resize(PANEL_WIDTH, max(300, min(want, PANEL_MAX_HEIGHT)))

    # -- showing ---------------------------------------------------------------------------

    def show_at(self, pos: QPoint) -> None:
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.activities_page.tick(now_ms())
        self._ticker.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._ticker.stop()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if (self.popup and event.type() == QEvent.Type.ActivationChange and self.isVisible()
                and not self.isActiveWindow()):
            self.last_auto_hide = time.monotonic()
            self.hide()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
