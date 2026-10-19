"""Device `state` / DeviceActivity `stale`: a phone briefly dropping must not make the panel flicker."""

import time

import pytest
from mock_orbitd import GALAXY_TAB, PIXEL_8, MockOrbitd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QToolButton

from orbit.applet import model
from orbit.applet.panel import Panel
from orbit.applet.theme import LIGHT


def make_panel(qtbot, client):
    panel = Panel(client, LIGHT, popup=False)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def cards_of(panel, device):
    return {k: c for k, c in panel.activities_page.cards.items() if k[0] == device}


def controls(card):
    """Every control on the card that would send something to the phone."""
    return [b for b in card.findChildren(QToolButton) if b.objectName().startswith("action:")]


def state_of(client, device):
    return next(d["state"] for d in client.devices if d["id"] == device)


# -- model -----------------------------------------------------------------------------------


@pytest.mark.parametrize("device, state", [
    ({"state": "reconnecting", "connected": False}, "reconnecting"),
    ({"state": "offline", "connected": False}, "offline"),
    ({"state": "connected", "connected": True}, "connected"),
    ({"connected": True}, "connected"),  # daemons predating `state`
    ({"connected": False}, "offline"),
    ({"state": "bogus", "connected": True}, "connected"),
])
def test_device_state(device, state):
    assert model.device_state(device) == state


def test_device_status_text_per_state():
    base = {"paired": True, "discovered": ["ble"]}
    assert model.device_status_text({**base, "state": "connected", "connected": True,
                                     "transport": "ble"}) == "Connected via Bluetooth"
    assert model.device_status_text({**base, "state": "reconnecting", "connected": False}) == "Reconnecting..."
    assert model.device_status_text({**base, "state": "offline", "connected": False}) == "Nearby (Bluetooth)"


def test_devices_summary():
    def d(name, state, paired=True):
        return {"name": name, "paired": paired, "state": state, "connected": state == "connected"}

    assert model.devices_summary([d("Pixel 8", "connected"), d("Tab", "connected")]) == "Pixel 8, Tab connected"
    assert model.devices_summary([d("Pixel 8", "reconnecting"), d("Tab", "connected")]) == \
        "Tab connected · Pixel 8 reconnecting..."
    assert model.devices_summary([d("OPPO", "reconnecting")]) == "OPPO reconnecting..."
    assert model.devices_summary([d("OPPO", "offline")]) == "OPPO offline"
    assert model.devices_summary([d("A", "offline"), d("B", "offline")]) == "No phone connected"
    assert model.devices_summary([d("Stranger", "connected", paired=False)]) == "No phone connected"


@pytest.mark.parametrize("error, result, text, gentle", [
    ({"code": "not_connected", "message": "reconnecting"}, None, "Reconnecting to Pixel 8...", True),
    ({"code": "not_connected", "message": "connection lost after the action was sent; it may have been applied"},
     None, "Connection dropped - the action may have gone through", True),
    ({"code": "failed", "message": "write failed; it may have been applied"}, None,
     "Connection dropped - the action may have gone through", True),
    ({"code": "not_connected", "message": "device is not connected"}, None, "Pixel 8 is offline", False),
    ({"code": "timeout", "message": "phone did not answer"}, None, "Pixel 8 didn't respond", False),
    ({"code": "not_paired", "message": "device is not paired"}, None, "Pixel 8 isn't paired", False),
    ({"code": model.ORBITD_DOWN, "message": "orbitd is not running"}, None, "orbitd is not running", False),
    ({"code": model.ORBITD_UNRESPONSIVE, "message": "orbitd isn't responding"}, None,
     "orbitd isn't responding", False),
    ({"code": "bad_request", "message": "media.seek needs an integer"}, None, "Next: media.seek needs an integer",
     False),
    (None, {"ok": False, "error": "phone is locked"}, "Next: phone is locked", False),
    (None, None, "Next: failed", False),
])
def test_action_failure_text(error, result, text, gentle):
    assert model.action_failure(error, result, "Pixel 8", "Next") == (text, gentle)


# -- mock ------------------------------------------------------------------------------------


def test_mock_drop_restore_and_grace_expiry(mock):
    before = [a for a in mock.activities if a["device"] == PIXEL_8]
    assert before and all(a["stale"] is False for a in before)
    assert {d["id"]: d["state"] for d in mock.devices}[PIXEL_8] == "connected"

    mock.drop_device(PIXEL_8)
    dev = next(d for d in mock.devices if d["id"] == PIXEL_8)
    assert (dev["state"], dev["connected"], dev["transport"]) == ("reconnecting", False, None)
    stale = [a for a in mock.activities if a["device"] == PIXEL_8]
    assert [a["id"] for a in stale] == [a["id"] for a in before]
    assert all(a["stale"] is True for a in stale)
    assert all(a["stale"] is False for a in mock.activities if a["device"] == GALAXY_TAB)

    mock.restore_device(PIXEL_8)
    dev = next(d for d in mock.devices if d["id"] == PIXEL_8)
    assert (dev["state"], dev["connected"], dev["transport"]) == ("connected", True, "lan")
    assert all(a["stale"] is False for a in mock.activities if a["device"] == PIXEL_8)

    mock.drop_device(PIXEL_8)
    mock.expire_device(PIXEL_8)
    assert next(d for d in mock.devices if d["id"] == PIXEL_8)["state"] == "offline"
    assert not [a for a in mock.activities if a["device"] == PIXEL_8]


def test_mock_grace_period_runs_out_by_itself(sock_path):
    m = MockOrbitd(sock_path, evolve=False)
    m.reconnect_grace_s = 0.2
    m.start()
    try:
        m.drop_device(PIXEL_8)
        assert next(d for d in m.devices if d["id"] == PIXEL_8)["state"] == "reconnecting"
        time.sleep(0.5)
        assert next(d for d in m.devices if d["id"] == PIXEL_8)["state"] == "offline"
        assert not [a for a in m.activities if a["device"] == PIXEL_8]
    finally:
        m.stop()


def test_mock_holds_actions_then_fails_with_reconnecting(qtbot, client, mock):
    media = next(a for a in mock.activities if a["kind"] == "media")
    mock.reconnect_hold_s = 0.3
    mock.drop_device(PIXEL_8)
    results = []
    client.action(PIXEL_8, media["id"], "media.next", callback=lambda r, e: results.append((r, e)))
    qtbot.wait(150)
    assert results == []  # held, not failed straight away
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [(None, {"code": "not_connected", "message": "reconnecting"})]

    # Back within the hold: the action goes through.
    mock.reconnect_hold_s = 5
    results.clear()
    client.action(PIXEL_8, media["id"], "media.next", callback=lambda r, e: results.append((r, e)))
    qtbot.wait(150)
    mock.restore_device(PIXEL_8)
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [({"ok": True}, None)]


# -- panel -----------------------------------------------------------------------------------


def test_stale_cards_keep_layout_freeze_and_disable_then_recover(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    fresh = cards_of(panel, PIXEL_8)
    media = next(c for c in fresh.values() if c.activity["kind"] == "media")
    timer = next(c for c in fresh.values() if c.activity["kind"] == "timer")
    reply = next(c for c in fresh.values() if c.findChild(QLineEdit))
    field = reply.findChild(QLineEdit)
    other = next(iter(cards_of(panel, GALAXY_TAB).values()))
    qtbot.keyClicks(field, "on my way")
    heights = {k: c.height() for k, c in fresh.items()}
    assert all(controls(c) for c in fresh.values())
    assert all(b.isEnabled() for c in fresh.values() for b in controls(c))
    assert media.seek.isEnabled()

    mock.drop_device(PIXEL_8)
    qtbot.waitUntil(lambda: media.stale, timeout=3000)
    qtbot.wait(50)

    # Same widgets, same place, same size: no flicker.
    assert cards_of(panel, PIXEL_8) == fresh
    assert {k: c.height() for k, c in fresh.items()} == heights
    for card in fresh.values():
        # Said once per device (panel and group header), not on every card.
        assert not any("reconnecting" in w.text().lower() for w in card.findChildren(QLabel) if w.isVisibleTo(card))
        assert card._dim.isEnabled()
        for b in controls(card):
            assert not b.isEnabled(), b.objectName()
            assert b.toolTip() == "Reconnecting to Pixel 8"
        if card.action_strip is not None:
            assert not card.action_strip.more.isEnabled()
    assert not media.seek.isEnabled()
    assert field.isEnabled() and field.text() == "on my way"  # the draft stays editable
    assert not other.stale and not other._dim.isEnabled()
    assert all(b.isEnabled() for b in controls(other))
    assert panel.status.fullText() == "Galaxy Tab S9 connected · Pixel 8 reconnecting..."
    headers = [h.text() for h in panel.activities_page._headers]
    assert "Pixel 8 - reconnecting..." in headers and "Galaxy Tab S9" in headers

    # Extrapolation stops: an unconfirmed position or timer does not keep moving.
    pos, elapsed, chrono = media.seek.value(), media.elapsed.text(), timer.chrono.text()
    later = model.now_ms() + 30_000
    media.tick(later)
    timer.tick(later)
    assert (media.seek.value(), media.elapsed.text(), timer.chrono.text()) == (pos, elapsed, chrono)

    # Nothing reaches the daemon from a stale card, not even via Return in the reply field.
    mock.clear_recorded()
    qtbot.mouseClick(next(b for b in controls(media) if b.objectName() == "action:media.next"),
                     Qt.MouseButton.LeftButton)
    qtbot.keyClick(field, Qt.Key.Key_Return)
    media.trigger("media.pause")
    qtbot.wait(200)
    assert mock.actions() == []

    mock.restore_device(PIXEL_8)
    qtbot.waitUntil(lambda: not media.stale, timeout=3000)
    assert cards_of(panel, PIXEL_8) == fresh
    for card in fresh.values():
        assert not card._dim.isEnabled()
        assert all(b.isEnabled() for b in controls(card))
        assert all(b.toolTip() != "Reconnecting to Pixel 8" for b in controls(card))
    assert media.seek.isEnabled()
    assert panel.status.fullText() == "Pixel 8, Galaxy Tab S9 connected"
    media.tick(model.now_ms() + 30_000)
    assert media.seek.value() > pos  # live again


def test_held_action_failing_with_reconnecting_is_gentle(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    media = next(c for c in cards_of(panel, PIXEL_8).values() if c.activity["kind"] == "media")
    mock.reconnect_hold_s = 0.3
    # Clicked just as the link drops: the card has not heard about it yet.
    mock.drop_device(PIXEL_8)
    media.trigger("media.next")
    qtbot.waitUntil(lambda: media.error.isVisibleTo(panel), timeout=3000)
    assert media.error.text() == "Reconnecting to Pixel 8..."
    assert media.error.property("role") == "notice"
    assert media.error.palette().color(media.error.foregroundRole()).name() == LIGHT.muted

    # A real failure afterwards is still red.
    mock.restore_device(PIXEL_8)
    qtbot.waitUntil(lambda: not media.stale, timeout=3000)
    mock.fail_next_action = "phone is locked"
    media.trigger("media.next")
    qtbot.waitUntil(lambda: media.error.text() == "Next: phone is locked", timeout=3000)
    assert media.error.property("role") == "error"
    assert media.error.palette().color(media.error.foregroundRole()).name() == LIGHT.danger


def test_grace_expiry_removes_stale_cards(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    assert cards_of(panel, PIXEL_8)
    mock.drop_device(PIXEL_8)
    qtbot.waitUntil(lambda: all(c.stale for c in cards_of(panel, PIXEL_8).values()), timeout=3000)
    mock.expire_device(PIXEL_8)
    qtbot.waitUntil(lambda: not cards_of(panel, PIXEL_8), timeout=3000)
    assert cards_of(panel, GALAXY_TAB)
    assert panel.status.fullText() == "Galaxy Tab S9 connected"


def test_reconnecting_without_activities_has_its_own_empty_state(qtbot, sock_path):
    from applet_helpers import make_mock

    from orbit.applet.client import DaemonClient

    m = make_mock(sock_path, scenario="idle")
    c = DaemonClient(sock_path)
    c.start()
    try:
        qtbot.waitUntil(lambda: bool(c.connected and c.devices), timeout=5000)
        panel = make_panel(qtbot, c)
        qtbot.waitUntil(lambda: panel.activities_page.empty.headline.text() == "Nothing live right now")
        m.drop_device(PIXEL_8)
        qtbot.waitUntil(lambda: panel.activities_page.empty.headline.text() == "Reconnecting to Pixel 8...",
                        timeout=3000)
        assert panel.status.fullText() == "Pixel 8 reconnecting..."
        m.expire_device(PIXEL_8)
        qtbot.waitUntil(lambda: panel.activities_page.empty.headline.text() == "No phone connected", timeout=3000)
        assert panel.status.fullText() == "Pixel 8 offline"
    finally:
        c.stop()
        m.stop()


def test_devices_page_shows_reconnecting_and_disconnect_stops_it(qtbot, client, mock):
    panel = make_panel(qtbot, client)
    panel.show_devices()
    mock.drop_device(PIXEL_8)
    page = panel.devices_page
    qtbot.waitUntil(lambda: state_of(client, PIXEL_8) == "reconnecting", timeout=3000)
    row = page._rows[PIXEL_8]
    assert row.status.fullText() == "Reconnecting..."
    assert set(row.buttons) == {"Disconnect", "Forget"}
    qtbot.mouseClick(row.findChild(QPushButton, "device:disconnect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: state_of(client, PIXEL_8) == "offline", timeout=3000)
    assert any(r == {"method": "devices.disconnect", "params": {"id": PIXEL_8}} for r in mock.requests)
    qtbot.waitUntil(lambda: set(page._rows[PIXEL_8].buttons) == {"Connect", "Forget"}, timeout=3000)
