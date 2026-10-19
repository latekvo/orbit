"""Light/dark color tokens, palette and style sheet for the applet's windows."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette


@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str
    card: str
    border: str
    text: str
    muted: str
    accent: str
    danger: str
    track: str
    hover: str
    field: str

    def mix(self, color: str, alpha: float) -> str:
        """`color` at `alpha` over the card background, as a solid hex color."""
        a, b = QColor(color), QColor(self.card)
        return QColor(
            round(a.red() * alpha + b.red() * (1 - alpha)),
            round(a.green() * alpha + b.green() * (1 - alpha)),
            round(a.blue() * alpha + b.blue() * (1 - alpha)),
        ).name()


LIGHT = Theme(dark=False, window="#f2f2f5", card="#ffffff", border="#e2e2e7", text="#1d1d1f",
              muted="#6e6e73", accent="#0a7cff", danger="#d70015", track="#e3e3e8",
              hover="#ececf1", field="#f5f5f8")
DARK = Theme(dark=True, window="#1b1b1d", card="#2a2a2d", border="#3a3a3e", text="#f2f2f7",
             muted="#9a9aa1", accent="#3b9bff", danger="#ff5c52", track="#414146",
             hover="#38383c", field="#1f1f22")


def resolve(mode: str = "auto") -> Theme:
    if mode == "light":
        return LIGHT
    if mode == "dark":
        return DARK
    hints = QGuiApplication.styleHints()
    if hints.colorScheme() == Qt.ColorScheme.Dark:
        return DARK
    if hints.colorScheme() == Qt.ColorScheme.Light:
        return LIGHT
    return DARK if QGuiApplication.palette().window().color().lightness() < 128 else LIGHT


def palette(t: Theme) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.window, QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.field, QPalette.ColorRole.AlternateBase: t.card,
        QPalette.ColorRole.Text: t.text, QPalette.ColorRole.Button: t.card,
        QPalette.ColorRole.ButtonText: t.text, QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: t.card,
        QPalette.ColorRole.ToolTipText: t.text, QPalette.ColorRole.PlaceholderText: t.muted,
        QPalette.ColorRole.Mid: t.border, QPalette.ColorRole.Light: t.hover,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.muted))
    return p


def stylesheet(t: Theme) -> str:
    return f"""
#panel, #pairingDialog {{ background: {t.window}; }}
#panelFrame {{ background: {t.window}; border: 1px solid {t.border}; border-radius: 14px; }}
QLabel {{ color: {t.text}; background: transparent; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="title"] {{ font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 15px; font-weight: 700; }}
QLabel[role="section"] {{ color: {t.muted}; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.6px; padding: 6px 4px 0 4px; }}
QLabel[role="error"] {{ color: {t.danger}; font-size: 12px; }}
QLabel[role="notice"] {{ color: {t.muted}; font-size: 12px; }}
QLabel[role="chrono"] {{ font-size: 22px; font-weight: 600; }}
QLabel[role="time"] {{ color: {t.muted}; font-size: 11px; }}
QLabel[role="sas"] {{ font-size: 44px; font-weight: 700; letter-spacing: 4px; }}
#card, #deviceRow {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 12px; }}
QScrollArea, #scrollBody {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.track}; border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolButton {{ color: {t.text}; background: transparent; border: none; border-radius: 8px;
    padding: 4px 8px; }}
QToolButton:hover {{ background: {t.hover}; }}
QToolButton:pressed {{ background: {t.border}; }}
QToolButton:disabled {{ color: {t.muted}; }}
QToolButton[pending="true"] {{ color: {t.muted}; }}
QToolButton[role="pill"] {{ background: {t.field}; border: 1px solid {t.border}; padding: 3px 8px;
    font-size: 12px; }}
QToolButton[role="pill"]:hover {{ background: {t.hover}; }}
QToolButton[role="pill"][toggleState="active"], QToolButton[role="pill"][toggleState="one"] {{
    color: {t.accent}; border-color: {t.accent}; background: {t.mix(t.accent, 0.12)}; }}
QToolButton[role="media"] {{ border-radius: 18px; padding: 6px; }}
QToolButton[role="primary"] {{ border-radius: 20px; padding: 8px; }}
QPushButton {{ color: {t.text}; background: {t.card}; border: 1px solid {t.border};
    border-radius: 8px; padding: 6px 14px; }}
QPushButton[compact="true"] {{ padding: 4px 10px; font-size: 12px; }}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:disabled {{ color: {t.muted}; }}
QPushButton[role="primary"] {{ background: {t.accent}; color: #ffffff; border: none; font-weight: 600; }}
QPushButton[role="primary"]:disabled {{ background: {t.track}; color: {t.muted}; }}
QPushButton[role="danger"] {{ color: {t.danger}; }}
QLineEdit {{ color: {t.text}; background: {t.field}; border: 1px solid {t.border};
    border-radius: 8px; padding: 5px 8px; selection-background-color: {t.accent}; }}
QLineEdit:focus {{ border: 1px solid {t.accent}; }}
QProgressBar {{ background: {t.track}; border: none; border-radius: 3px; max-height: 6px;
    min-height: 6px; }}
QSlider::groove:horizontal {{ background: {t.track}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t.text}; width: 12px; height: 12px; margin: -4px 0;
    border-radius: 6px; }}
QSlider::handle:horizontal:disabled {{ background: transparent; }}
"""
