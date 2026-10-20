"""Click feedback and failure wording for activities.action: slow phones, error codes, a wedged orbitd."""

import pytest
from mock_orbitd import PIXEL_8
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QToolButton

from orbit.applet import client as client_mod
from orbit.applet import icons
from orbit.applet.panel import Panel
from orbit.applet.theme import LIGHT

# Real timeouts divided by this, so a 13 s phone round trip takes 1.3 s.
SCALE = 10


def make_panel(qtbot, client):
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def scale_timeouts(client):
    client.timeouts_ms = {k: v // SCALE for k, v in client_mod.REQUEST_TIMEOUTS_MS.items()}
    client.quick_timeout_ms = client_mod.QUICK_TIMEOUT_MS // SCALE
    client.probe_timeout_ms = client_mod.PROBE_TIMEOUT_MS // SCALE


def media_card(panel):
    return next(
        c for c in panel.activities_page.cards.values() if c.activity["kind"] == "media" and c.device_id == PIXEL_8
    )


def next_button(card) -> QToolButton:
    return card.findChild(QToolButton, "action:media.next")


def image(icon):
    return icon.pixmap(QSize(24, 24)).toImage()


def test_slow_phone_shows_pending_then_clears_quietly(qtbot, client, mock):
    scale_timeouts(client)
    panel = make_panel(qtbot, client)
    card = media_card(panel)
    btn = next_button(card)
    glyph = image(icons.icon("next", LIGHT.text))
    assert image(btn.icon()) == glyph
    card.show_error("Next: phone is locked")  # left over from an earlier click
    mock.action_delay_s = 13 / SCALE  # just past orbitd's own ~12 s bound
    mock.clear_recorded()

    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
    assert btn.property("pending") is True
    assert btn.isEnabled()  # busy, not greyed out
    assert image(btn.icon()) != glyph  # spinner in the glyph's place
    assert btn.size() == QSize(36, 36)
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)  # impatient second click
    card.trigger("media.next")
    qtbot.wait(100)
    assert len(mock.actions()) == 1

    qtbot.waitUntil(lambda: btn.property("pending") is False, timeout=3000)
    assert image(btn.icon()) == glyph
    assert not card.error.isVisibleTo(panel)
    qtbot.wait(13 / SCALE * 1000)  # a client-side timeout would land around now
    assert not card.error.isVisibleTo(panel)
    assert mock.actions() == [{"device": PIXEL_8, "activity": card.activity_id, "action": "media.next"}]


def test_pending_spinner_tracks_the_pressed_toggle_only(qtbot, client, mock):
    scale_timeouts(client)
    panel = make_panel(qtbot, client)
    card = media_card(panel)
    mock.action_delay_s = 0.3
    btn = next_button(card)
    prev = card.findChild(QToolButton, "action:media.previous")
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
    assert btn.property("pending") is True and not prev.property("pending")
    assert image(prev.icon()) == image(icons.icon("previous", LIGHT.text))
    qtbot.waitUntil(lambda: btn.property("pending") is False, timeout=3000)


@pytest.mark.parametrize(
    "error, text, role",
    [
        (
            {"code": "timeout", "message": "phone did not answer"},
            "Pixel 8 didn't respond - the action may have gone through",
            "notice",
        ),
        ({"code": "not_connected", "message": "reconnecting"}, "Reconnecting to Pixel 8...", "notice"),
        ({"code": "not_connected", "message": "device is not connected"}, "Pixel 8 is offline", "error"),
        (
            {"code": "failed", "message": "connection lost after the action was sent; it may have been applied"},
            "Connection dropped - the action may have gone through",
            "notice",
        ),
    ],
)
def test_daemon_error_codes_read_as_plain_sentences(qtbot, client, mock, error, text, role):
    panel = make_panel(qtbot, client)
    card = media_card(panel)
    btn = next_button(card)
    mock.fail_next_action = error
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.error.isVisibleTo(panel), timeout=3000)
    assert (card.error.text(), card.error.property("role")) == (text, role)
    assert btn.property("pending") is False and btn.isEnabled()


def test_action_outliving_the_client_bound_with_orbitd_alive_blames_the_phone(qtbot, client, mock):
    scale_timeouts(client)
    panel = make_panel(qtbot, client)
    card = media_card(panel)
    mock.action_delay_s = 30 / SCALE
    qtbot.mouseClick(next_button(card), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.error.isVisibleTo(panel), timeout=3000)
    assert card.error.text() == "Pixel 8 didn't respond - the action may have gone through"
    assert [r["method"] for r in mock.requests][-1] == "status"  # the probe that cleared orbitd


def test_wedged_orbitd_reads_as_not_responding(qtbot, client, mock):
    scale_timeouts(client)
    panel = make_panel(qtbot, client)
    card = media_card(panel)
    mock.unresponsive = True
    qtbot.mouseClick(next_button(card), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.error.isVisibleTo(panel), timeout=3000)
    assert (card.error.text(), card.error.property("role")) == ("orbitd isn't responding", "error")
    assert next_button(card).property("pending") is False


def test_quick_methods_keep_short_timeouts(qtbot, client, mock):
    scale_timeouts(client)
    mock.unresponsive = True
    results = []
    client.request("activities.list", None, lambda r, e: results.append(e))
    qtbot.wait(8_000 // SCALE + 100)  # a wedged orbitd is reported within ~8 s for local methods
    assert results == [{"code": "orbitd_unresponsive", "message": "orbitd isn't responding"}]
