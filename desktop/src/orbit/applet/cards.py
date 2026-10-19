"""Activity card: one live activity with its art, live clocks and actions."""

from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QSizePolicy,
                               QSlider, QStyle, QStyleOptionSlider, QToolButton, QVBoxLayout, QWidget)

from shiboken6 import isValid

from . import icons
from .client import DaemonClient
from .model import chrono_value_ms, format_clock, media_position_ms, now_ms
from .theme import Theme

ERROR_VISIBLE_MS = 5000
MEDIA_TRANSPORT = {"media.previous", "media.play", "media.pause", "media.next", "media.stop"}


class ElidedLabel(QLabel):
    """Single-line label that elides instead of growing the layout."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 30 else "")
        self._elide()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width(), 10)
        super().setText(QFontMetrics(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight, width))


class ArtView(QWidget):
    """Rounded image, or a tinted placeholder with the app's initial."""

    def __init__(self, size: int, radius: float, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._radius = radius
        self._pix: QPixmap | None = None
        self._color = QColor("#888888")
        self._letter = ""
        self._text_color = QColor("#ffffff")

    def set_placeholder(self, color: str, letter: str, text_color: str) -> None:
        self._color, self._letter, self._text_color = QColor(color), letter[:1].upper(), QColor(text_color)
        self.update()

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pix = pix
        self.update()

    def has_pixmap(self) -> bool:
        return self._pix is not None

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        p.setClipPath(path)
        if self._pix is not None and not self._pix.isNull():
            src = self._pix
            side = min(src.width(), src.height())
            sx, sy = (src.width() - side) / 2, (src.height() - side) / 2
            p.drawPixmap(QRectF(self.rect()), src, QRectF(sx, sy, side, side))
            return
        p.fillPath(path, self._color)
        if self._letter:
            f = QFont(self.font())
            f.setBold(True)
            f.setPixelSize(int(self.height() * 0.5))
            p.setFont(f)
            p.setPen(self._text_color)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._letter)


class SeekSlider(QSlider):
    """Slider that jumps to the clicked position and reports the final value on release."""

    seekRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sliderReleased.connect(lambda: self.seekRequested.emit(self.value()))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                     QStyle.SubControl.SC_SliderGroove, self)
                span = max(groove.width() - handle.width(), 1)
                x = int(event.position().x()) - groove.x() - handle.width() // 2
                self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span))
        super().mousePressEvent(event)


def _action_icon_name(action: dict) -> str | None:
    name = action.get("icon")
    if icons.has_icon(name):
        return name
    if not name:
        return icons.ACTION_ID_ICONS.get(action.get("id", ""))
    return None


class ActivityCard(QFrame):
    """Renders one DeviceActivity; `update_activity` reconciles in place to keep input state."""

    def __init__(self, client: DaemonClient, theme: Theme, activity: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.client = client
        self.theme = theme
        self.activity: dict = {}
        self._actions_sig: Any = None
        self._buttons: dict[str, QToolButton] = {}
        self._inputs: dict[str, QLineEdit] = {}
        self._optimistic_media: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.app_icon = ArtView(16, 4)
        self.app_name = ElidedLabel()
        self.app_name.setProperty("role", "muted")
        f = self.app_name.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.app_name.setFont(f)
        header.addWidget(self.app_icon)
        header.addWidget(self.app_name, 1)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.art = ArtView(56, 8)
        body.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.title = ElidedLabel()
        self.title.setProperty("role", "title")
        self.subtitle = ElidedLabel()
        self.subtitle.setProperty("role", "muted")
        self.text = QLabel()
        self.text.setProperty("role", "muted")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        texts.addWidget(self.text)
        texts.addStretch(1)
        body.addLayout(texts, 1)
        self.chrono = QLabel()
        self.chrono.setProperty("role", "chrono")
        self.chrono.setFont(self._tabular(self.chrono.font()))
        body.addWidget(self.chrono, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(body)

        prog_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress_text = QLabel()
        self.progress_text.setProperty("role", "time")
        prog_row.addWidget(self.progress, 1)
        prog_row.addWidget(self.progress_text)
        self.progress_row = QWidget()
        self.progress_row.setLayout(prog_row)
        prog_row.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.progress_row)

        self.seek_box = QWidget()
        seek_layout = QVBoxLayout(self.seek_box)
        seek_layout.setContentsMargins(0, 0, 0, 0)
        seek_layout.setSpacing(0)
        self.seek = SeekSlider()
        self.seek.seekRequested.connect(self._on_seek)
        times = QHBoxLayout()
        self.elapsed = QLabel()
        self.elapsed.setProperty("role", "time")
        self.duration = QLabel()
        self.duration.setProperty("role", "time")
        for lbl in (self.elapsed, self.duration):
            lbl.setFont(self._tabular(lbl.font()))
        times.addWidget(self.elapsed)
        times.addStretch(1)
        times.addWidget(self.duration)
        seek_layout.addWidget(self.seek)
        seek_layout.addLayout(times)
        root.addWidget(self.seek_box)

        self.actions_box = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_box)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(6)
        root.addWidget(self.actions_box)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        self.error.hide()
        self._error_timer = QTimer(self, singleShot=True)
        self._error_timer.timeout.connect(self.error.hide)
        root.addWidget(self.error)

        client.artLoaded.connect(self._on_art_loaded)
        self.update_activity(activity)

    @staticmethod
    def _tabular(font: QFont) -> QFont:
        font = QFont(font)
        font.setFeature(QFont.Tag("tnum"), 1)
        return font

    # -- data ------------------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self.activity.get("device", "")

    @property
    def activity_id(self) -> str:
        return self.activity.get("id", "")

    def accent(self) -> str:
        color = QColor(self.activity.get("color") or "")
        return color.name() if color.isValid() else self.theme.accent

    def update_activity(self, activity: dict) -> None:
        self.activity = copy.deepcopy(activity)
        self._optimistic_media = None
        a = self.activity
        app = a.get("app") or {}
        app_name = app.get("name") or app.get("id") or ""
        accent = self.accent()

        self.app_name.setFullText(app_name)
        self.app_icon.set_placeholder(accent, app_name, "#ffffff")
        self.title.setFullText(a.get("title") or "")
        self.subtitle.setFullText(a.get("subtitle") or "")
        self.subtitle.setVisible(bool(a.get("subtitle")))
        self.text.setText(a.get("text") or "")
        self.text.setVisible(bool(a.get("text")))

        self.art.setVisible(bool(a.get("artKey")))
        self.art.set_placeholder(self.theme.mix(accent, 0.25), "", "#ffffff")
        self._load_art()

        self.chrono.setVisible(bool(a.get("chrono")))

        prog = a.get("progress")
        self.progress_row.setVisible(bool(prog))
        if prog:
            maximum = int(prog.get("max") or 0)
            if prog.get("indeterminate") or maximum <= 0:
                self.progress.setRange(0, 0)
                self.progress_text.hide()
            else:
                value = max(0, min(int(prog.get("value") or 0), maximum))
                self.progress.setRange(0, maximum)
                self.progress.setValue(value)
                self.progress_text.setText(f"{round(100 * value / maximum)}%")
                self.progress_text.show()
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}")

        media = a.get("media") or {}
        has_seek = bool(media.get("durationMs"))
        self.seek_box.setVisible(has_seek)
        if has_seek:
            if not self.seek.isSliderDown():
                self.seek.setRange(0, int(media["durationMs"]))
            self.seek.setEnabled(bool(media.get("seekable")))
            self.duration.setText(format_clock(media["durationMs"]))
            self.seek.setStyleSheet(
                f"QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}")

        self._rebuild_actions()
        self.tick(now_ms())

    def _load_art(self) -> None:
        a = self.activity
        self.app_icon.set_pixmap(self.client.art(self.device_id, a.get("iconKey")))
        self.art.set_pixmap(self.client.art(self.device_id, a.get("artKey")))

    def _on_art_loaded(self, key: str) -> None:
        if key in (self.activity.get("iconKey"), self.activity.get("artKey")):
            self._load_art()

    def _media(self) -> dict:
        return self._optimistic_media or self.activity.get("media") or {}

    def tick(self, now: float) -> None:
        chrono = self.activity.get("chrono")
        if chrono:
            self.chrono.setText(format_clock(chrono_value_ms(chrono, now)))
        media = self._media()
        if media.get("durationMs"):
            if not self.seek.isSliderDown():
                pos = media_position_ms(media, now)
                self.seek.blockSignals(True)
                self.seek.setValue(int(pos))
                self.seek.blockSignals(False)
                self.elapsed.setText(format_clock(pos))
            else:
                self.elapsed.setText(format_clock(self.seek.value()))

    # -- actions ---------------------------------------------------------------------------

    def _rebuild_actions(self) -> None:
        actions = [x for x in self.activity.get("actions") or [] if isinstance(x, dict) and x.get("id")]
        is_media = self.activity.get("kind") == "media"
        sig = (is_media, [(x.get("id"), x.get("label"), x.get("icon"), bool(x.get("input"))) for x in actions])
        if sig == self._actions_sig:
            return
        self._actions_sig = sig
        drafts = {aid: le.text() for aid, le in self._inputs.items()}
        while self.actions_layout.count():
            item = self.actions_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear_layout(item.layout())
        self._buttons.clear()
        self._inputs.clear()

        plain = [x for x in actions if not x.get("input")]
        inputs = [x for x in actions if x.get("input")]
        if plain:
            row = QHBoxLayout()
            row.setSpacing(6)
            transport = [x for x in plain if is_media and x["id"] in MEDIA_TRANSPORT]
            others = [x for x in plain if x not in transport]
            if transport:
                row.addStretch(1)
                for x in transport:
                    row.addWidget(self._make_button(x, media=True))
                row.addStretch(1)
                for x in others:
                    row.addWidget(self._make_button(x, media=True))
            else:
                for x in others:
                    row.addWidget(self._make_button(x, media=False))
                row.addStretch(1)
            self.actions_layout.addLayout(row)
        for x in inputs:
            self.actions_layout.addWidget(self._make_input(x, drafts.get(x["id"], "")))
        self.actions_box.setVisible(bool(actions))

    def _make_button(self, action: dict, media: bool) -> QToolButton:
        aid = action["id"]
        label = action.get("label") or aid
        btn = QToolButton()
        btn.setObjectName(f"action:{aid}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(label)
        btn.setAccessibleName(label)
        icon_name = _action_icon_name(action)
        primary = media and aid in ("media.play", "media.pause")
        if icon_name:
            color = "#ffffff" if primary else self.theme.text
            btn.setIcon(icons.icon(icon_name, color))
        if media and icon_name:
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setIconSize(QSize(24, 24) if primary else QSize(22, 22))
            btn.setProperty("role", "primary" if primary else "media")
            if primary:
                btn.setStyleSheet(f"QToolButton {{ background: {self.accent()}; border-radius: 20px; }}"
                                  f"QToolButton:hover {{ background: {QColor(self.accent()).lighter(115).name()}; }}")
        else:
            btn.setText(label)
            btn.setIconSize(QSize(16, 16))
            btn.setProperty("role", "pill")
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if icon_name
                                   else Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.clicked.connect(lambda: self.trigger(aid))
        self._buttons[aid] = btn
        return btn

    def _make_input(self, action: dict, draft: str) -> QWidget:
        aid = action["id"]
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        field = QLineEdit(draft)
        field.setObjectName(f"input:{aid}")
        field.setPlaceholderText((action.get("input") or {}).get("label") or action.get("label") or "Reply")
        field.setClearButtonEnabled(True)
        send = QToolButton()
        send.setObjectName(f"action:{aid}")
        send.setToolTip(action.get("label") or "Send")
        send.setIcon(icons.icon("send", self.accent()))
        send.setIconSize(QSize(20, 20))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setEnabled(bool(draft.strip()))
        field.textChanged.connect(lambda t: send.setEnabled(bool(t.strip())))
        submit = lambda: self.trigger(aid, field.text()) if field.text().strip() else None  # noqa: E731
        field.returnPressed.connect(submit)
        send.clicked.connect(submit)
        row.addWidget(field, 1)
        row.addWidget(send)
        self._buttons[aid] = send
        self._inputs[aid] = field
        return box

    def trigger(self, action_id: str, value: Any = None) -> None:
        btn = self._buttons.get(action_id)
        if btn is not None:
            btn.setEnabled(False)
        field = self._inputs.get(action_id)
        if field is not None:
            field.setEnabled(False)
        device, activity = self.device_id, self.activity_id

        def done(result: Any, error: dict | None) -> None:
            if not _alive(self):
                return
            ok = error is None and isinstance(result, dict) and result.get("ok") is True
            if btn is not None and _alive(btn):
                btn.setEnabled(True if field is None else bool(field.text().strip()))
            if field is not None and _alive(field):
                field.setEnabled(True)
                if ok:
                    field.clear()
            if action_id == "media.seek" and not ok:
                self._optimistic_media = None
                self.tick(now_ms())
            if not ok:
                reason = (error or {}).get("message") or (result or {}).get("error") or "failed"
                self.show_error(f"{self._action_label(action_id)}: {reason}")

        self.client.action(device, activity, action_id, value, done)

    def _action_label(self, action_id: str) -> str:
        if action_id == "media.seek":
            return "Seek"
        for x in self.activity.get("actions") or []:
            if x.get("id") == action_id:
                return x.get("label") or action_id
        return action_id

    def _on_seek(self, value: int) -> None:
        media = self.activity.get("media") or {}
        if not media.get("seekable"):
            return
        self._optimistic_media = {**media, "positionMs": value, "positionAtMs": now_ms()}
        self.trigger("media.seek", int(value))
        self.tick(now_ms())

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
        self._error_timer.start(ERROR_VISIBLE_MS)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())


def _alive(obj) -> bool:
    return isValid(obj)
