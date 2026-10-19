"""Odd payloads, interrupted drags, keyboard/wheel on the seek bar, accessibility and slow connects."""

import pytest
from mock_orbitd import PIXEL_8
from PySide6.QtCore import QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QToolButton

from orbit.applet import actions as acts
from orbit.applet import client as client_mod
from orbit.applet import icons, model
from orbit.applet.cards import ActivityCard, SeekSlider
from orbit.applet.panel import Panel
from orbit.applet.theme import DARK, LIGHT, stylesheet

NOW = model.now_ms()
BASE = {"device": PIXEL_8, "id": "x", "kind": "media", "app": {"id": "a", "name": "A"}, "title": "T", "actions": []}
# The final review's fuzz cases: each used to raise out of ActivityCard and blank the panel.
FUZZ = {
    "media duration 2^31": {"media": {"state": "paused", "positionMs": 0, "durationMs": 2**31}},
    "media duration str": {"media": {"state": "paused", "positionMs": 1000, "durationMs": "10000"}},
    "media is list": {"media": [1]},
    "media pos huge": {"media": {"state": "paused", "positionMs": 5e12, "durationMs": 10000}},
    "progress max huge": {"progress": {"value": 1, "max": 2**31}},
    "progress value str": {"progress": {"value": "50", "max": 100}},
    "app string": {"app": "Spotify"},
    "title int": {"title": 42},
    "actions dict": {"actions": {"id": "x"}},
    "action label int": {"actions": [{"id": "notif.0", "label": 5}]},
    "action id int": {"actions": [{"id": 7, "label": "Seven"}]},
    "action input str": {"actions": [{"id": "notif.0", "label": "Reply", "input": "text"}]},
    "action input label int": {"actions": [{"id": "notif.0", "label": "Reply", "input": {"kind": "text", "label": 3}}]},
    "chrono base str": {"chrono": {"baseMs": "abc", "running": True}},
    "chrono valueMs huge": {"chrono": {"running": False, "valueMs": 1e300}},
    "chrono baseMs inf-ish": {"chrono": {"running": True, "baseMs": 1e308}},
    "color int": {"color": 123},
    "artKey int": {"artKey": 12345},
    "nan position": {
        "media": {"state": "playing", "positionMs": float("nan"), "positionAtMs": NOW, "durationMs": 10000}
    },
}


def make_panel(qtbot, client):
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


@pytest.mark.parametrize("name", FUZZ)
def test_odd_payloads_render(qtbot, client, name):
    card = ActivityCard(client, LIGHT, {**BASE, **FUZZ[name]})
    qtbot.addWidget(card)
    card.resize(362, 300)
    card.show()
    card.tick(model.now_ms())
    assert card.title.fullText() == ("42" if name == "title int" else "T")
    assert len(card.chrono.text()) <= 9  # never a 300-digit clock


def test_sanitize_activity_types():
    a = model.sanitize_activity(
        {
            **BASE,
            "app": "Spotify",
            "title": 42,
            "stale": "yes",
            "media": {"durationMs": "1", "positionMs": float("inf"), "seekable": 1, "state": "playing", "rate": True},
            "progress": [1],
            "chrono": {"running": True},
            "actions": [
                {"id": "a", "label": 5, "active": "true", "overflow": True, "input": {"kind": "text", "label": 3}},
                {"id": 7},
                "junk",
                {"id": ""},
            ],
            "updatedMs": 5,
        }
    )
    assert a["app"] == {} and a["title"] == "42" and a["stale"] is False
    assert a["media"] == {"state": "playing"}
    assert "progress" not in a and "chrono" not in a
    assert a["actions"] == [{"id": "a", "overflow": True, "input": {"kind": "text"}}]
    assert a["updatedMs"] == 5


def test_huge_duration_seeks_in_real_milliseconds(qtbot, client, mock):
    long = {
        **BASE,
        "id": "long",
        "media": {"state": "paused", "positionMs": 2_000_000_000.0, "durationMs": 3_000_000_000.0, "seekable": True},
        "actions": [{"id": "media.play", "label": "Play"}],
    }
    mock.set_activities([long])
    panel = make_panel(qtbot, client)
    qtbot.waitUntil(lambda: (PIXEL_8, "long") in panel.activities_page.cards)
    card = panel.activities_page.cards[(PIXEL_8, "long")]
    assert card.seek.maximum() <= model.INT32_MAX
    assert card.duration.text() == model.format_clock(3_000_000_000)
    assert card.elapsed.text() == model.format_clock(2_000_000_000)
    assert abs(card.seek.value() * card._seek_unit - 2_000_000_000) <= card._seek_unit
    mock.clear_recorded()
    card.seek.setValue(card.seek.maximum())
    card.seek.seekRequested.emit(card.seek.value())
    qtbot.waitUntil(lambda: bool(mock.actions()))
    assert abs(mock.actions()[0]["value"] - 3_000_000_000) <= card._seek_unit


def test_one_unrenderable_activity_does_not_blank_the_panel(qtbot, client, mock, monkeypatch):
    panel = make_panel(qtbot, client)
    page = panel.activities_page
    acts_before = mock.activities
    n = len(acts_before)
    victim = acts_before[0]
    real = ActivityCard.update_activity

    def flaky(self, activity):
        if activity.get("id") == victim["id"] and activity.get("title") == "boom":
            raise RuntimeError("cannot render")
        real(self, activity)

    monkeypatch.setattr(ActivityCard, "update_activity", flaky)
    # An upsert turns an existing card bad, and in the same snapshot another activity goes away.
    gone = acts_before[-1]
    changed = [dict(a, title="boom") if a["id"] == victim["id"] else a for a in acts_before if a is not gone]
    changed = [dict(a, subtitle="fresh") if a["id"] not in (victim["id"],) else a for a in changed]
    mock.set_activities(changed)
    qtbot.waitUntil(lambda: (victim["device"], victim["id"]) not in page.cards, timeout=3000)
    assert page.stack.currentWidget() is page.scroll
    assert len(page.cards) == n - 2
    assert (gone["device"], gone["id"]) not in page.cards
    assert all(c.subtitle.fullText() == "fresh" for c in page.cards.values())

    # A brand-new bad activity is skipped the same way.
    mock.set_activities(changed + [dict(BASE, id=victim["id"], device=victim["device"], title="boom")])
    qtbot.wait(200)
    assert page.stack.currentWidget() is page.scroll and len(page.cards) == n - 2


def test_hidden_mid_drag_releases_the_slider_without_seeking(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = next(a for a in mock.activities if a["kind"] == "media")
    card = panel.activities_page.cards[(media["device"], media["id"])]
    mock.clear_recorded()
    qtbot.mousePress(card.seek, Qt.MouseButton.LeftButton, pos=QPoint(card.seek.width() // 2, card.seek.height() // 2))
    assert card.seek.isSliderDown()
    panel.hide()
    assert not card.seek.isSliderDown()
    panel.show()
    m = dict(media["media"], durationMs=200_000, positionMs=50_000, positionAtMs=model.now_ms(), state="paused")
    mock.update_activity(media["device"], media["id"], media=m)
    qtbot.waitUntil(lambda: card.seek.maximum() == 200_000, timeout=3000)
    card.tick(model.now_ms())
    assert card.elapsed.text() == "0:50"
    qtbot.wait(100)
    assert mock.actions() == []


def test_wheel_passes_to_the_panel_and_keys_seek(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = next(a for a in mock.activities if a["kind"] == "media")
    card = panel.activities_page.cards[(media["device"], media["id"])]
    mock.update_activity(
        media["device"],
        media["id"],
        media=dict(media["media"], state="paused", positionMs=60_000, positionAtMs=model.now_ms()),
    )
    qtbot.waitUntil(lambda: card.elapsed.text() == "1:00", timeout=3000)
    mock.clear_recorded()
    center = QPointF(card.seek.width() / 2, card.seek.height() / 2)
    wheel = QWheelEvent(
        center,
        card.seek.mapToGlobal(center),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(card.seek, wheel)
    assert not wheel.isAccepted()
    assert card.seek.value() == 60_000

    card.seek.setFocus()
    qtbot.keyClick(card.seek, Qt.Key.Key_Right)
    qtbot.keyClick(card.seek, Qt.Key.Key_Right)
    card.tick(model.now_ms())
    assert card.elapsed.text() == "1:10"  # held while keys settle, not snapped back
    qtbot.waitUntil(lambda: bool(mock.actions()), timeout=3000)
    assert [(x["action"], x["value"]) for x in mock.actions()] == [("media.seek", 70_000)]


def test_controls_have_accessible_names(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    names = {panel.devices_button.accessibleName(), panel.back.accessibleName()}
    assert names == {"Devices", "Back"}
    for card in panel.activities_page.cards.values():
        for b in card.findChildren(QToolButton):
            if b.objectName().startswith("action:") or b.objectName() == "overflow":
                assert b.accessibleName(), b.objectName()
        for f in card.findChildren(QLineEdit):
            assert f.accessibleName() == "Reply"
        assert card.findChild(SeekSlider).accessibleName() == "Playback position"


@pytest.mark.parametrize(
    "action, state",
    [
        ({"id": "notif.0", "label": "Mute", "icon": "mute", "active": True}, "active"),
        ({"id": "notif.0", "label": "Speaker", "active": True}, "active"),
        ({"id": "media.custom.MUTE", "label": "Mute", "active": False}, "inactive"),
        ({"id": "notif.0", "label": "Speaker"}, None),
        ({"id": "notif.0", "label": "Turn speaker off"}, None),  # labels only count for known toggles
    ],
)
def test_active_is_honoured_for_any_action(action, state):
    assert acts.toggle_state(action) == state


def test_active_pill_and_mute_glyph_show_their_state(qtbot, client, mock):
    call = {
        **BASE,
        "id": "call",
        "kind": "generic",
        "title": "Call",
        "actions": [
            {"id": "notif.0", "label": "Speaker", "active": True},
            {"id": "notif.1", "label": "Mute", "icon": "mute", "active": True},
            {"id": "notif.2", "label": "Hold", "active": False},
        ],
    }
    mock.set_activities([call])
    panel = make_panel(qtbot, client)
    qtbot.waitUntil(lambda: (PIXEL_8, "call") in panel.activities_page.cards)
    card = panel.activities_page.cards[(PIXEL_8, "call")]
    speaker, mute, hold = (card.findChild(QToolButton, f"action:notif.{i}") for i in range(3))
    assert speaker.property("toggleState") == "active" and speaker.accessibleDescription() == "On"
    assert hold.property("toggleState") == "inactive" and hold.accessibleDescription() == "Off"
    assert speaker.palette().color(speaker.foregroundRole()).name() == LIGHT.accent
    assert hold.palette().color(hold.foregroundRole()).name() != LIGHT.accent
    img = mute.icon().pixmap(QSize(16, 16)).toImage()
    assert img == icons.icon("mute", card.accent()).pixmap(QSize(16, 16)).toImage()

    mock.update_activity(PIXEL_8, "call", actions=[dict(call["actions"][0], active=False)] + call["actions"][1:])
    qtbot.waitUntil(lambda: speaker.property("toggleState") == "inactive", timeout=3000)
    assert speaker.palette().color(speaker.foregroundRole()).name() != LIGHT.accent


def test_slow_connect_is_not_cut_short(qtbot, client, mock):
    scale = 50
    client.timeouts_ms = {k: v // scale for k, v in client_mod.REQUEST_TIMEOUTS_MS.items()}
    client.probe_timeout_ms = client_mod.PROBE_TIMEOUT_MS // scale
    # A BLE connect through several stale addresses: well past the action bound.
    mock.method_delay_s["devices.connect"] = 30 / scale
    results = []
    client.request("devices.connect", {"id": PIXEL_8}, lambda r, e: results.append((r, e)))
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    assert results == [({"id": PIXEL_8}, None)]


def _ink_columns(img, bg, inset):
    """Columns holding anything clearly unlike the background, ignoring an `inset` frame (the border)."""
    cols = []
    for x in range(inset, img.width() - inset):
        for y in range(inset, img.height() - inset):
            c = QColor(img.pixel(x, y))
            if abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) > 60:
                cols.append(x)
                break
    return cols


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
def test_icon_pills_keep_a_gap_between_icon_and_text(qtbot, client, theme):
    names = sorted(acts.WELL_KNOWN)
    card = ActivityCard(
        client,
        theme,
        {
            **BASE,
            "kind": "generic",
            "actions": [{"id": f"notif.{i}", "label": "Hang up", "icon": n} for i, n in enumerate(names)],
        },
    )
    qtbot.addWidget(card)
    card.setStyleSheet(stylesheet(theme))
    card.resize(2400, 400)
    card.show()
    gaps = {}
    for i, name in enumerate(names):
        btn = card.findChild(QToolButton, f"action:notif.{i}")
        assert btn is not None and not btn.icon().isNull(), name
        with_icon = btn.grab().toImage()
        glyph = btn.icon()
        blank = QPixmap(btn.iconSize())
        blank.fill(Qt.GlobalColor.transparent)
        btn.setIcon(QIcon(blank))  # same layout, no glyph: what remains is the label
        text_only = btn.grab().toImage()
        btn.setIcon(glyph)
        icon_cols = [
            x
            for x in range(with_icon.width())
            if any(with_icon.pixel(x, y) != text_only.pixel(x, y) for y in range(with_icon.height()))
        ]
        text_cols = _ink_columns(text_only, QColor(text_only.pixel(3, text_only.height() // 2)), 3)
        gaps[name] = min(text_cols) - max(icon_cols) - 1
    assert min(gaps.values()) >= 4, gaps
