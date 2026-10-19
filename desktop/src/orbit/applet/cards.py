"""Activity card: one live activity with its art, live clocks and actions."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from . import actions as acts
from . import icons
from .client import DaemonClient
from .model import (
    INT32_MAX,
    action_failure,
    chrono_value_ms,
    format_clock,
    media_position_ms,
    now_ms,
    sanitize_activity,
)
from .theme import Theme

ERROR_VISIBLE_MS = 5000
SPIN_FRAME_MS = 60
KEY_SEEK_SETTLE_MS = 400
STALE_OPACITY = 0.55
PILL_MAX_TEXT = 120


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


class ClampedLabel(QLabel):
    """Wrapped text clamped to `max_lines` with the last line elided; long words break anywhere."""

    def __init__(self, max_lines: int = 3, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self._max_lines = max_lines
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setFullText(self, text: str) -> None:
        self._full = (text or "").replace("\r\n", "\n").replace("\n", "\u2028")
        self._relayout()

    def fullText(self) -> str:
        return self._full.replace("\u2028", "\n")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        width = max(self.width(), 10)
        layout = QTextLayout(self._full, self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)
        layout.beginLayout()
        spans = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            spans.append((line.textStart(), line.textLength()))
        layout.endLayout()
        fm = QFontMetrics(self.font())
        lines = []
        for i, (start, length) in enumerate(spans[: self._max_lines]):
            if i == self._max_lines - 1 and len(spans) > self._max_lines:
                rest = self._full[start:].replace("\u2028", " ")
                lines.append(fm.elidedText(rest, Qt.TextElideMode.ElideRight, width))
            else:
                lines.append(self._full[start : start + length].replace("\u2028", "").rstrip())
        text = "\n".join(lines)
        clamped = len(spans) > self._max_lines
        self.setToolTip(self.fullText() if clamped else "")
        if text != self.text():
            super().setText(text)


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
        self.setAccessibleName("Playback position")
        self.sliderReleased.connect(lambda: self.seekRequested.emit(self.value()))
        # Arrow-key steps are batched into one seek once the keys go quiet.
        self._key_seek = QTimer(self, singleShot=True, interval=KEY_SEEK_SETTLE_MS)
        self._key_seek.timeout.connect(lambda: self.seekRequested.emit(self.value()))

    def is_held(self) -> bool:
        """The user owns the value: dragging, or a keyboard seek not sent yet."""
        return self.isSliderDown() or self._key_seek.isActive()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        before = self.value()
        super().keyPressEvent(event)
        if self.value() != before:
            self._key_seek.start()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()  # the panel scrolls; a wheel tick is too easy to trigger as a seek

    def hideEvent(self, event) -> None:  # noqa: N802
        # Hidden mid-drag, the release never arrives: drop the drag without seeking.
        if self.isSliderDown():
            self.blockSignals(True)
            self.setSliderDown(False)
            self.blockSignals(False)
        self._key_seek.stop()
        super().hideEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self
            )
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(
                    QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self
                )
                span = max(groove.width() - handle.width(), 1)
                x = int(event.position().x()) - groove.x() - handle.width() // 2
                self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span))
        super().mousePressEvent(event)


class ActionStrip(QWidget):
    """Up to `max_rows` rows of buttons; what does not fit moves into a trailing "more" menu."""

    def __init__(self, theme: Theme, center: bool, spacing: int, max_rows: int = 1, parent: QWidget | None = None):
        super().__init__(parent)
        self._center = center
        self._max_rows = max_rows
        self._spacing = spacing
        self._buttons: list[QToolButton] = []
        self._hidden: list[QToolButton] = []
        self._menu_only: list[tuple[str, str, QIcon | None]] = []
        self._on_menu_only: Callable[[str], None] = lambda aid: None
        self.more = QToolButton(self)
        self.more.setObjectName("overflow")
        self.more.setProperty("role", "media")
        self.more.setToolTip("More actions")
        self.more.setAccessibleName("More actions")
        self.more.setIcon(icons.icon("more", theme.text))
        self.more.setFixedSize(32, 32)
        self.more.setIconSize(QSize(20, 20))
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        self.menu = QMenu(self.more)
        self.menu.aboutToShow.connect(self.populate_menu)
        self.more.setMenu(self.menu)
        self.more.hide()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_buttons(self, buttons: list[QToolButton]) -> None:
        self._buttons = buttons
        for b in buttons:
            b.setParent(self)
        self.relayout()

    def set_menu_only(self, entries: list[tuple[str, str, QIcon | None]], on_trigger: Callable[[str], None]) -> None:
        self._menu_only = entries
        self._on_menu_only = on_trigger
        self.relayout()

    def buttons(self) -> list[QToolButton]:
        return list(self._buttons)

    @staticmethod
    def _hint(b: QWidget) -> QSize:
        return b.sizeHint().expandedTo(b.minimumSize()).boundedTo(b.maximumSize())

    def _fill(self, items: list[QWidget], width: int) -> list[list[QWidget]]:
        """Greedy rows of `items` within `width` and `max_rows`; stops at the first misfit."""
        rows: list[list[QWidget]] = [[]]
        used = 0
        for item in items:
            w = self._hint(item).width()
            if rows[-1] and used + self._spacing + w > width:
                if len(rows) == self._max_rows:
                    break
                rows.append([])
                used = 0
            if not rows[-1] and w > width:
                break
            used += (self._spacing if rows[-1] else 0) + w
            rows[-1].append(item)
        return rows

    def _plan(self, width: int) -> tuple[list[list[QWidget]], list[QToolButton]]:
        rows = self._fill(self._buttons, width)
        placed = sum(len(r) for r in rows)
        if placed == len(self._buttons) and not self._menu_only:
            return rows, []
        for keep in range(placed, -1, -1):
            rows = self._fill(self._buttons[:keep] + [self.more], width)
            if sum(len(r) for r in rows) != keep + 1:
                continue
            if len(rows) > 1 and rows[-1] == [self.more]:
                continue  # "more" alone on a row of its own: move a button into the menu instead
            return rows, self._buttons[keep:]
        return [[self.more]], list(self._buttons)

    def _height(self, rows: list[list[QWidget]]) -> int:
        heights = [max(self._hint(i).height() for i in r) for r in rows if r]
        return sum(heights) + self._spacing * max(len(heights) - 1, 0)

    def sizeHint(self) -> QSize:  # noqa: N802
        items = self._buttons + ([self.more] if self._menu_only else [])
        widths = [self._hint(i).width() for i in items]
        return QSize(sum(widths) + self._spacing * max(len(widths) - 1, 0), self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.more.width(), self.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.relayout()

    def relayout(self) -> None:
        width, sp = max(self.width(), 1), self._spacing
        rows, self._hidden = self._plan(width)
        y = 0
        for row in rows:
            if not row:
                continue
            row_h = max(self._hint(i).height() for i in row)
            row_w = sum(self._hint(i).width() for i in row) + sp * (len(row) - 1)
            x = max((width - row_w) // 2, 0) if self._center else 0
            for item in row:
                hint = self._hint(item)
                item.setGeometry(x, y + (row_h - hint.height()) // 2, hint.width(), hint.height())
                item.show()
                x += hint.width() + sp
            y += row_h + sp
        for b in self._hidden:
            b.hide()
        if not any(self.more in r for r in rows):
            self.more.hide()
        height = self._height(rows) or self.more.height()
        if self.height() != height or self.minimumHeight() != height:
            self.setFixedHeight(height)

    def populate_menu(self) -> list[QAction]:
        """(Re)build the overflow menu: buttons that did not fit, then menu-only actions."""
        self.menu.clear()
        entries = []
        for b in self._hidden:
            act = self.menu.addAction(b.icon(), b.toolTip() or b.text())
            act.setData(b.property("actionId"))
            act.setEnabled(b.isEnabled())
            act.triggered.connect(b.click)
            entries.append(act)
        for aid, label, ic in self._menu_only:
            act = self.menu.addAction(ic or QIcon(), label)
            act.setData(aid)
            act.triggered.connect(lambda _=False, a=aid: self._on_menu_only(a))
            entries.append(act)
        return entries


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
        self._slot_buttons: dict[str, QToolButton] = {}
        self._inputs: dict[str, QLineEdit] = {}
        self._input_sends: dict[str, QToolButton] = {}
        self.action_strip: ActionStrip | None = None
        self._open_available = False
        self._open_listed = False
        self._optimistic_media: dict | None = None
        self._pending: set[str] = set()
        self._spin_angle = 0
        self._spin = QTimer(self, interval=SPIN_FRAME_MS)
        self._spin.timeout.connect(self._spin_step)
        # While stale, extrapolation stops at the moment the card went stale.
        self._frozen_at: float | None = None
        self._seek_unit = 1

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

        # What the phone last reported; dimmed while stale. Actions dim by being disabled.
        self.info = QWidget()
        info = QVBoxLayout(self.info)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(8)
        self._dim = QGraphicsOpacityEffect(self.info)
        self._dim.setOpacity(STALE_OPACITY)
        self._dim.setEnabled(False)
        self.info.setGraphicsEffect(self._dim)
        root.addWidget(self.info)

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
        self.text = ClampedLabel(max_lines=3)
        self.text.setProperty("role", "muted")
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        texts.addWidget(self.text)
        texts.addStretch(1)
        body.addLayout(texts, 1)
        self.chrono = QLabel()
        self.chrono.setProperty("role", "chrono")
        self.chrono.setFont(self._tabular(self.chrono.font()))
        body.addWidget(self.chrono, 0, Qt.AlignmentFlag.AlignVCenter)
        info.addLayout(body)

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
        info.addWidget(self.progress_row)

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
        info.addWidget(self.seek_box)

        self.actions_box = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_box)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(6)
        root.addWidget(self.actions_box)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        self.error.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.error.hide()
        self._error_timer = QTimer(self, singleShot=True)
        self._error_timer.timeout.connect(self.error.hide)
        root.addWidget(self.error)

        client.artLoaded.connect(self._on_art_loaded)
        for w in (self.app_icon, self.app_name, self.art, self.title):
            w.installEventFilter(self)
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

    @property
    def stale(self) -> bool:
        return self.activity.get("stale") is True

    def device_name(self) -> str:
        dev = next((d for d in self.client.devices if d.get("id") == self.device_id), None)
        return (dev or {}).get("name") or "your phone"

    def accent(self) -> str:
        color = QColor(self.activity.get("color") or "")
        return color.name() if color.isValid() else self.theme.accent

    def update_activity(self, activity: dict) -> None:
        self.activity = sanitize_activity(activity)
        self._optimistic_media = None
        a = self.activity
        stale = self.stale
        if not stale:
            self._frozen_at = None
        elif self._frozen_at is None:
            self._frozen_at = now_ms()
        self._dim.setEnabled(stale)
        app = a.get("app") or {}
        app_name = app.get("name") or app.get("id") or ""
        accent = self.accent()

        self.app_name.setFullText(app_name)
        self.app_icon.set_placeholder(accent, app_name, "#ffffff")
        self.title.setFullText(a.get("title") or "")
        self.subtitle.setFullText(a.get("subtitle") or "")
        self.subtitle.setVisible(bool(a.get("subtitle")))
        self.text.setFullText(a.get("text") or "")
        self.text.setVisible(bool(a.get("text")))

        self.art.setVisible(bool(a.get("artKey")))
        self.art.set_placeholder(self.theme.mix(accent, 0.25), "", "#ffffff")
        self._load_art()

        self.chrono.setVisible(bool(a.get("chrono")))

        prog = a.get("progress")
        self.progress_row.setVisible(bool(prog))
        if prog:
            maximum = prog.get("max") or 0
            if prog.get("indeterminate") or maximum <= 0:
                self.progress.setRange(0, 0)
                self.progress_text.hide()
            else:
                value = max(0.0, min(prog.get("value") or 0, maximum))
                unit = _unit(maximum)
                self.progress.setRange(0, int(maximum / unit))
                self.progress.setValue(int(value / unit))
                self.progress_text.setText(f"{round(100 * value / maximum)}%")
                self.progress_text.show()
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}")

        media = a.get("media") or {}
        has_seek = (media.get("durationMs") or 0) > 0
        self.seek_box.setVisible(has_seek)
        if has_seek:
            if not self.seek.isSliderDown():
                # Slider steps are ms, or coarser when the duration overflows Qt's int range.
                self._seek_unit = _unit(media["durationMs"])
                self.seek.setRange(0, int(media["durationMs"] / self._seek_unit))
                self.seek.setSingleStep(max(1, 5_000 // self._seek_unit))
                self.seek.setPageStep(max(1, 30_000 // self._seek_unit))
            self.duration.setText(format_clock(media["durationMs"]))
            self.seek.setStyleSheet(f"QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}")

        self._rebuild_actions()
        self._sync_enabled()
        self._spin_frame()
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
        if self._frozen_at is not None:
            now = min(now, self._frozen_at)
        chrono = self.activity.get("chrono")
        if chrono:
            self.chrono.setText(format_clock(chrono_value_ms(chrono, now)))
        media = self._media()
        if media.get("durationMs"):
            if not self.seek.is_held():
                pos = media_position_ms(media, now)
                self.seek.blockSignals(True)
                self.seek.setValue(int(pos / self._seek_unit))
                self.seek.blockSignals(False)
                self.elapsed.setText(format_clock(pos))
            else:
                self.elapsed.setText(format_clock(self.seek.value() * self._seek_unit))

    # -- actions ---------------------------------------------------------------------------

    def _rebuild_actions(self) -> None:
        actions = [x for x in self.activity.get("actions") or [] if isinstance(x, dict) and x.get("id")]
        is_media = self.activity.get("kind") == "media"
        places = acts.placements(actions, is_media)
        # Widgets are keyed by slot, not label, so a toggle relabelling itself keeps its button.
        slots = (is_media, [(acts.slot_key(x, is_media), p) for x, p in zip(actions, places, strict=True)])
        if slots != self._actions_sig:
            self._actions_sig = slots
            self._build_action_widgets(actions, places, is_media)
        self._apply_actions(actions, places, is_media)
        self._open_listed = "open" in places
        self.actions_box.setVisible(any(p != "open" for p in places))

    def _build_action_widgets(self, actions: list[dict], places: list[str], is_media: bool) -> None:
        drafts = {aid: le.text() for aid, le in self._inputs.items()}
        _clear_layout(self.actions_layout)
        self._slot_buttons = {}
        self._inputs.clear()
        self._input_sends.clear()
        self.action_strip = None
        inline = [x for x, p in zip(actions, places, strict=True) if p == "inline"]
        if inline or "menu" in places:
            self.action_strip = ActionStrip(self.theme, center=is_media, spacing=6, max_rows=2)
            self.action_strip.setObjectName("actionStrip")
            kinds = [
                ("transport" if x["id"] in acts.TRANSPORT_IDS else "secondary") if is_media else "pill" for x in inline
            ]
            self.action_strip.set_buttons([self._new_button(x, k) for x, k in zip(inline, kinds, strict=True)])
            self.actions_layout.addWidget(self.action_strip)
        for x, p in zip(actions, places, strict=True):
            if p == "input":
                self.actions_layout.addWidget(self._make_input(x, drafts.get(x["id"], "")))

    def _new_button(self, action: dict, kind: str) -> QToolButton:
        btn = QToolButton()
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setProperty("kind", kind)
        btn.clicked.connect(lambda _=False, b=btn: self.trigger(b.property("actionId")))
        if kind == "pill":
            btn.setProperty("role", "pill")
            btn.setIconSize(QSize(16, 16))
        else:
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setProperty("role", "media")
            side, icon_side = (36, 24) if kind == "transport" else (32, 20)
            btn.setFixedSize(side, side)
            btn.setIconSize(QSize(icon_side, icon_side))
        self._slot_buttons[acts.slot_key(action, kind != "pill")] = btn
        return btn

    def _apply_actions(self, actions: list[dict], places: list[str], is_media: bool) -> None:
        """Refresh ids, labels, glyphs and toggle states in place."""
        self._buttons = {}
        menu_only = []
        for x, place in zip(actions, places, strict=True):
            aid, label = x["id"], x.get("label") or x["id"]
            if place == "input":
                self._inputs[aid].setPlaceholderText((x.get("input") or {}).get("label") or label)
                self._buttons[aid] = self._input_sends[aid]
            elif place == "menu":
                menu_only.append((aid, label, self._glyph_icon(x, None)))
            elif place == "inline":
                btn = self._slot_buttons[acts.slot_key(x, is_media)]
                btn.setObjectName(f"action:{aid}")
                btn.setProperty("actionId", aid)
                btn.setProperty("label", label)
                btn.setAccessibleName(label)
                self._style_button(btn, x)
                self._buttons[aid] = btn
        if self.action_strip is not None:
            self.action_strip.set_menu_only(menu_only, lambda aid: self.trigger(aid))

    def _sync_enabled(self) -> None:
        """A stale card disables its controls until fresh data arrives. A pending action's button
        stays enabled-looking but busy (see trigger), so the press reads as accepted."""
        stale = self.stale
        hint = f"Reconnecting to {self.device_name()}" if stale else None
        for aid, btn in self._buttons.items():
            pending = aid in self._pending
            if aid in self._inputs:
                field = self._inputs[aid]
                field.setEnabled(not pending)
                btn.setEnabled(not stale and (pending or bool(field.text().strip())))
            else:
                btn.setEnabled(not stale)
            if bool(btn.property("pending")) != pending:
                btn.setProperty("pending", pending)
                btn.setCursor(Qt.CursorShape.BusyCursor if pending else Qt.CursorShape.PointingHandCursor)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            btn.setToolTip(hint or btn.property("label") or "")
        if self.action_strip is not None:
            self.action_strip.more.setEnabled(not stale)
            self.action_strip.more.setToolTip(hint or "More actions")
        self.seek.setEnabled(bool(self._media().get("seekable")) and not stale)
        self.seek.setToolTip(hint or "")
        self._set_open_target(self._open_listed and not stale)
        if hint:
            for w in (self.app_icon, self.app_name, self.art, self.title):
                w.setToolTip(hint)

    def _set_open_target(self, available: bool) -> None:
        """`open` is the card's primary tap target: header, art and title."""
        self._open_available = available
        cursor = Qt.CursorShape.PointingHandCursor if available else Qt.CursorShape.ArrowCursor
        for w in (self.app_icon, self.app_name, self.art, self.title):
            w.setCursor(cursor)
        hint = "Open on phone" if available else ""
        self.app_icon.setToolTip(hint)
        self.app_name.setToolTip(hint)
        self.art.setToolTip(hint)
        title = self.title.fullText()
        self.title.setToolTip("\n".join(t for t in (title if len(title) > 30 else "", hint) if t))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self._open_available
            and obj.rect().contains(event.position().toPoint())
        ):
            self.trigger("open")
            return True
        return super().eventFilter(obj, event)

    def _glyph_icon(self, action: dict, color: str | None):
        glyph = acts.icon_for(action)
        if glyph is None:
            return None
        state = acts.toggle_state(action, glyph)
        if glyph == "like" and state != "active":
            glyph = "like_outline"
        elif glyph == "repeat" and state == "one":
            glyph = "repeat_one"
        if color is None:
            color = {"active": self.accent(), "one": self.accent(), "inactive": self.theme.muted}.get(
                state or "", self.theme.text
            )
        return icons.icon(glyph, color)

    def _style_button(self, btn: QToolButton, action: dict) -> None:
        kind = btn.property("kind")
        primary = kind == "transport" and action["id"] in ("media.play", "media.pause")
        state = acts.toggle_state(action) or ""
        if btn.property("toggleState") != state:
            btn.setProperty("toggleState", state)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        btn.setAccessibleDescription({"active": "On", "one": "On, repeat one", "inactive": "Off"}.get(state, ""))
        ic = self._glyph_icon(action, "#ffffff" if primary else None)
        btn.setIcon(ic if ic is not None else QIcon())
        if kind == "pill":
            label = action.get("label") or action["id"]
            btn.setText(QFontMetrics(btn.font()).elidedText(label, Qt.TextElideMode.ElideRight, PILL_MAX_TEXT))
            btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon if ic is not None else Qt.ToolButtonStyle.ToolButtonTextOnly
            )
        elif primary:
            btn.setFixedSize(44, 44)
            accent = self.accent()
            btn.setStyleSheet(
                f"QToolButton {{ background: {accent}; border-radius: 22px; }}"
                f"QToolButton:hover {{ background: {QColor(accent).lighter(115).name()}; }}"
                f"QToolButton:disabled {{ background: {self.theme.mix(accent, 0.45)}; }}"
            )

    def _make_input(self, action: dict, draft: str) -> QWidget:
        aid = action["id"]
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        field = QLineEdit(draft)
        field.setObjectName(f"input:{aid}")
        field.setAccessibleName((action.get("input") or {}).get("label") or action.get("label") or "Reply")
        field.setClearButtonEnabled(True)
        send = QToolButton()
        send.setObjectName(f"action:{aid}")
        send.setProperty("label", action.get("label") or "Send")
        send.setAccessibleName(action.get("label") or "Send")
        send.setIcon(icons.icon("send", self.accent()))
        send.setIconSize(QSize(20, 20))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        field.textChanged.connect(lambda _t: self._sync_enabled())
        submit = lambda: self.trigger(aid, field.text()) if field.text().strip() else None  # noqa: E731
        field.returnPressed.connect(submit)
        send.clicked.connect(submit)
        row.addWidget(field, 1)
        row.addWidget(send)
        self._input_sends[aid] = send
        self._inputs[aid] = field
        return box

    def trigger(self, action_id: str, value: Any = None) -> None:
        if self.stale or action_id in self._pending:
            return
        self._pending.add(action_id)
        self._sync_enabled()
        self._spin_frame()
        self._spin.start()
        device, activity = self.device_id, self.activity_id

        def done(result: Any, error: dict | None) -> None:
            if not _alive(self):
                return
            ok = error is None and isinstance(result, dict) and result.get("ok") is True
            self._pending.discard(action_id)
            if not self._pending:
                self._spin.stop()
            field = self._inputs.get(action_id)
            if ok:
                self.error.hide()
                if field is not None:
                    field.clear()
            self._sync_enabled()
            self._restore_icon(action_id)
            if action_id == "media.seek" and not ok:
                self._optimistic_media = None
                self.tick(now_ms())
            if not ok:
                text, gentle = action_failure(error, result, self.device_name(), self._action_label(action_id))
                self.show_error(text, gentle=gentle)

        self.client.action(device, activity, action_id, value, done)

    def _spin_step(self) -> None:
        self._spin_angle = (self._spin_angle + 30) % 360
        self._spin_frame()

    def _spin_frame(self) -> None:
        """Pending buttons that carry a glyph show a spinner in its place (same size, no shift)."""
        for aid in self._pending:
            btn = self._buttons.get(aid)
            if btn is None or btn.icon().isNull():
                continue
            primary = btn.property("kind") == "transport" and aid in ("media.play", "media.pause")
            btn.setIcon(icons.spinner("#ffffff" if primary else self.accent(), self._spin_angle))

    def _restore_icon(self, action_id: str) -> None:
        btn = self._buttons.get(action_id)
        if btn is None:
            return
        if action_id in self._inputs:
            btn.setIcon(icons.icon("send", self.accent()))
            return
        action = next((x for x in self.activity.get("actions") or [] if x.get("id") == action_id), None)
        if action is not None:
            self._style_button(btn, action)

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
        position = value * self._seek_unit
        self._optimistic_media = {**media, "positionMs": position, "positionAtMs": now_ms()}
        self.trigger("media.seek", position)
        self.tick(now_ms())

    def show_error(self, message: str, gentle: bool = False) -> None:
        self.error.setProperty("role", "notice" if gentle else "error")
        self.error.style().unpolish(self.error)
        self.error.style().polish(self.error)
        self.error.setText(message)
        self.error.show()
        self._error_timer.start(ERROR_VISIBLE_MS)


def _unit(span: float) -> int:
    """Step size that fits `span` into Qt's int range."""
    return max(1, math.ceil(span / INT32_MAX))


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())


def _alive(obj) -> bool:
    return isValid(obj)
